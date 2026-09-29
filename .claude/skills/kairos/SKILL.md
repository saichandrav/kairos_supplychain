---
name: kairos
description: Working rules for the Kairos repo (SAP Hackfest 2026) — a 6-agent + human-approval system that flags NPPA/DPCO price-controlled medicines whose production economics are breaking before a shortage appears, built on SAP HANA Cloud, SAP BTP and SAP Build. Use for any coding, data, SAP, demo or commit task in this repo.
---

# Kairos working rules

## 1. Load context cheaply (do this first, every session)
1. Read `docs/STATUS.md` (short by design). It says what is done, what is in progress, who owns what, and what is blocked.
2. Do NOT read `docs/IMPLEMENTATION_PLAN.md` end to end. Grep for the section you need and read only that range:
   `Grep "^## §" docs/IMPLEMENTATION_PLAN.md` → pick the section → `Read` with offset/limit.
   Section map: §1 problem & scope · §2 architecture · §3 agents (A1–A6 + human gate) · §4 scoring rules · §5 data sources · §6 HANA data model · §7 SAP tools mapping · §8 repo layout · §9 team split & timeline · §10 demo script · §11 risks · §12 deck fixes · §13 copy-paste stage prompts (S00–S13, reviewer 13.R, Git steps 13.0).
   If the user pastes a stage prompt, follow it exactly, including the §13.0 Git steps. Stay within that stage's files.
3. For agent inputs and outputs, read `agents/contracts.py` only. It is the single source of truth, so you don't need to open every agent file.
4. Rules and thresholds live in `rules/*.yaml`. Change numbers there, not in code.

## 2. Token discipline
- Never print whole datasets or large files. Use `head -5`, `wc -l`, and `LIMIT 20` in SQL. Summarise query results; don't paste them.
- Read files with offset/limit when you know the region. Don't re-read a file you just edited.
- Prefer Grep/Glob over directory walks. Don't open `data/raw/**`; it is large and gitignored.
- Don't spawn subagents unless the user asks.
- Replies: short. Show the diff or the changed function, not the whole file. No recap of what the plan already says.
- End of a work block: update `docs/STATUS.md` in place (keep it under ~60 lines, overwrite old "Now/Next" lines instead of appending history). Add a one-line entry to `docs/DECISIONS.md` for any design decision.

## 3. Git and commits (hard rules)
- **No Claude attribution anywhere.** Never add a `Co-Authored-By: Claude …` trailer, a "Generated with Claude Code" line, or a session link to commits or PR bodies. `.claude/settings.json` disables this, and `scripts/git-hooks/commit-msg` strips it as a backstop. Enable the hook once per clone: `git config core.hooksPath scripts/git-hooks`.
- Commit or push only when the user asks. Use Conventional Commits (`feat(a1): margin headroom calc`, `fix(hana): …`, `data: …`, `docs: …`), with the scope set to the agent or module.
- One logical change per commit. Work on `feat/<owner>-<topic>` branches and merge to `main` only when `pytest -q` passes.
- Never commit `.env`, credentials, the HANA password, API keys, `data/raw/**`, or patient/hospital-identifying data.

## 4. Secrets and the shared HANA instance
- HANA host, user, and password come only from env (`HANA_HOST`, `HANA_PORT=443`, `HANA_USER`, `HANA_PASSWORD`). Never hard-code them or echo them in output.
- The Hackfest HANA password is shared across every participant, so any team can log in as our user. Treat the DB as untrusted and disposable:
  - Keep all DDL and loads idempotent (`db/*.sql`, `ingest/load_hana.py --reset`) so the schema can be rebuilt in minutes.
  - Prefix every object with `FF_` inside our own schema. Never touch other schemas.
  - Keep queries light; the instance is shared (~14 GB). No `SELECT *` on big tables, and no cross joins.
- `DB_BACKEND=sqlite` must keep working as the offline demo fallback.

## 5. Domain guardrails (must hold in code, UI and deck)
- Scores come from deterministic rules (`rules/weights.yaml`). The LLM only writes the explanation from a JSON fact sheet. It never computes, changes, or invents a number. `agents/explain.py` checks that every number in the LLM text exists in the fact sheet; if not, it falls back to the template.
- Every output is a **probabilistic flag for review**. Never state or imply a manufacturer's intent ("X will discontinue"). Say "margin headroom below threshold; exit risk elevated".
- Every data row carries `SOURCE`, `SOURCE_URL`, `FETCHED_AT` and `IS_PROXY` (real / proxy / synthetic). The UI shows the label.
- Producer counts are lower bounds: always render "at least N producers".
- NPPA ceiling prices are **exclusive of GST**. A recommended procurement price may never exceed ceiling + GST (DPCO para 14). A5 blocks it.
- No action reaches the S/4-shaped PR endpoint without a row in `FF_APPROVAL` from a human. A6 enforces this.

## 6. Conventions
- Python 3.11, FastAPI, pydantic v2, `hdbcli` + `hana-ml`, pytest. Type hints on public functions. Money in INR `DECIMAL(15,4)`; dates in ISO.
- Each agent is a pure function `run(input: AxIn, ctx: Ctx) -> AxOut` in `agents/aN_*.py`. No agent calls another directly; only `agents/orchestrator.py` sequences them.
- The audit log is append-only and hash-chained (`FF_AUDIT_LOG.PREV_HASH`). Never UPDATE or DELETE it.
- Before claiming something works, run it: `pytest -q`, or `python -m agents.orchestrator --molecule <id> --dry-run`.
