# Kairos: MVP Implementation Plan
SAP Hackfest 2026 · South & East Region Finale, SRM Chennai · Kairos
Theme: Resilient Supply Chains · Idea: predict pharmaceutical supply disruptions before they reach patients.

> How to read this: every top-level section starts with `## §n`. Grep for it and read only what you need.
> Tags used below: **[REAL]** public data · **[PROXY]** derived estimate · **[SYNTH]** synthetic, SAP-shaped · **[VERIFY]** check against the primary source before quoting it on stage.

---

## §1 Problem, users and MVP scope

**One-line pitch.** For a medicine under a DPCO price ceiling, the first sign of a shortage is not an empty shelf. It is the input cost rising towards a price the manufacturer is not allowed to raise. Kairos watches that gap for every scheduled formulation a hospital depends on. It traces how concentrated the supply is, estimates when an exit becomes likely, proposes buffer stock or an alternate source that does not share the same weakness, and routes the proposal to the Chief Pharmacist for approval. Only then does it raise an SAP purchase requisition, and every step is audit-logged.

**Primary users** (validated in our interviews with the PSG Hospitals Director and the PSG Pharmacy Chief Pharmacist):
| User | Needs from Kairos | Where in the pharmacist's SAP SOP |
|---|---|---|
| Chief Pharmacist | Early warning on vital (V) and NLEM items; recall trace; approve buffer PRs | Demand identification §6, ROL/ROQ §7, PR §8–10, Recall §25, Dashboard §31–32 |
| Purchase Dept | A validated alternate source with a rate at or under the ceiling | Source determination §11, RFQ §12, PO §13 |
| Director / Finance | Budget impact, audit evidence (NABH) | Approval workflow §10/§14, 3-way match §29 |

**The MVP must demo, end to end:**
1. Real NPPA ceiling prices for a focused set of **30–40 NLEM formulations** (old antibiotics, injectables, respiratory, TB/anti-malarial, cardiac: low-price, high-risk). We can say the design scales to ~900 formulations, but we do not load them all.
2. The six agents running in sequence with persisted outputs in SAP HANA Cloud.
3. A watchlist UI plus a **human approval screen** in SAP Build Apps.
4. An approved action becomes an S/4HANA-shaped purchase requisition, with the audit trail shown.
5. A **backtest** on real NPPA Para-19 price-revision events. This is our evidence that the signal works (§4.5).
6. Recall trace (a CDSCO NSQ alert → affected batches across pharmacy locations) as the secondary operational signal the pharmacist asked for.

**Out of scope for the MVP:** real IoT cold chain (simulated), real S/4 posting (sandbox or mock), all ~900 formulations, counterfeit detection, transport rerouting.

---

## §2 Architecture

```
 EXTERNAL (public)                    SAP HANA Cloud (Hackfest-DB, schema = our user)
 NPPA ceiling prices [REAL] ─┐        ┌───────────────────────────────────────────────┐
 NLEM 2022 list      [REAL] ─┤ ingest │ FF_REF_*   regulatory + market reference      │
 TradeStat API imports[REAL]─┼──────▶ │ FF_MM_*    hospital materials/stock (S/4-shaped)│
 WPI index           [REAL] ─┤  (py)  │ FF_GRAPH   supply graph (HANA Graph workspace)│
 CDSCO NSQ alerts    [REAL] ─┤        │ FF_AG_*    agent outputs, approvals, audit    │
 Hospital pharmacy   [SYNTH]─┘        └──────────────────────┬────────────────────────┘
                                                             │ hdbcli / hana-ml
                       SAP BTP (Cloud Foundry, Python buildpack)
             ┌───────────────────────────────────────────────┴──────────────────┐
             │ FastAPI  ── orchestrator ── A1 → A2 → A3 → A4 → A5 ─┐             │
             │                                                     ▼             │
             │        A6 Audit (observer on every hop) ◀── HUMAN GATE ──▶ A6 act │
             │ explain.py (LLM writes the narrative only; numbers are checked)  │
             └───────────────┬───────────────────────────────────────┬──────────┘
                             │ REST (API key)                        │ OData-shaped
                  SAP Build Apps: Watchlist,           S/4HANA API_PURCHASEREQ_PROCESS_SRV
                  Molecule detail, Approval,           (API Hub sandbox read + FF_MM_EBAN mock write)
                  Audit viewer, Recall trace
```

Design principles:
- **Deterministic core.** Scores come from transparent rules. ML is used for forecasting consumption only. The LLM only narrates. (This resolves the deck's contradiction between "XGBoost risk detection" and "deterministic rules".)
- **SAP-native where it adds real value.** HANA holds everything. HANA Graph handles dependencies. HANA PAL does forecasting if the script server is enabled. Build Apps is the UI. S/4 API shapes are used for the action.
- **Offline-safe.** `DB_BACKEND=sqlite` with NetworkX and statsmodels fallbacks, plus a local UI5/HTML fallback page, in case SRM Wi-Fi or the shared HANA fails during the demo.

---

## §3 Agents (A1–A6) and the human gate

All agent inputs and outputs are pydantic models in `agents/contracts.py`. Each agent writes its output row(s) to HANA and emits an audit event through A6.

### A1: Margin Sentinel (Detect)
- **Question:** is making this formulation still viable under its ceiling price?
- **Inputs:** `FF_REF_CEILING_PRICE`, `FF_REF_API_COST_MONTHLY`, `FF_REF_WPI`, `FF_REF_BOM_ASSUMPTION`. Secondary: NSQ alerts, supplier on-time delivery (OTD) from `FF_MM_EKPO` / goods receipts, cold-chain excursions.
- **Logic** (§4.1): compute the estimated manufacturer realisation, the estimated unit cost, and the **headroom %**, plus a 6-month trend and months-to-breach.
- **Output:** `RiskSignal{formulation_id, headroom_pct, headroom_trend, months_to_breach, secondary_signals[], data_quality}` → `FF_AG_SIGNAL`.
- **Trigger:** a nightly batch over all tracked formulations, plus on demand for a **shock scenario** (e.g. "API import cost +30%", the Hormuz/tariff story).

### A2: Dependency Mapper (Assess)
- **Question:** if one producer exits, who is left, and do the remaining producers share one upstream point of failure?
- **Inputs:** `FF_REF_PRODUCER` (formulation → manufacturer), `FF_REF_API_ORIGIN` (API/KSM → import share by country, from TradeStat), `FF_MM_MARA` (which of our hospital materials map to the formulation), and consumption by department.
- **Logic:** traverse the HANA Graph workspace `FF_SUPPLY_GRAPH` (vertices: KSM, API, country, manufacturer, formulation, hospital material, ward). Compute: at-least-N producers, producer HHI, top-country API share, and the downstream set of materials and wards affected.
- **Output:** `DependencyProfile{n_producers_min, hhi, top_origin_country, top_origin_share, affected_materials[], affected_wards[]}` → `FF_AG_DEPENDENCY`.

### A3: Shortage Forecaster (Forecast + stated cause)
- **Question:** when could supply at our hospital fail, and why?
- **Inputs:** A1 and A2 outputs, and hospital stock and consumption (`FF_MM_MCHB`, `FF_MM_MSEG` goods issues).
- **Logic** (§4.3):
  1. **Exit-risk window**: from months-to-breach, adjusted by concentration. Also note the DPCO para 21(2) six-month discontinuation notice: our aim is to raise the flag *before* that notice would be filed.
  2. **Hospital days-of-cover**: usable stock (FEFO, non-expired, non-quarantined) ÷ forecast daily consumption (HANA PAL exponential smoothing, or statsmodels as fallback).
  3. Exposure = the gap between when an exit becomes likely and how long hospital cover plus supplier lead time lasts.
- **Output:** `Forecast{exit_risk_band, window_months_lo/hi, days_of_cover, stockout_date_if_exit, cause_code, cause_facts{}}`. `cause_code` is one of `CEILING_BELOW_COST`, `HEADROOM_ERODING`, `SINGLE_ORIGIN_API`, `FEW_PRODUCERS`, `QUALITY_NSQ`, `SUPPLIER_OTD_DROP`.

### A4: Resilience Simulator (Scenarios + correlated-risk check)
- **Question:** what is the cheapest action that keeps the hospital covered through the risk window without letting stock expire?
- **Options generated:** (a) buffer stock from the current supplier; (b) an alternate manufacturer of the same formulation; (c) a therapeutic alternative from NLEM/the formulary (flagged for clinical review); (d) inter-pharmacy rebalancing (Central → A/B/C Block → Oncology).
- **Correlated-risk check:** reject or penalise any alternate whose API shares the **same origin country or the same KSM node** in the graph. This prevents a "fake fix".
- **Optimisation** (OR-Tools CP-SAT or PuLP): choose quantity per option to minimise cost + stock-out penalty, subject to budget, storage (including cold-chain capacity), **expiry** (quantity ≤ consumption over remaining shelf life − safety margin), and MOQ.
- **Output:** a ranked list of `Scenario{option_type, supplier, qty, cost, coverage_days, expiry_waste_risk, correlated_risk_flag}` → `FF_AG_SCENARIO`.

### A5: Feasibility & Policy Validator (Rules engine; runs before the human sees anything)
Runs `rules/dpco.yaml` and `rules/sop_controls.yaml`. Each check returns PASS / WARN / FAIL with evidence.
| Check | Rule | Source |
|---|---|---|
| Price compliance | unit rate ≤ ceiling price + GST (ceiling excludes GST) | DPCO 2013 para 14 [VERIFY wording] |
| Scheduled-status | formulation is in Schedule I (NLEM 2022); ceiling S.O. number is on record | DPCO Sch. I |
| Supplier licence | vendor master has a valid drug licence number and GST; not blacklisted | SOP §5, CP-04 |
| Quality history | no NSQ alert for this manufacturer/formulation in the last 12 months (WARN if older) | CDSCO NSQ |
| Shelf life | offered residual shelf life ≥ hospital minimum (default 12 months, configurable) | SOP §13, CP-07 |
| Expiry waste | projected expired qty at end of horizon ≤ 5% | SOP §23 |
| Budget | PR value ≤ department budget remaining; value > threshold → second approver | SOP §10 |
| Storage | cold-chain items fit available fridge capacity | SOP §20, CP-08 |
| ROL/ROQ sanity | resulting stock ≤ max-stock × buffer factor | SOP §7, CP-02 |
| Clinical | a therapeutic substitute always needs a pharmacist/clinician sign-off | Formulary policy |
- **Output:** `Recommendation{scenario_id, checks[], overall=READY|NEEDS_CHANGES|BLOCKED, draft_pr{}}`. Only READY or NEEDS_CHANGES items reach the gate.

### 🧑‍⚕️ Human gate: manual verification (the "one approval gate")
- **Who:** the Chief Pharmacist (level 1). Above the ₹ threshold or for a therapeutic substitute, also Procurement / Director (level 2).
- **Screen (SAP Build Apps):** the cause statement, key facts with source labels (REAL/PROXY/SYNTH), the A2 graph snapshot, the chosen scenario and its alternatives, and the A5 check table.
- **Manual verification checklist.** The reviewer must tick every item, and each maps to the pharmacist's control points:
  - [ ] CP-01 material master matches (generic, strength, pack)
  - [ ] CP-02 ROL/ROQ and resulting stock level acceptable
  - [ ] CP-04 supplier/source verified (licence, past performance)
  - [ ] Price ≤ ceiling + GST confirmed against the NPPA notification
  - [ ] Shelf life / FEFO impact acceptable
  - [ ] Cause statement reviewed; this is a risk flag, not a claim about the manufacturer
- **Actions:** Approve · Edit quantity/supplier then approve · Reject (reason required) · Snooze (re-evaluate in N days).
- The decision is written to `FF_AG_APPROVAL` with user, timestamp and checklist state. Rejections become labels for tuning (`rules/weights.yaml`).

### A6: Compliance, Audit & Action
- **Observer role (always on):** every agent hop appends to `FF_AG_AUDIT_LOG` (run_id, agent, input hash, output hash, rule versions, data sources + fetched_at, `PREV_HASH` chain). The table is append-only.
- **Action role (only after approval):** builds the purchase requisition in the exact shape of **S/4HANA `API_PURCHASEREQ_PROCESS_SRV`** (`A_PurchaseRequisitionHeader` / `A_PurchaseReqnItem`: Material, Plant, Quantity, DeliveryDate, PurchasingGroup, FixedSupplier) and posts it to `FF_MM_EBAN` (mock). It also validates master data against the **SAP API Business Hub sandbox** (`API_PRODUCT_SRV`, `API_BUSINESS_PARTNER`) read-only.
- **Reports:** a per-decision audit PDF/HTML (NABH evidence: who, what, why, sources) and a "Form-IV watch" (formulations with elevated exit risk).
- **Blocks** any action without an approval row, and any hash-chain break.

---

## §4 Scoring rules (deterministic, explainable)

All constants live in `rules/weights.yaml` and `rules/dpco.yaml`, with their source noted.

### 4.1 Margin headroom (A1)
```
CP            = NPPA ceiling price per unit (excl. GST)                         [REAL]
PTR           = CP / (1 + retailer_margin)        retailer_margin = 0.16         (DPCO para 4 basis) [VERIFY]
realisation   = PTR / (1 + wholesaler_margin)     wholesaler_margin = 0.10 (assumption, configurable)
unit_cost     = api_cost_per_kg × api_g_per_unit / 1000 / yield
              + conversion_cost + packaging_cost + freight_cost                  [PROXY]
headroom_pct  = (realisation − unit_cost) / realisation
```
- The API cost per kg comes from the TradeStat import **unit value** (value ÷ quantity) for the API's HS 8-digit code, as a 3-month moving median [PROXY from REAL]. Conversion and packaging costs are indexed to WPI (manufactured products) [PROXY].
- **Trend:** OLS slope of headroom_pct over the last 6–12 months. `months_to_breach = headroom_pct / −slope` (only if slope < 0).
- **Signal bands:** `headroom < 5%` or `months_to_breach ≤ 6` → RED; `< 15%` or `≤ 12` → AMBER.

### 4.2 Concentration (A2)
`conc = 0.5·(1/n_producers_min) + 0.3·HHI_producers + 0.2·top_origin_share`, bounded to [0,1].

### 4.3 Exit risk and exposure (A3)
```
exit_risk = sigmoid( a·(−headroom_pct) + b·(1/months_to_breach) + c·conc + d·nsq_recent + e·otd_drop )
exposure  = exit_risk × criticality × clamp( (lead_time_days + requal_days) / max(days_of_cover,1), 0, 3 )
criticality: VED V=1.0, E=0.6, D=0.3; ×1.2 if NLEM core/primary level
```
- Start with hand-set coefficients. Tune them once, on the backtest (§4.5), and freeze them before the finale. Do not overfit.
- **Confidence** = share of inputs that are REAL rather than PROXY/SYNTH, times data freshness. It is shown next to every score.

### 4.4 Watchlist ranking
Sort by `exposure`, tie-break by `confidence`. Show the top 10 with cause chips.

### 4.5 Validation: the Para-19 backtest (our strongest slide)
NPPA has used DPCO para 19 (extraordinary circumstances) to **raise** ceiling prices of formulations that manufacturers reported as unviable. Those events are real "silent-disruption" labels.
- Positive set **[VERIFY exact list from the NPPA office memoranda]**: the Oct 2024 increase of ~50% for ~11 formulations of 8 drugs (e.g. benzylpenicillin inj., atropine inj., streptomycin inj., salbutamol tab/respirator solution, pilocarpine drops, cefadroxil tab, desferrioxamine inj., lithium carbonate tab), and the Dec 2019 increase for ~21 formulations (e.g. BCG vaccine, chloroquine, dapsone, metronidazole, vitamin C). Also include any 2025–26 para-19 orders you find.
- Negatives: randomly sampled scheduled formulations with no para-19 action in the same window.
- Method: replay A1–A3 month by month using only data available at that time (no look-ahead). Report the **median lead time** (first AMBER/RED → NPPA order date), **precision@10** and **recall**.
- Be honest: the cost side is a proxy, so present this as "directional evidence", not a validated model.

---

## §5 Data sources (what is real, what is not)

| Dataset | Source | Tag | Ingest script | Notes |
|---|---|---|---|---|
| Ceiling prices (scheduled formulations, S.O. no., date) | NPPA website: ceiling price compendium / notifications (nppaindia.nic.in) | REAL | `ingest/nppa.py` | xlsx/PDF → tabula/pdfplumber; store S.O. number + date |
| Annual WPI revision % (1 April each year) | NPPA office memoranda | REAL | `ingest/nppa.py` | [VERIFY] 2022 +10.76%, 2023 +12.12%, 2024 +0.0055%, 2025 +1.74% |
| Para-19 orders | NPPA office memoranda | REAL | `ingest/nppa_para19.py` | backtest labels |
| NLEM 2022 (384 medicines) | MoHFW / CDSCO | REAL | `ingest/nlem.py` | dosage form, strength, level of care |
| API import value & qty by HS8 × country × month | Ministry of Commerce TradeStat (tradestat.commerce.gov.in) | REAL → PROXY cost | `ingest/tradestat.py` | map API → HS8 by hand for the 30–40 targets; the origin share feeds A2 |
| WPI (manufactured products, chemicals) | Office of the Economic Adviser (eaindustry.nic.in) | REAL | `ingest/wpi.py` | cost indexation |
| NSQ / spurious drug alerts | CDSCO monthly drug alerts | REAL | `ingest/nsq.py` | manufacturer, batch, drug → recall trace + quality signal |
| Producers per formulation | NPPA price notifications (company names), CDSCO approvals, Jan Aushadhi / BPPI supplier lists | REAL (lower bound) | `ingest/producers.py` | always shown as "at least N" |
| BOM assumptions (API mg/unit, yield, conversion cost) | label strength + literature defaults | PROXY | `rules/bom_defaults.yaml` | documented per molecule |
| Hospital pharmacy: materials, vendors, stock by batch, goods movements, POs, PRs, cold-chain logs | generator shaped on the PSG Chief Pharmacist's SAP SOP | SYNTH | `ingest/synth_hospital.py` | seeded; 5 locations (Central, A, B, C Block, Oncology); 18 months |

Every table gets `SOURCE, SOURCE_URL, FETCHED_AT, IS_PROXY` columns. Raw downloads go in `data/raw/` (gitignored); cleaned seeds (small CSVs) go in `data/seed/` (committed).

---

## §6 SAP HANA Cloud data model (schema = our DB user; all objects `FF_`)

**Reference (external):**
- `FF_REF_FORMULATION(FORM_ID PK, GENERIC, DOSAGE_FORM, STRENGTH, UNIT, NLEM_LEVEL, THERAPEUTIC_CLASS)`
- `FF_REF_CEILING_PRICE(FORM_ID, EFFECTIVE_FROM, CEILING_PRICE, SO_NUMBER, PARA {4|16|19}, …provenance)`
- `FF_REF_API(API_ID, NAME, HS8, KSM_ID)`, `FF_REF_FORM_API(FORM_ID, API_ID, API_MG_PER_UNIT)`
- `FF_REF_API_COST_MONTHLY(API_ID, MONTH, UNIT_VALUE_INR_KG, QTY_KG, IS_PROXY)`
- `FF_REF_API_ORIGIN(API_ID, MONTH, COUNTRY, SHARE)`
- `FF_REF_PRODUCER(FORM_ID, MANUFACTURER_ID, EVIDENCE_SOURCE)`, `FF_REF_MANUFACTURER(ID, NAME, STATE, LICENCE_NO)`
- `FF_REF_WPI(MONTH, SERIES, INDEX_VALUE)`, `FF_REF_NSQ_ALERT(MONTH, DRUG, BATCH, MANUFACTURER_ID, REASON)`

**Hospital MM (S/4-shaped, SYNTH). Column names mirror the standard tables so a real S/4 swap is mechanical:**
- `FF_MM_MARA` (MATNR, generic, strength, VED, ABC, FSN, storage condition, batch-managed flag, FORM_ID link)
- `FF_MM_LFA1` (LIFNR, name, GSTIN, drug licence no, blacklist flag) · `FF_MM_T001W` (plants/locations)
- `FF_MM_MCHB` (MATNR, WERKS, LGORT, CHARG, CLABS qty, VFDAT expiry, status unrestricted/quarantine)
- `FF_MM_MSEG` (movement types 101 GR, 261/201 GI to ward, 311 transfer, 122 return, 551 scrap; date, qty, batch)
- `FF_MM_EKKO/EKPO` (POs incl. delivery date vs actual → OTD) · `FF_MM_EBAN` (PRs; A6 writes here)
- `FF_MM_COLDCHAIN(LOCATION, TS, TEMP_C, EXCURSION_FLAG)`

**Agent layer:** `FF_AG_RUN`, `FF_AG_SIGNAL`, `FF_AG_DEPENDENCY`, `FF_AG_FORECAST`, `FF_AG_SCENARIO`, `FF_AG_RECOMMENDATION`, `FF_AG_CHECK`, `FF_AG_APPROVAL`, `FF_AG_AUDIT_LOG` (append-only, `PREV_HASH`, `ROW_HASH`).

**Views** (`db/views.sql`): `FF_V_WATCHLIST`, `FF_V_DAYS_OF_COVER` (FEFO usable stock ÷ avg daily issue), `FF_V_SUPPLIER_OTD`, `FF_V_RECALL_TRACE` (NSQ batch ↔ MCHB across locations).

**Graph** (`db/graph.sql`): vertex table `FF_G_V(ID, TYPE, LABEL)` and edge table `FF_G_E(ID, SRC, DST, REL)`, then `CREATE GRAPH WORKSPACE FF_SUPPLY_GRAPH …`. Test on day 1 whether the shared instance allows it; if not, use the NetworkX fallback over the same tables.

---

## §7 SAP tools: what we use and how (access given by the organisers + trials)

| SAP asset | Access | Use in Kairos | Day-1 check |
|---|---|---|---|
| **SAP HANA Cloud** (Hackfest-DB, eu10, user `HACKFEST0xxx`) | organiser landscape; shared password | System of record for all tables, views, audit; Graph workspace; PAL forecasting via `hana-ml` | Connect with `hdbcli` from a laptop (port 443, TLS). Check `CREATE TABLE` rights, `CREATE GRAPH WORKSPACE`, and `SELECT * FROM SYS.AFL_AREAS` (PAL) |
| **SAP HANA Database Explorer** | via HANA Cloud Central → Open | Run DDL, inspect data, show the SQL console to judges | — |
| **SAP BTP trial** (Cloud Foundry) | self-registered trial | Host the FastAPI orchestrator (`cf push`, Python buildpack); user-provided service holding HANA creds | Region (us10/ap21) and memory quota; outbound to HANA eu10 allowed |
| **SAP Build Apps** | BTP trial (SAP Build booster) | Watchlist, molecule detail, approval checklist, audit viewer, recall trace; calls FastAPI via a BTP destination | Build a hello-world that calls a public REST endpoint |
| **SAP Build Process Automation** | BTP trial | *Stretch:* the approval as a formal workflow with a 2-level approver. The MVP does approval inside Build Apps → FastAPI | Only if time allows |
| **SAP API Business Hub sandbox** | api.sap.com (free API key) | Read-only master-data validation (`API_PRODUCT_SRV`, `API_BUSINESS_PARTNER`); PR payload shaped on `API_PURCHASEREQ_PROCESS_SRV` | GET `A_Product?$top=1` with the APIKey header |
| **SAP Business Data Cloud trial** | self-registered | Not in the MVP. Mention as the production path for combining S/4 + external data products | — |
| **Joule / SAP Generative AI Hub** | not available to us | Say "designed to plug into Generative AI Hub / Joule". The MVP uses `LLM_PROVIDER=anthropic` or template-only | Don't claim Joule in the demo |

**Environment variables** (keep in a gitignored `.env` file; never commit or paste them): `DB_BACKEND (hana|sqlite)`, `HANA_HOST`, `HANA_PORT=443`, `HANA_USER`, `HANA_PASSWORD`, `HANA_SCHEMA`, `SAP_API_HUB_KEY`, `LLM_PROVIDER (anthropic|sap_genai_hub|none)`, `LLM_MODEL`, `ANTHROPIC_API_KEY`, `APP_API_KEY` (header shared by Build Apps → FastAPI).

**Shared-instance safety:** all participants share one password, so anyone can log in as our user. Keep every load idempotent (`python -m ingest.load_hana --reset` rebuilds the schema in < 5 min), take a nightly `data/seed` export, and have the sqlite fallback ready.

---

## §8 Repository layout
```
CLAUDE.md                      .claude/skills/kairos/SKILL.md   .claude/settings.json
docs/ IMPLEMENTATION_PLAN.md  STATUS.md  DECISIONS.md  demo_script.md
db/ schema.sql  views.sql  graph.sql  seed_small.sql
ingest/ nppa.py nppa_para19.py nlem.py tradestat.py wpi.py nsq.py producers.py synth_hospital.py load_hana.py
rules/ dpco.yaml  sop_controls.yaml  weights.yaml  bom_defaults.yaml
agents/ contracts.py  ctx.py  a1_margin_sentinel.py  a2_dependency.py  a3_forecast.py
        a4_resilience.py  a5_validator.py  a6_audit.py  explain.py  orchestrator.py
api/ main.py  (routes: /watchlist /molecule/{id} /run /scenario/shock /approval /audit/{run} /recall/{alert})
ui/ buildapps/ (exported project)   fallback/ index.html (UI5 CDN or plain JS)
tests/ test_rules.py  test_a1.py  test_audit_chain.py  backtest_para19.py
scripts/ git-hooks/commit-msg   deploy_cf.sh   export_seed.py
manifest.yml  requirements.txt  .gitignore
```

---

## §9 Team split and timeline

**Roles (3 builders + 2 on story/deck).** Fill in names in `docs/STATUS.md`.
| Role | Owns | Hand-off contract |
|---|---|---|
| **B1: Data & HANA** | ingest/*, db/*, HANA Graph, synthetic hospital data, recall view | tables populated + `FF_V_*` views by end of Day 1 |
| **B2: Agents & rules** | agents/a1–a5, rules/*.yaml, forecasting, OR-Tools, backtest | `orchestrator --dry-run` green on 5 molecules by end of Day 2 |
| **B3: Platform, UI & action** | FastAPI, BTP CF deploy, Build Apps screens, approval, A6 audit/action, API Hub | Build Apps calling live API by end of Day 2 |
| **P1: Story & deck** | deck rewrite (§12), citations check, architecture slide from §2 | deck v2 by Day 2 evening |
| **P2: Demo & validation** | demo script (§10), backtest numbers into slides, backup video, Q&A sheet, pharmacist quote/photos consent | full dry-run recording by Day 3 |

**Timeline** (D0 = finale day; set the date in STATUS.md):
| Day | Builders | Deck pair | Exit criterion |
|---|---|---|---|
| **D-4** | B1: HANA connect + DDL + NPPA/NLEM ingest for 30–40 targets. B2: contracts.py, A1 on seed CSV. B3: FastAPI skeleton, CF trial push, Build Apps hello-world | Freeze story; start deck v2; request the pharmacist's permission to quote | HANA reachable from laptop **and** CF; A1 numbers for 5 molecules |
| **D-3** | B1: TradeStat + WPI + NSQ + producers; synth hospital; graph. B2: A2, A3, A5 rules. B3: watchlist + detail screens, audit chain | Architecture + agent slides; KPI slide placeholder | All 5 agents dry-run; watchlist live on Build Apps |
| **D-2** | B1: recall view, seed export, sqlite fallback. B2: A4 optimiser + correlated check; backtest. B3: approval screen + checklist, A6 PR mock + API Hub validation, explain.py | Insert backtest results; demo script v1 | Full end-to-end: shock → watchlist → approve → PR + audit |
| **D-1** | All: bug bash, freeze rules/weights, rehearse 3×, record backup video, test on a phone hotspot | Final deck; Q&A sheet; print one-page leave-behind | Demo < 6 min, 2 clean runs in a row |
| **D0** | Pre-flight: HANA up, CF app awake, `--reset` tested, sqlite ready | Present | — |

**Cut list if behind** (drop in this order): Build Process Automation → therapeutic-alternative option → HANA PAL (use statsmodels) → HANA Graph (use NetworkX) → API Hub validation. **Never cut:** A1 on real NPPA data, the human gate, the audit trail, the backtest slide.

---

## §10 Finale demo script (≈6 minutes)
1. **Hook (30s):** a pharmacy shelf photo from our PSG visit. "The first sign of a shortage isn't a shortage."
2. **Mechanism (45s):** the ceiling-vs-cost chart for one real formulation: a fixed NPPA ceiling, and API import cost (TradeStat) rising towards it.
3. **Live run (2m):** press *Shock: API import cost +30%* (the Hormuz/freight/tariff story from the theme brief). A1 moves 4 molecules to RED. Open one: A2 shows "at least 3 producers, 72% of API from one country". A3: "exit-risk window 4–7 months; hospital cover 21 days; cause: ceiling below estimated cost".
4. **Fake-fix catch (30s):** A4's cheapest alternate is rejected because it shares the same API origin; the next-best is chosen with buffer quantity sized to avoid expiry waste.
5. **Human gate (45s):** the Chief Pharmacist view. A5 shows all checks green (price ≤ ceiling + GST, licence, shelf life). Tick the checklist → Approve.
6. **Action + audit (30s):** the S/4-shaped PR appears; the audit chain shows every hop with sources and hashes (NABH evidence).
7. **Recall bonus (20s):** a CDSCO NSQ alert → the affected batch is located in 3 pharmacy locations in one click (the pharmacist's CP-14).
8. **Proof (30s):** the backtest slide: median X months of lead time before real NPPA para-19 revisions. Close on scalability and the B2B model.

---

## §11 Risks and mitigations
| Risk | Mitigation |
|---|---|
| Shared HANA password; another team drops or edits our tables | idempotent `--reset`, seed export, sqlite fallback, verify row counts pre-demo |
| HANA not reachable from BTP CF (allowlist) or eu10 latency | test on D-4; if blocked, run FastAPI locally and point Build Apps at a tunnel, or use the fallback UI |
| NPPA data in PDF, messy | restrict to 30–40 targets; hand-verify each row; store S.O. number as proof |
| TradeStat HS8 ↔ API mapping ambiguous | pick APIs with a dedicated HS8 line; label PROXY; show confidence |
| Judges question the proxy cost model | show the formula, assumptions file, sensitivity (±20%), and backtest honesty |
| Build Apps learning curve | B3 starts D-4; fallback HTML/UI5 page on the same API |
| Venue Wi-Fi | phone hotspot, sqlite + local LLM-off mode (`LLM_PROVIDER=none`), backup video |
| Over-claiming | never "Joule-powered", never "predicts manufacturer intent", never "real S/4 posting" |

---

## §12 Deck fixes for the PPT pair (current PDF → v2)
1. **Slide 2:** the five "why" questions (transport, overstock, cold chain, recall, counterfeit) dilute the thesis. Keep one line, and lead with the margin-squeeze mechanism. **Verify the Reuters "June 12, 2026" cancer-drug headline**; if it can't be sourced, remove it.
2. **Slide 3 (ground validation):** add who was met (PSG Hospitals Director, PSG Pharmacy Chief Pharmacist) and 2–3 concrete insights (the SAP pharmacy SOP, recall trace, FEFO/expiry, control points). Get consent before showing faces.
3. **Slide 4:** good. Show the headroom formula under the chart.
4. **Slide 5 (workflow):** rename the agents to match §3 (A1 Margin Sentinel … A6 Compliance/Audit/Action). Move the **human gate before A6's action**, and show A6 as audit-on-every-hop.
5. **Slide 6:** replace **DSCSA** (US law) with Indian references: DPCO 2013 paras 14/16/19/21, CDSCO NSQ alerts, the QR-code/track-and-trace mandate for APIs & top brands [VERIFY], and revised Schedule M GMP [VERIFY].
6. **Slide 8:** remove the duplicated blocks. Replace "Joule" with "GenAI explanation layer (Generative AI Hub-ready)". Remove XGBoost/Random-Forest risk detection (scores are rule-based); keep forecasting as PAL/statsmodels. Show the SAP tools table from §7.
7. **Slide 9:** replace "907–928" with one sourced number [VERIFY]. Add the backtest KPIs (lead time, precision@10).
8. **Slide 10:** replace the generic organiser roadmap with our own §9 timeline.
9. **Slide 11:** "No real-user validation yet" is now outdated: we met the Chief Pharmacist and the Director. Change it to "Validated with PSG Pharmacy; pilot on their SAP data proposed".
10. **Slide 12 (related work):** delete the leftover assistant notes: "(Read the paper before quoting specifics.)" and "(Figures as cited earlier in your deck.)". Verify every citation (arXiv 2601.09680, Pall et al. 2023, Frontiers 2025, Liu et al. AJHP 2021, USP report 2026) before the finale. Drop any that can't be verified.
11. **Throughout:** tag every number REAL / PROXY / SYNTH, the same way the product does.

---

## §13 Copy-paste stage prompts (build workflow)

### 13.0 How to use these prompts
**Flow for every stage:** open Claude Code in `FlowForge_SAP_Hackfest/` → paste the whole prompt block for your stage → let it build and verify → it opens a PR → Bhavesh reviews (13.R) and merges → the next stage starts from the updated `main`.

**Rules**
- Take one stage at a time. Don't start a stage until everything in its "Needs" column is merged into `main`.
- Each stage owns the files it lists. If you need a change in another stage's file, say so in the PR description instead of editing it.
- Agent stages build and test against the small fixtures in `tests/fixtures/` using SQLite, so they don't wait for HANA or real data.
- If Claude says it needs a human step (download a file, fill `.env`, click in the SAP UI), do that step and reply "done".

**Standard Git steps.** Every prompt ends with "Finish with §13.0 Git steps". Claude follows these:
1. At the start: `git checkout main && git pull`, then `git checkout -b feat/<your-first-name>-<stage-id>` (e.g. `feat/gopika-s04`).
2. At the end, the stage's **Verify** commands must all pass. If they don't, fix the problem or stop and report; don't open a PR with failing checks.
3. Update only your stage's row in `docs/STATUS.md` (set it to `PR open`).
4. `git add` only the stage's files plus `docs/STATUS.md`. Commit with a Conventional Commit message (`feat(a1): …`) and **no AI attribution lines**.
5. `git push -u origin HEAD`.
6. If `gh` is installed and logged in: `gh pr create --base main --title "<STAGE-ID>: <summary>"` with a body that fills in `.github/pull_request_template.md`. Otherwise print `https://github.com/Bhavesh-Reddy/FlowForge_SAP_Hackfest/compare/main...<branch>?expand=1` so the human can open it.
7. Stop. Never merge, and never push to `main`.

### 13.1 Stage map
| ID | Stage | Owner | Needs | Target day |
|---|---|---|---|---|
| S00 | Bootstrap: contracts, config, DB wrapper, rules, CI | B2 | — | D-4 (merge first!) |
| S01 | HANA schema, loader, Day-1 checks | B1 | S00 | D-4 |
| S02 | NPPA + NLEM ingest, target list, Para-19 events | B1 | S01 | D-4/D-3 |
| S03 | Cost, origin, NSQ, producers, synthetic hospital | B1 | S02 | D-3 |
| S04 | A1 Margin Sentinel | B2 | S00 | D-4/D-3 |
| S05 | A2 Dependency Mapper + graph | B2 | S00 | D-3 |
| S06 | A3 Shortage Forecaster | B2 | S00 | D-3 |
| S07 | A4 Resilience Simulator + A5 Validator | B2 | S00 | D-2 |
| S08 | A6 Audit/Action + orchestrator + human gate | B3 | S00 | D-3 |
| S09 | FastAPI + BTP Cloud Foundry deploy | B3 | S08 | D-3/D-2 |
| S10 | SAP Build Apps guide + fallback UI | B3 | S09 | D-2 |
| S11 | LLM explanation with number check | B3 | S08 | D-2 |
| S12 | Para-19 backtest + results for the deck | B2 + P2 | S02–S07 | D-2 |
| S13 | Integration on real data + demo hardening | Bhavesh + all | all | D-1 |

B1, B2 and B3 can run in parallel as soon as S00 is merged.

---

### S00: Bootstrap (B2). Merge this first.
```text
Load the kairos skill. Read docs/STATUS.md, then only plan §2, §3, §4, §7 and §8 of docs/IMPLEMENTATION_PLAN.md.

Stage S00 (Bootstrap). Build the shared foundation every other stage imports. Do NOT implement any agent logic.

Create:
1. requirements.txt: fastapi, uvicorn, pydantic>=2, python-dotenv, pyyaml, pandas, numpy, hdbcli, hana-ml, networkx, ortools, statsmodels, pdfplumber, openpyxl, requests, anthropic, pytest, httpx.
2. config/env.sample: every env var in plan §7, with a one-line comment each and empty values. (Don't create or read .env.)
3. agents/ctx.py: load settings from env (python-dotenv). get_db() returns a small wrapper with query(sql, params) -> pandas.DataFrame, execute(sql, params) and executemany(sql, rows). It works for DB_BACKEND=hana (hdbcli, TLS, port 443, default schema = HANA_SCHEMA) and DB_BACKEND=sqlite (file data/kairos.sqlite). Use "?" placeholders for both. Never log credentials.
4. agents/contracts.py: pydantic v2 models for every input and output in plan §3: RiskSignal, DependencyProfile, Forecast, Scenario, CheckResult, Recommendation, DraftPR, ApprovalDecision (with checklist fields from the human-gate section), AuditEvent, RunContext. Enums: SignalBand (GREEN/AMBER/RED), CauseCode (the six codes in §3 A3), DataTag (REAL/PROXY/SYNTH), CheckStatus (PASS/WARN/FAIL), Overall (READY/NEEDS_CHANGES/BLOCKED), OptionType (BUFFER/ALT_SUPPLIER/THERAPEUTIC_ALT/REBALANCE). Add docstrings that point to the plan section.
5. rules/weights.yaml, rules/dpco.yaml, rules/sop_controls.yaml: every constant from §4 and every check in the §3 A5 table. Store each as {value, source, verify: true|false}. agents/rules.py loads and validates them.
6. tests/fixtures/: a tiny CSV set (3 formulations, 2 APIs, 4 producers, 12 months of API cost, 1 hospital material with stock and issues) so later stages can test without HANA. Tag the rows SYNTH.
7. tests/test_contracts.py and tests/test_rules.py.
8. .github/workflows/tests.yml: run pytest -q on push and pull_request (Python 3.11, DB_BACKEND=sqlite).
9. Empty packages with __init__.py for agents/, ingest/ and api/.

Verify: pip install -r requirements.txt && pytest -q (all green).
Finish with §13.0 Git steps (stage id s00).
```

### S01: HANA schema, loader, Day-1 checks (B1)
```text
Load the kairos skill. Read docs/STATUS.md, then only plan §6, §7 and §11. Read agents/ctx.py and agents/contracts.py.

Stage S01. Create the database layer for both HANA and SQLite.

Create:
1. db/schema.sql: every table in §6 with the FF_ prefix. Every reference/MM table gets the provenance columns SOURCE, SOURCE_URL, FETCHED_AT, IS_PROXY. FF_AG_AUDIT_LOG has PREV_HASH and ROW_HASH. Use HANA column-table syntax. If SQLite needs different types, generate db/schema_sqlite.sql from the same definitions (a script, not hand-copying).
2. db/views.sql: FF_V_WATCHLIST, FF_V_DAYS_OF_COVER (FEFO usable, non-expired, unrestricted stock ÷ average daily goods issue over 90 days), FF_V_SUPPLIER_OTD, FF_V_RECALL_TRACE. Keep them portable to SQLite where possible.
3. ingest/load_hana.py: CLI with --backend hana|sqlite, --reset (drop and recreate ONLY FF_ objects in our own schema), and --seed (load CSVs from data/seed/ and tests/fixtures/ if present). Idempotent.
4. scripts/day1_checks.py: connects with env creds and prints a PASS/FAIL table for: connect; create+drop a table; CREATE GRAPH WORKSPACE on two tiny temp tables (then drop); PAL available (SELECT from SYS.AFL_AREAS); row count of FF_ tables. Never print host, user or password.
5. tests/test_schema_sqlite.py: the schema and views create on SQLite and the fixtures load.

Human step: ask me to fill .env (from config/env.sample) and run `python scripts/day1_checks.py` myself, then paste the output. Don't read .env. Write the results into the "Day-1 check results" line of docs/STATUS.md.

Verify: pytest -q && python -m ingest.load_hana --backend sqlite --reset --seed
Finish with §13.0 Git steps (stage id s01).
```

### S02: NPPA + NLEM ingest, target list, Para-19 events (B1)
```text
Load the kairos skill. Read docs/STATUS.md, then only plan §1, §4.5 and §5. Read db/schema.sql (only the FF_REF_ tables).

Stage S02. Load REAL regulatory data. Never type a price or date by hand as if it were REAL. Every REAL row must come from a downloaded source file, with SOURCE_URL.

Human step first: check data/raw/ for (a) the NPPA ceiling-price list (xlsx or pdf) and (b) the NLEM 2022 list. If either is missing, stop and tell me the exact page to download from and the filename to save it as. Wait for me to reply "done".

Create:
1. ingest/nppa.py: parse the ceiling-price file into FF_REF_FORMULATION and FF_REF_CEILING_PRICE (generic, dosage form, strength, unit, ceiling price excl. GST, S.O. number, notification date, PARA). Normalise names (case, spacing, "Tablet"/"Tab.").
2. ingest/nlem.py: parse NLEM 2022 into the formulation's NLEM_LEVEL and therapeutic class.
3. data/seed/targets.csv: 30–40 formulations chosen by the §1 criteria (old antibiotics, injectables, respiratory, TB/anti-malarial, cardiac; low price). Include every Para-19 molecule from §4.5 that exists in the parsed data. Pick only rows that actually exist in the parsed data, and add a "why chosen" column.
4. ingest/nppa_para19.py + data/seed/para19_events.csv (form_id, order_date, % change, SOURCE_URL, verify flag). If the memorandum PDFs are in data/raw/, parse them. Otherwise create the CSV with only the molecule names from §4.5, empty dates, and verify=true, and list for me what must be confirmed.
5. tests/test_nppa_parser.py on a 5-row fixture copied from the real file.

Verify: pytest -q && python -m ingest.nppa --backend sqlite && print row counts plus 5 sample target rows.
Finish with §13.0 Git steps (stage id s02).
```

### S03: Cost, origin, quality, producers, synthetic hospital (B1)
```text
Load the kairos skill. Read docs/STATUS.md, then only plan §4.1, §5 and §6. Read data/seed/targets.csv (head only) and db/schema.sql (only the tables you load).

Stage S03. Load the cost/origin/quality/producer data and generate the synthetic hospital data.

Human step first: check data/raw/ for the TradeStat import exports (HS8 × country × month for the target APIs), the WPI series, and the CDSCO NSQ monthly alert files. List anything missing with the exact source page (§5), and wait for "done". If TradeStat can't be downloaded in time, say so; we will then mark cost as SYNTH, not PROXY.

Create:
1. data/seed/api_hs8.csv: target formulation → API → HS8 code, with a mapping-confidence note. rules/bom_defaults.yaml: API mg per unit (from label strength), yield, conversion, packaging and freight cost, each with a source note.
2. ingest/tradestat.py → FF_REF_API_COST_MONTHLY (unit value INR/kg, 3-month rolling median, IS_PROXY=1) and FF_REF_API_ORIGIN (country share per month).
3. ingest/wpi.py → FF_REF_WPI. ingest/nsq.py → FF_REF_NSQ_ALERT. ingest/producers.py → FF_REF_PRODUCER and FF_REF_MANUFACTURER (lower bound; store the evidence source per row).
4. ingest/synth_hospital.py (seeded, SYNTH): 5 locations (Central, A Block, B Block, C Block, Oncology); FF_MM_MARA for the targets with VED/ABC/FSN; 18 months of FF_MM_MSEG (101/261/311/122/551); FF_MM_MCHB batches with expiry (some near-expiry); FF_MM_LFA1 vendors with licence/GST; FF_MM_EKKO/EKPO with some late deliveries; FF_MM_COLDCHAIN with 2 excursions. Plant exactly one batch that matches a real NSQ alert so the recall demo works. Base the shapes on the pharmacist's SOP (§6 notes).
5. scripts/export_seed.py: dump FF_ tables to data/seed/*.csv (small ones only) for backup/restore.
6. Tests for the unit-value calculation and generator determinism.

Verify: pytest -q && python -m ingest.load_hana --backend sqlite --reset --seed && print row counts per table.
Finish with §13.0 Git steps (stage id s03).
```

### S04: A1 Margin Sentinel (B2)
```text
Load the kairos skill. Read docs/STATUS.md, then only plan §3 (A1) and §4.1. Read agents/contracts.py, agents/ctx.py and rules/weights.yaml.

Stage S04. Implement agents/a1_margin_sentinel.py.
- run(form_ids, ctx, api_cost_multiplier=1.0) -> list[RiskSignal]. Compute realisation, unit_cost, headroom_pct, 6–12-month OLS trend, months_to_breach and the band, exactly as in §4.1, with all constants from rules/*.yaml.
- Secondary signals: NSQ alert in the last 12 months, supplier OTD drop (FF_V_SUPPLIER_OTD), cold-chain excursions. List them in secondary_signals with their source.
- data_quality = share of REAL inputs (use the IS_PROXY/tag columns).
- Write the results to FF_AG_SIGNAL with the run_id. Emit nothing to the audit log yourself; return the objects (the orchestrator audits them).
- CLI: python -m agents.a1_margin_sentinel --form <id> [--shock 1.3] prints a compact table.
- tests/test_a1.py: hand-compute the headroom for one fixture formulation and assert it to 4 decimals; check that the shock multiplier moves the band; check that missing cost data gives a low data_quality, not a crash.

Verify: pytest -q && python -m agents.a1_margin_sentinel --form <a fixture id> --shock 1.3
Finish with §13.0 Git steps (stage id s04).
```

### S05: A2 Dependency Mapper + graph (B2)
```text
Load the kairos skill. Read docs/STATUS.md (including the Day-1 Graph result), then only plan §3 (A2), §4.2 and the Graph paragraph of §6. Read agents/contracts.py.

Stage S05.
1. ingest/build_graph.py: build FF_G_V (KSM, API, COUNTRY, MANUFACTURER, FORMULATION, MATERIAL, WARD) and FF_G_E from the reference and MM tables.
2. db/graph.sql: CREATE GRAPH WORKSPACE FF_SUPPLY_GRAPH over FF_G_V/FF_G_E (HANA only).
3. agents/a2_dependency.py: run(form_ids, ctx) -> list[DependencyProfile]. If backend=hana and the Day-1 check says Graph works, query the workspace (neighbourhood/paths). Otherwise use NetworkX over the same tables. Both paths must return identical results on the fixtures. Compute n_producers_min, producer HHI, top origin country and share, affected materials and wards, and the concentration score from §4.2. Write to FF_AG_DEPENDENCY.
4. A helper shares_upstream(form_a, form_b or supplier) that returns shared origin country or KSM nodes. A4 will use it for the correlated-risk check.
5. tests/test_a2.py, including a case where two producers share one API origin.

Verify: pytest -q
Finish with §13.0 Git steps (stage id s05).
```

### S06: A3 Shortage Forecaster (B2)
```text
Load the kairos skill. Read docs/STATUS.md (including the Day-1 PAL result), then only plan §3 (A3) and §4.3. Read agents/contracts.py.

Stage S06. Implement agents/a3_forecast.py.
- Consumption forecast per hospital material: HANA PAL exponential smoothing via hana-ml if backend=hana and PAL is available; otherwise statsmodels ETS; if there are fewer than 6 months of history, use a 90-day moving average. Record which method was used.
- days_of_cover from FF_V_DAYS_OF_COVER (FEFO usable stock only).
- exit_risk and exposure exactly as in §4.3, with coefficients from rules/weights.yaml. Exit-risk window (lo/hi months) from months_to_breach, adjusted by concentration.
- cause_code: pick the dominant contributor and fill cause_facts with the exact numbers used (headroom, ceiling, estimated cost, n producers, origin share, days of cover), each with its DataTag. The LLM will narrate from these facts later, so they must be complete.
- Write to FF_AG_FORECAST.
- tests/test_a3.py: known inputs give a known exposure ordering; a molecule with fewer producers ranks higher, all else equal.

Verify: pytest -q
Finish with §13.0 Git steps (stage id s06).
```

### S07: A4 Resilience Simulator + A5 Validator (B2)
```text
Load the kairos skill. Read docs/STATUS.md, then only plan §3 (A4, A5 and the human gate). Read agents/contracts.py, rules/dpco.yaml and rules/sop_controls.yaml.

Stage S07.
1. agents/a4_resilience.py: generate the options BUFFER, ALT_SUPPLIER, THERAPEUTIC_ALT (always flagged for clinical review) and REBALANCE (between the 5 locations). Size quantities with OR-Tools CP-SAT (or PuLP) to minimise cost + stock-out penalty, subject to budget, storage/cold-chain capacity, expiry (qty ≤ forecast consumption over the remaining shelf life − margin) and MOQ. Correlated-risk check: use a2_dependency.shares_upstream to reject or penalise an alternate that shares the API origin/KSM, and set correlated_risk_flag with the reason. Return ranked Scenarios and write them to FF_AG_SCENARIO.
2. agents/a5_validator.py: run every check in the §3 A5 table from the YAML rules. Each returns PASS/WARN/FAIL with evidence text and the rule source. Price rule: unit rate must be ≤ ceiling price + GST (the ceiling excludes GST). Produce a Recommendation with overall READY/NEEDS_CHANGES/BLOCKED and a DraftPR (material, plant, qty, delivery date, fixed supplier, rate). Write to FF_AG_RECOMMENDATION and FF_AG_CHECK.
3. Tests: a price above ceiling+GST is BLOCKED; a correlated alternate is rejected and the next-best chosen; a buffer never exceeds the expiry-safe quantity; a therapeutic alternative always needs a second approver.

Verify: pytest -q
Finish with §13.0 Git steps (stage id s07).
```

### S08: A6 Audit/Action + orchestrator + human gate (B3)
```text
Load the kairos skill. Read docs/STATUS.md, then only plan §3 (A6 and the human gate) and §7 (API Hub row). Read agents/contracts.py and agents/ctx.py.

Stage S08. If A1–A5 aren't merged yet, write minimal stubs that return valid contract objects from the fixtures. Put them in agents/_stubs.py; the orchestrator picks the real module when it exists.
1. agents/a6_audit.py:
   (a) audit(event): append to FF_AG_AUDIT_LOG with input/output hashes, agent, rule-file versions (hash of the YAML), data sources + fetched_at, PREV_HASH and ROW_HASH (sha256). Append-only.
   (b) verify_chain(run_id) -> ok/broken-at.
   (c) act(recommendation_id): refuse unless FF_AG_APPROVAL has an APPROVED row with every checklist item ticked. Build the PR payload in the exact shape of S/4HANA API_PURCHASEREQ_PROCESS_SRV (A_PurchaseRequisitionHeader with to_PurchaseReqnItem: Material, Plant, RequestedQuantity, BaseUnit, DeliveryDate, PurchasingGroup, FixedSupplier). Insert it into FF_MM_EBAN and store the JSON payload.
   (d) Optional master-data check against the SAP API Business Hub sandbox (API_PRODUCT_SRV, API_BUSINESS_PARTNER; GET only, header APIKey from SAP_API_HUB_KEY). Skip with a WARN if there's no key or no network.
2. agents/orchestrator.py: run(form_ids, shock=1.0) runs A1→A5, audits every hop, stores FF_AG_RUN, and stops at the human gate (status AWAITING_APPROVAL). decide(rec_id, user, decision, checklist, edits, reason) writes FF_AG_APPROVAL (a reason is required on reject; a second approver is needed above the ₹ threshold or for a therapeutic alternative), then calls a6.act on approve. CLI: --molecule <id> --shock 1.3 --dry-run.
3. agents/report.py: per-decision audit report as HTML (who/what/why/sources/hashes) for NABH evidence.
4. Tests: the chain verifies; tampering with one row is detected; act() without an approval is refused; a rejection without a reason is refused.

Verify: pytest -q && python -m agents.orchestrator --molecule <fixture id> --shock 1.3 --dry-run
Finish with §13.0 Git steps (stage id s08).
```

### S09: FastAPI + BTP Cloud Foundry deploy (B3)
```text
Load the kairos skill. Read docs/STATUS.md, then only plan §2, §7 and §8. Read agents/orchestrator.py (signatures only).

Stage S09.
1. api/main.py (FastAPI): GET /health, GET /watchlist, GET /molecule/{id} (signal + dependency + forecast + scenarios + checks + graph snapshot as nodes/edges + data tags), POST /run {form_ids, shock}, POST /scenario/shock {multiplier}, GET /approvals/pending, POST /approval {rec_id, decision, checklist, edits, reason, user}, GET /audit/{run_id} (+ chain status), GET /audit/{run_id}/report (HTML), GET /recall/{alert_id}. Every route except /health requires the X-API-Key header = APP_API_KEY. Enable CORS for SAP Build Apps origins. Response models come from contracts.py.
2. manifest.yml (Python buildpack, 512M, health check /health), runtime.txt, Procfile. The app reads HANA creds from a user-provided service (VCAP_SERVICES) or from env.
3. docs/deploy.md: exact steps for cf login (trial region), cf create-user-provided-service ff-hana -p '{...}' (the human types the values), cf push, and how to test from a browser. Also a local run: uvicorn api.main:app.
4. tests/test_api.py with httpx TestClient on SQLite: auth required, the watchlist returns rows, approve → PR created.

Human step: ask me to run the cf commands. Don't read credentials.
Verify: pytest -q && uvicorn api.main:app (hit /health and /watchlist locally)
Finish with §13.0 Git steps (stage id s09).
```

### S10: SAP Build Apps guide + fallback UI (B3)
```text
Load the kairos skill. Read docs/STATUS.md, then only plan §3 (human gate), §7 and §10. Read the route list in api/main.py.

Stage S10. SAP Build Apps is built by clicking in the browser, so produce (a) a precise build guide and (b) a working fallback UI on the same API.
1. docs/buildapps_guide.md, step by step:
   - BTP destination to our API (URL, X-API-Key header)
   - data resources per endpoint
   - five pages: Watchlist (sorted by exposure, cause chips, confidence, REAL/PROXY/SYNTH badges); Molecule detail (ceiling-vs-cost chart, dependency summary, forecast, scenarios); Approval (A5 check table + the 6-item manual checklist, Approve / Edit & approve / Reject with reason / Snooze); Audit (hop timeline + chain status + report link); Recall trace
   - a "Shock +30%" button
   - bindings and formulas for each field
2. ui/fallback/index.html: a single file, vanilla JS + CSS, no build step. The same five pages and the shock button, calling the API with an API-key field stored in localStorage. It works on a laptop with the API on localhost. Clean and readable on a projector.
3. Keep the wording safe: "risk flag for review", never claims about manufacturer intent.

Verify: run the API locally, open ui/fallback/index.html, walk shock → watchlist → detail → approve → audit, and describe what you saw.
Finish with §13.0 Git steps (stage id s10).
```

### S11: LLM explanation with number check (B3)
```text
Load the kairos skill (especially §5 guardrails). Read docs/STATUS.md, then only plan §3 (A3 cause_facts). Read agents/contracts.py.

Stage S11. Implement agents/explain.py.
- explain(forecast, dependency, recommendation) -> {text, used_llm, fallback_reason}.
- Build a JSON fact sheet from cause_facts. The prompt tells the model to write 3–4 plain sentences for a Chief Pharmacist, use only the numbers in the fact sheet, label PROXY/SYNTH values, and never claim a manufacturer's intent.
- LLM_PROVIDER: anthropic (model from LLM_MODEL), none (template only), or sap_genai_hub (leave as a clearly marked stub).
- Number check: extract every number from the LLM text. Each must match a fact-sheet value (allowing rounding). Otherwise, discard the text and use the deterministic template.
- Timeout of 8s → template. Never send hospital identifiers to the LLM.
- Plug it into orchestrator output and /molecule/{id}.
- Tests: a mocked LLM reply with a made-up number → template fallback; provider none → template.

Verify: pytest -q
Finish with §13.0 Git steps (stage id s11).
```

### S12: Para-19 backtest + deck results (B2 + P2)
```text
Load the kairos skill. Read docs/STATUS.md, then only plan §4.3 and §4.5. Read data/seed/para19_events.csv.

Stage S12. Create tests/backtest_para19.py (a script, not a unit test):
- For each month in the window, run A1→A3 using ONLY data dated before that month (no look-ahead; assert it).
- Positives = para-19 formulations with confirmed dates (skip rows still marked verify=true and report how many were skipped). Negatives = a seeded random sample of other scheduled targets.
- Report: median and range of lead time (first AMBER/RED → order date), precision@10, recall. Include a ±20% cost-proxy sensitivity run.
- Write docs/backtest_results.md (table + honest caveats: proxy costs, small sample, directional evidence) and docs/img/backtest_leadtime.png (a clean chart for the deck).
- If the results are weak, say so plainly. Propose at most one principled weight change, then freeze the weights and add a line to docs/DECISIONS.md.

Verify: python tests/backtest_para19.py runs end to end, and pytest -q stays green.
Finish with §13.0 Git steps (stage id s12).
```

### S13: Integration on real data + demo hardening (Bhavesh, with all)
```text
Load the kairos skill. Read docs/STATUS.md, then only plan §10 and §11.

Stage S13. Make the demo bulletproof.
1. scripts/demo_reset.py: rebuild FF_ objects (--reset), load seeds, run the baseline for all targets. Must finish in under 5 minutes on HANA and under 1 minute on SQLite.
2. Check the end-to-end flow on HANA and on SQLite (offline, LLM_PROVIDER=none): shock +30% → ≥3 RED → a detail page with a correlated alternate rejected → approve with checklist → PR payload in FF_MM_EBAN → audit chain ok → recall trace finds the planted batch in 3 locations.
3. docs/demo_script.md: the exact clicks and spoken lines for §10, timed to 6 minutes, plus a fallback path for each step (HANA down → SQLite; Build Apps down → fallback UI; no network → LLM off).
4. docs/preflight.md: a checklist for finale morning (HANA running, CF app awake, row counts, API key, projector resolution, hotspot).
5. Fix only integration bugs here; no new features.

Verify: python scripts/demo_reset.py --backend sqlite && run the full flow twice in a row without errors.
Finish with §13.0 Git steps (stage id s13).
```

### 13.R: Reviewer prompt (Bhavesh, for each PR)
```text
Load the kairos skill. Review PR #<N> in this repo. Do not merge it.

1. gh pr view <N> and gh pr diff <N>. Identify the stage ID and read that stage's prompt in plan §13, plus the plan sections it cites.
2. gh pr checkout <N>, then run pytest -q and the stage's Verify command.
3. Check:
   - scope matches the stage (no edits to other stages' files)
   - contracts are respected
   - constants come from rules/*.yaml
   - provenance columns and REAL/PROXY/SYNTH tags are correct
   - no REAL data typed by hand
   - no secrets, .env or data/raw committed
   - no AI attribution lines in commits (git log main..HEAD)
   - wording never claims manufacturer intent
   - SQL is light on the shared HANA
4. Report: Verdict (merge / merge after small fixes / changes needed), then blocking issues with file:line, then non-blocking suggestions. Keep it short.
5. If I then say "merge", run: gh pr merge <N> --squash --delete-branch, then git checkout main && git pull.
```
