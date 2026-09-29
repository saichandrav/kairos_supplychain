"""Para-19 backtest (plan §4.5): replay A1 → A2 → A3 month by month with no look-ahead.

    python tests/backtest_para19.py            # writes docs/backtest_results.md and docs/img/backtest_leadtime.png

For each month M in the window, a fresh in-memory database is built from data/seed that holds ONLY rows
dated before M (asserted per dated table), and A1–A3 run with as_of = the day before M.

Positives: para19_events.csv rows with VERIFY=false, a FORM_ID and an ORDER_DATE, that also have an A1 cost
model (FF_REF_BOM_ASSUMPTION + FF_REF_FORM_API). Negatives: a seeded random sample of the other targets
with a cost model, excluding every Para-19-flagged target (unverified ones could be positives).

Hospital MM tables are left out: they are a present-day SYNTH snapshot and the score here is formulation-level.
Ceilings before the first one on record are back-cast by the annual WPI change (DPCO para 16), tagged PROXY.

This is a pipeline check, not evidence: API costs are SYNTH (see ingest/tradestat.py) and are anchored on
today's ceilings, which for the positives are the post-order ceilings.
"""
from __future__ import annotations

import csv
import math
import random
import statistics
import sys
if sys.platform == "win32" and hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path
from types import SimpleNamespace

import pandas as pd

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from agents import a1_margin_sentinel as a1  # noqa: E402
from agents import a2_dependency as a2  # noqa: E402
from agents import a3_forecast as a3  # noqa: E402
from agents.contracts import RunContext, SignalBand  # noqa: E402
from agents.ctx import Db, Settings, get_db  # noqa: E402
from agents.rules import Rules, load_rules  # noqa: E402
from ingest import load_hana as lh  # noqa: E402

SEED_DIR = REPO_ROOT / "data" / "seed"
RESULTS_MD = REPO_ROOT / "docs" / "backtest_results.md"
CHART_PNG = REPO_ROOT / "docs" / "img" / "backtest_leadtime.png"

WINDOW_MONTHS = 24          # snapshots before (and including) the order month
NEG_SAMPLE = 15
RNG_SEED = 42
TOP_K = 10
SCENARIOS = {"base": 1.0, "cost −20%": 0.8, "cost +20%": 1.2}
WPI_SERIES = "MANUFACTURED_PRODUCTS"   # proxy for the index NPPA applies under para 16
FLAG_BANDS = (SignalBand.AMBER, SignalBand.RED)

DATED = {  # table -> ISO date column; only rows dated before the snapshot month are copied
    "FF_REF_CEILING_PRICE": "EFFECTIVE_FROM",
    "FF_REF_API_COST_MONTHLY": "MONTH",
    "FF_REF_API_ORIGIN": "MONTH",
    "FF_REF_WPI": "MONTH",
    "FF_REF_NSQ_ALERT": "MONTH",
}
STATIC = ("FF_REF_FORMULATION", "FF_REF_API", "FF_REF_FORM_API", "FF_REF_BOM_ASSUMPTION",
          "FF_REF_PRODUCER", "FF_REF_MANUFACTURER")


# ---------------------------------------------------------------- data


def _read(name: str) -> list[dict[str, str]]:
    with (SEED_DIR / f"{name}.csv").open(encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


def _month_start(d: date) -> date:
    return d.replace(day=1)


def _add_months(d: date, n: int) -> date:
    y, m = divmod(d.month - 1 + n, 12)
    return date(d.year + y, m + 1, 1)


def load_base() -> Db:
    db = get_db(Settings(db_backend="sqlite"), sqlite_path=":memory:")
    lh.create_objects(db)
    lh.seed(db, [SEED_DIR])
    return db


def backfill_ceilings(db: Db, first_year: int) -> int:
    """Back-cast April ceilings before the earliest on record: CP(Apr y) = CP(Apr y+1) / (1 + g(y)),
    g(y) = mean WPI(calendar y) / mean WPI(calendar y−1) − 1 (DPCO para 16 annual revision). Tagged PROXY."""
    wpi = db.query("SELECT MONTH, INDEX_VALUE FROM FF_REF_WPI WHERE SERIES = ?", (WPI_SERIES,))
    wpi = wpi.assign(Y=pd.to_datetime(wpi["MONTH"]).dt.year, V=wpi["INDEX_VALUE"].astype(float))
    annual = wpi.groupby("Y")["V"].mean()
    cp = db.query("SELECT FORM_ID, EFFECTIVE_FROM, CEILING_PRICE FROM FF_REF_CEILING_PRICE")
    rows = []
    for form_id, g in cp.groupby("FORM_ID"):
        first = g.sort_values("EFFECTIVE_FROM").iloc[0]
        price, year = float(first["CEILING_PRICE"]), int(str(first["EFFECTIVE_FROM"])[:4])
        for y in range(year - 1, first_year - 1, -1):
            if y not in annual or (y - 1) not in annual:
                break
            price = price / (annual[y] / annual[y - 1])
            rows.append({"FORM_ID": form_id, "EFFECTIVE_FROM": f"{y}-04-01", "SO_NUMBER": f"PROXY-{y}",
                         "CEILING_PRICE": round(price, 4), "PARA": "16", "SOURCE": "backtest WPI back-cast (para 16)",
                         "IS_PROXY": "PROXY"})
    lh.upsert_rows(db, lh.schema_tables()["FF_REF_CEILING_PRICE"], rows)
    return len(rows)


def snapshot(base: Db, month: date) -> Db:
    """A fresh database with static reference rows and only dated rows before `month`."""
    snap = get_db(Settings(db_backend="sqlite"), sqlite_path=":memory:")
    lh.create_objects(snap)
    tables = lh.schema_tables()
    cutoff = month.isoformat()
    for t in STATIC + tuple(DATED):
        df = base.query(f"SELECT * FROM {t}")
        if t in DATED:
            df = df[df[DATED[t]].astype(str).str[:10] < cutoff]
        if len(df):
            lh.upsert_rows(snap, tables[t], df.astype(object).where(df.notna(), None).to_dict("records"))
    assert_no_lookahead(snap, month)
    return snap


def assert_no_lookahead(db: Db, month: date) -> None:
    for t, col in DATED.items():
        mx = db.query(f"SELECT MAX({col}) AS M FROM {t}").iloc[0]["M"]
        assert mx is None or str(mx)[:10] < month.isoformat(), f"look-ahead: {t}.{col} = {mx} >= {month}"


# ---------------------------------------------------------------- universe


@dataclass
class Universe:
    positives: dict[str, date]           # FORM_ID -> order date
    negatives: list[str]
    skipped_verify: int
    skipped_no_cost_model: list[str]
    excluded_unverified: list[str]


def build_universe(base: Db) -> Universe:
    modelled = set(base.query("SELECT b.FORM_ID FROM FF_REF_BOM_ASSUMPTION b "
                              "JOIN FF_REF_FORM_API f ON f.FORM_ID = b.FORM_ID")["FORM_ID"].astype(str))
    events = _read("para19_events")
    confirmed = [e for e in events if e["VERIFY"].strip().lower() == "false" and e["FORM_ID"] and e["ORDER_DATE"]]
    positives = {e["FORM_ID"]: date.fromisoformat(e["ORDER_DATE"]) for e in confirmed if e["FORM_ID"] in modelled}
    targets = _read("targets")
    para19_targets = {t["FORM_ID"] for t in targets if t["PARA19"].strip().lower() == "yes"}
    pool = sorted(t["FORM_ID"] for t in targets
                  if t["FORM_ID"] in modelled and t["FORM_ID"] not in para19_targets and t["FORM_ID"] not in positives)
    negatives = sorted(random.Random(RNG_SEED).sample(pool, min(NEG_SAMPLE, len(pool))))
    return Universe(
        positives=positives,
        negatives=negatives,
        skipped_verify=sum(1 for e in events if e["VERIFY"].strip().lower() != "false"),
        skipped_no_cost_model=sorted(e["FORM_ID"] for e in confirmed if e["FORM_ID"] not in modelled),
        excluded_unverified=sorted(para19_targets & modelled - set(positives)),
    )


# ---------------------------------------------------------------- replay


@dataclass
class Point:
    exit_risk: float
    band: SignalBand
    has_margin: bool  # A1 could compute headroom (ceiling + cost on record)

    @property
    def flagged(self) -> bool:
        return self.has_margin and self.band in FLAG_BANDS


@dataclass
class Replay:
    months: list[date]
    history: dict[str, dict[str, dict[date, Point]]] = field(default_factory=dict)  # scenario -> form -> month


def replay(base: Db, forms: list[str], months: list[date], rules: Rules) -> Replay:
    out = Replay(months, {s: {f: {} for f in forms} for s in SCENARIOS})
    for i, m in enumerate(months, start=1):
        snap = snapshot(base, m)
        as_of = m - timedelta(days=1)
        for name, mult in SCENARIOS.items():
            ctx = SimpleNamespace(db=snap, rules=rules,
                                  run=RunContext(run_id=f"bt-{m:%Y%m}", as_of=as_of, dry_run=True))
            sigs = a1.run(forms, ctx, api_cost_multiplier=mult)
            deps = a2.run(forms, ctx, engine="networkx")
            by_sig = {s.formulation_id: s for s in sigs}
            for fc in a3.run(sigs, deps, ctx):
                out.history[name][fc.formulation_id][m] = Point(
                    fc.exit_risk or 0.0, fc.exit_risk_band, math.isfinite(by_sig[fc.formulation_id].headroom_pct))
        snap.close()
        print(f"  {m:%Y-%m} ({i}/{len(months)})", end="\r", flush=True)
    print()
    return out


# ---------------------------------------------------------------- metrics


@dataclass
class Metrics:
    lead_days: dict[str, int | None]     # per positive; None = never flagged in the window
    censored: set[str]                   # flagged already in the window's first month: true lead is longer
    recall: float
    precision_at_k: float
    max_precision_at_k: float
    neg_flag_rate: float
    rank_of_positives: dict[str, int]

    @property
    def leads(self) -> list[int]:
        return [d for d in self.lead_days.values() if d is not None]


def metrics(rp: Replay, uni: Universe, scenario: str) -> Metrics:
    h = rp.history[scenario]
    lead: dict[str, int | None] = {}
    censored: set[str] = set()
    for f, order in uni.positives.items():
        order_m = _month_start(order)
        window = [m for m in rp.months if _add_months(order_m, -WINDOW_MONTHS) <= m <= order_m]
        first = next((m for m in window if h[f].get(m) and h[f][m].flagged), None)
        lead[f] = (order - (first - timedelta(days=1))).days if first else None
        if first is not None and first == window[0]:
            censored.add(f)
    # ranking at the last snapshot before the (latest) order month
    at = max(_month_start(d) for d in uni.positives.values())
    scores = {f: h[f][at].exit_risk for f in h if at in h[f] and h[f][at].has_margin}
    ranked = sorted(scores, key=lambda f: (-scores[f], f))
    top = ranked[:TOP_K]
    negs = [f for f in uni.negatives if f in scores]
    return Metrics(
        lead_days=lead,
        censored=censored,
        recall=sum(1 for d in lead.values() if d is not None) / len(lead) if lead else 0.0,
        precision_at_k=sum(1 for f in top if f in uni.positives) / TOP_K,
        max_precision_at_k=min(len(uni.positives), TOP_K) / TOP_K,
        neg_flag_rate=sum(1 for f in negs if h[f][at].flagged) / len(negs) if negs else 0.0,
        rank_of_positives={f: ranked.index(f) + 1 for f in uni.positives if f in ranked},
    )


# ---------------------------------------------------------------- report


def _fmt_lead(m: Metrics) -> str:
    if not m.leads:
        return "none flagged"
    med = statistics.median(m.leads)
    ge = "≥ " if m.censored else ""
    return f"{ge}{med:.0f} d (range {min(m.leads)}–{ge}{max(m.leads)})"


def write_markdown(uni: Universe, rp: Replay, res: dict[str, Metrics], n_backcast: int, rules: Rules) -> str:
    base = res["base"]
    lines = [
        "# Para-19 backtest: pipeline check (not evidence)",
        "",
        f"Generated by `python tests/backtest_para19.py` · plan §4.5 · weights `rules/weights.yaml` "
        f"v{rules.versions['weights']} (frozen)",
        "",
        "> **Read this first.** The API cost series are **synthetic** (`ingest/tradestat.py`, IS_PROXY = SYNTH) "
        "because TradeStat could not be downloaded in time, and they are anchored on today's ceilings, which for "
        "the positives are the *post-order* ceilings. Only "
        f"{len(uni.positives)} confirmed Para-19 formulation(s) can be scored. These numbers show that the "
        "replay runs end to end without look-ahead; they say nothing about whether the signal works. "
        "Do not put them on a slide as results.",
        "",
        "## Setup",
        "",
        f"- Window: {rp.months[0]:%b %Y} – {rp.months[-1]:%b %Y}, one snapshot per month; each snapshot holds only rows "
        f"dated before that month (asserted for {', '.join(f'`{t}`' for t in DATED)}).",
        f"- Positives scored: **{len(uni.positives)}** — "
        + ", ".join(f"`{f}` (order {d.isoformat()})" for f, d in sorted(uni.positives.items())) + ".",
        f"- Skipped: **{uni.skipped_verify}** event rows still marked `VERIFY=true`; "
        f"**{len(uni.skipped_no_cost_model)}** confirmed rows with no API cost model (vaccines/immunoglobulins): "
        + ", ".join(f"`{f}`" for f in uni.skipped_no_cost_model) + ".",
        f"- Excluded from negatives: {len(uni.excluded_unverified)} Para-19-flagged targets awaiting verification "
        "(they may be positives).",
        f"- Negatives: {len(uni.negatives)} targets sampled with seed {RNG_SEED} from the non-Para-19 targets that have a "
        "cost model.",
        f"- Ceilings before April 2026 are back-cast by the annual WPI change ({WPI_SERIES}, DPCO para 16): "
        f"{n_backcast} PROXY rows.",
        "- A flag = A3 exit-risk band AMBER or RED in a month where A1 had both a ceiling and a cost series. "
        "Lead time = order date − as-of date of the first flag in the 24 months before the order; "
        "\"≥\" means it was already flagged in the first month of the window (censored).",
        f"- Precision@{TOP_K} and the negative flag rate are taken at the last snapshot before the order month; "
        f"with {len(uni.positives)} positive(s) the best possible precision@{TOP_K} is {base.max_precision_at_k:.1f}.",
        "",
        "## Results",
        "",
        f"| Scenario | Lead time (median, range) | Recall | Precision@{TOP_K} (max {base.max_precision_at_k:.1f}) "
        "| Negatives flagged |",
        "|---|---|---|---|---|",
    ]
    for name, m in res.items():
        lines.append(f"| {name} | {_fmt_lead(m)} | {m.recall:.0%} | {m.precision_at_k:.2f} | {m.neg_flag_rate:.0%} |")
    lines += ["", "Per positive (base):", "", "| Formulation | Order date | Lead time | Rank by exit risk |", "|---|---|---|---|"]
    n_ranked = len({f for f in rp.history["base"]})
    for f, d in sorted(uni.positives.items()):
        lt = base.lead_days[f]
        lt_txt = "not flagged" if lt is None else f"{'≥ ' if f in base.censored else ''}{lt} d"
        lines.append(f"| `{f}` | {d.isoformat()} | {lt_txt} | "
                     f"{base.rank_of_positives.get(f, '–')} of {n_ranked} |")
    verdict = _verdict(uni, res)
    lines += [
        "",
        "## What this shows",
        "",
        verdict,
        "",
        "## Caveats",
        "",
        "- **Proxy and synthetic inputs.** API cost is a seeded random walk, not TradeStat. Conversion, packaging and "
        "freight costs are BOM assumptions. Ceilings before April 2026 are WPI back-casts.",
        "- **Built-in leakage on the cost side.** Each synthetic cost series ends at a level set from today's ceiling. "
        "For the positives that is the raised ceiling, so the history is derived from the outcome.",
        "- **Static structure.** Producer lists and market shares are today's; the replay can't see past entries or exits.",
        "- **Tiny sample.** "
        f"{len(uni.positives)} positive(s) from one order ({', '.join(sorted({d.isoformat() for d in uni.positives.values()}))}); "
        f"{len(uni.negatives)} negatives. Any single formulation moves recall by "
        f"{1 / max(1, len(uni.positives)):.0%}.",
        "- **No publication lag.** A snapshot sees the previous month's cost; real trade data arrives about two months late.",
        "",
        "## Weights",
        "",
        "No weight change. With synthetic, outcome-anchored costs and two positives, any tuning would fit noise. "
        f"`rules/weights.yaml` is frozen at v{rules.versions['weights']} (hand-set, plan §4.3). Re-run this script "
        "once the Oct-2024 / Dec-2019 Para-19 rows are verified and real TradeStat series replace the synthetic ones.",
        "",
        "![Exit risk before the order](img/backtest_leadtime.png)",
        "",
    ]
    return "\n".join(lines)


def _verdict(uni: Universe, res: dict[str, Metrics]) -> str:
    b = res["base"]
    parts = [f"In the base run {sum(1 for d in b.lead_days.values() if d is not None)} of {len(uni.positives)} "
             f"positive(s) were flagged before the order ({_fmt_lead(b)}), and {b.neg_flag_rate:.0%} of negatives "
             "were flagged at the same time."]
    if b.censored:
        parts.append("Lead times marked ≥ hit the window edge: the formulation was flagged from the first snapshot, "
                     "which with synthetic costs reflects where the random walk started, not an early warning.")
    if b.neg_flag_rate >= 0.5:
        parts.append("The flag is not selective: most negatives are flagged too, so the lead time on its own means little.")
    rates = [m.neg_flag_rate for m in res.values()]
    parts.append(f"Under ±20% cost the share of negatives flagged ranges from {min(rates):.0%} to {max(rates):.0%} "
                 f"and the median lead time from {_fmt_lead(res['cost −20%'])} to {_fmt_lead(res['cost +20%'])}: "
                 "the outcome is driven by the level of the cost proxy, not by a stable signal.")
    parts.append("**This is weak, and it is not evidence either way**: with synthetic costs and two positives the "
                 "backtest can only show that the replay machinery works without look-ahead.")
    return " ".join(parts)


# ---------------------------------------------------------------- main


def main() -> int:
    rules = load_rules()
    print("loading data/seed ...")
    base = load_base()
    uni = build_universe(base)
    if not uni.positives:
        print("no confirmed positives with a cost model; nothing to backtest")
        return 1
    last = max(_month_start(d) for d in uni.positives.values())
    months = [_add_months(last, -k) for k in range(WINDOW_MONTHS, -1, -1)]
    n_backcast = backfill_ceilings(base, months[0].year - 1)
    forms = sorted(set(uni.positives) | set(uni.negatives))
    print(f"positives={len(uni.positives)} negatives={len(uni.negatives)} skipped(verify)={uni.skipped_verify} "
          f"skipped(no cost model)={len(uni.skipped_no_cost_model)} months={len(months)} backcast ceilings={n_backcast}")
    rp = replay(base, forms, months, rules)
    res = {name: metrics(rp, uni, name) for name in SCENARIOS}
    for name, m in res.items():
        print(f"{name:>10}: lead {_fmt_lead(m)} · recall {m.recall:.0%} · precision@{TOP_K} {m.precision_at_k:.2f} "
              f"(max {m.max_precision_at_k:.1f}) · negatives flagged {m.neg_flag_rate:.0%}")
    RESULTS_MD.write_text(write_markdown(uni, rp, res, n_backcast, rules), encoding="utf-8")
    plot(rp, uni, rules)
    print(f"wrote {RESULTS_MD.relative_to(REPO_ROOT)} and {CHART_PNG.relative_to(REPO_ROOT)}")
    return 0


# Reference palette (dataviz skill, light mode): categorical slots 1–2 validated; neutrals for context.
SURFACE, INK, INK_2, GRID, NEUTRAL = "#fcfcfb", "#0b0b0b", "#52514e", "#e4e3df", "#8a8984"
SERIES = ("#2a78d6", "#eb6834")


def plot(rp: Replay, uni: Universe, rules: Rules) -> None:
    """Exit risk per month: positives as lines, negatives as median + IQR band, AMBER threshold, order date."""
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    h = rp.history["base"]
    xs = [m - timedelta(days=1) for m in rp.months]  # as-of dates
    amber = float(rules.value("weights", "exit_risk.band_amber"))

    fig, ax = plt.subplots(figsize=(10, 5.2), dpi=200)
    fig.patch.set_facecolor(SURFACE)
    ax.set_facecolor(SURFACE)

    neg = pd.DataFrame({f: [h[f][m].exit_risk if m in h[f] and h[f][m].has_margin else float("nan")
                            for m in rp.months] for f in uni.negatives}, index=xs)
    if not neg.empty:
        q = neg.quantile([0.25, 0.5, 0.75], axis=1).T
        ax.fill_between(xs, q[0.25], q[0.75], color=NEUTRAL, alpha=0.18, linewidth=0,
                        label=f"Negatives ({len(uni.negatives)}): middle 50%")
        ax.plot(xs, q[0.5], color=NEUTRAL, linewidth=2, label="Negatives: median")

    for (f, order), color in zip(sorted(uni.positives.items()), SERIES):
        ys = [h[f][m].exit_risk if m in h[f] and h[f][m].has_margin else float("nan") for m in rp.months]
        name = f.rsplit("-", 1)[0].title()
        ax.plot(xs, ys, color=color, linewidth=2, label=f"{name} (Para-19, {order:%d %b %Y})")
        last = next((i for i in range(len(ys) - 1, -1, -1) if not math.isnan(ys[i])), None)
        if last is not None:
            ax.annotate(name, (xs[last], ys[last]), xytext=(6, 0), textcoords="offset points",
                        va="center", fontsize=9, color=INK)

    ax.axhline(amber, color=INK_2, linewidth=1, linestyle=(0, (4, 3)))
    ax.text(xs[0], amber, f" AMBER threshold ({amber:.1f})", va="bottom", fontsize=8.5, color=INK_2)
    for order in sorted(set(uni.positives.values())):
        ax.axvline(order, color=INK_2, linewidth=1)
        ax.text(order, 0.02, "Para-19 order ", ha="right", va="bottom", fontsize=8.5, color=INK_2)

    ax.set_ylim(0, 1.02)
    ax.set_ylabel("A3 exit risk", color=INK_2, fontsize=9)
    ax.set_title("Exit risk before the June 2026 Para-19 order (monthly replay, no look-ahead)",
                 loc="left", fontsize=11.5, color=INK, pad=12)
    ax.grid(axis="y", color=GRID, linewidth=0.8)
    ax.set_axisbelow(True)
    for side in ("top", "right", "left"):
        ax.spines[side].set_visible(False)
    ax.spines["bottom"].set_color(GRID)
    ax.tick_params(colors=INK_2, labelsize=8.5, length=0)
    fig.autofmt_xdate(rotation=0, ha="center")
    ax.legend(loc="upper left", bbox_to_anchor=(0, -0.1), ncol=2, frameon=False, fontsize=8.5, labelcolor=INK)
    ax.text(0.5, 0.5, "SYNTHETIC COST DATA\npipeline check, not evidence", transform=ax.transAxes,
            ha="center", va="center", fontsize=26, color=INK_2, alpha=0.12, rotation=18, fontweight="bold")
    fig.text(0.01, 0.01, "Source: Kairos replay of A1–A3 on data/seed; API costs SYNTH (ingest/tradestat.py); "
             "pre-2026 ceilings WPI back-cast (PROXY).", fontsize=7, color=INK_2)
    CHART_PNG.parent.mkdir(parents=True, exist_ok=True)
    fig.tight_layout(rect=(0, 0.03, 1, 1))
    fig.savefig(CHART_PNG, facecolor=SURFACE)
    plt.close(fig)


if __name__ == "__main__":
    raise SystemExit(main())
