# STATUS (keep < 60 lines; overwrite, don't append history)

Finale: South & East Region, SRM Chennai, date: ____ (D0)
Updated: 2026-09-28 by Antigravity

## Owners
- B1 Data & HANA: ____
- B2 Agents & rules: ____
- B3 Platform, UI & action: ____
- P1 Story & deck: ____
- P2 Demo & validation: ____
- Reviewer / merger: Bhavesh

## Stages (plan §13). Edit only your own row: todo → in progress → PR open → merged
| ID | Stage | Owner | State |
|---|---|---|---|
| S00 | Bootstrap | B2 | merged |
| S01 | HANA schema + Day-1 checks | B1 | merged |
| S02 | NPPA + NLEM ingest | B1 | merged |
| S03 | Cost/origin/NSQ/producers + synth hospital | B1 | merged |
| S04 | A1 Margin Sentinel | B2 | merged |
| S05 | A2 Dependency + graph | B2 | merged |
| S06 | A3 Forecaster | B2 | merged |
| S07 | A4 Resilience + A5 Validator | B2 | merged |
| S08 | A6 Audit/Action + orchestrator | B3 | merged |
| S09 | FastAPI + BTP deploy | B3 | merged |
| S10 | Build Apps guide + fallback UI | B3 | merged |
| S11 | LLM explanation (OpenRouter) | B3 | merged |
| S12 | Para-19 backtest | B2 + P2 | merged |
| S13 | Disruption Sensing + Sea Routes agent | B2+B3 | merged |
| S14 | Scenario Planning agent (what-if) | B2+B3 | merged |
| S15 | Rerouting & Logistics agent | B2+B3 | merged |
| S16 | Inventory Rebalancing agent | B2+B3 | merged |
| S17 | Compliance & HITL Decision Queue | B3 | merged |
| S18 | Orchestrator: full end-to-end pipeline | B3 | merged |
| S19 | Agentic UI (Response Console + Decision Queue) | B3 | merged |
| S20 | Integration + demo hardening | Bhavesh | in progress |
| S21 | Route Optimizer (searoute + Leaflet) | Claude | in progress |
| S22 | Plain-language landing page (nav + hamburger menu) | Claude | merged |
| S23 | Order a Medicine: seeded demo suppliers ranked + mapped | Claude | in progress |
| S24 | Live Disruption Sensing: real weather (Open-Meteo) + news (NewsAPI.org) | Claude | in progress |

## Agent Pipeline
A1 Margin Sentinel → A2 Dependency → A3 Forecast → A4 Resilience → A5 Validator → Human Gate → A6 Audit
Disruption path: Sensing → Scenario Planning → Logistics → Inventory → A4/A5 → Compliance → Human Gate → A6 Audit

## Test coverage
- 216 tests · 215 pass on SQLite, 1 pre-existing HANA-only failure in test_api.py (unrelated to SQLite path) · ~45s
- HANA live: full pipeline ~40s, replica build ~11s, decision queue reads ~0.6s, audit chain verified

## Blocked / open questions
- S12 is a pipeline check only (SYNTH costs, 2 positives). Real evidence needs: verify the Oct-2024 / Dec-2019 Para-19 rows + real TradeStat costs, then rerun `python tests/backtest_para19.py`
- S01: add FF_G_E.WEIGHT (DOUBLE) + IS_PROXY (NVARCHAR(8)) so A2 can use HANA Graph (see PR feat/harnish-s01-align)
- Does the shared HANA allow CREATE GRAPH WORKSPACE / PAL?
- BTP trial region, and can CF reach HANA eu10?
- S24: NEWSAPI_KEY not set anywhere yet (free key at newsapi.org/register) - live weather works without it, but news-based signals stay off until someone adds it to .env

## Day-1 check results
- 2026-09-28 laptop: HANA connect PASS · create/drop table PASS | 7 new agent tables created on HANA PASS | API endpoints live PASS | OpenRouter LLM PASS (gpt-4o-mini) | Graph: PASS | PAL: BLOCKED (insufficient privilege) → A3 uses statsmodels
