# Revenue Intelligence v0 — Agentic GM, Revenue slice

A morning briefing for the Cluster Revenue Manager (briefing time fixed at **2026-08-25 09:00 IST**): which hotels need rate action,
where to restrict OTA inventory, and *why* — with the evidence one click away. Two LLM agents (Pricing Optimization, Channel & Inventory
Restriction) sit on a deterministic data layer and a deterministic post-check. **Recommend, don't act:** nothing writes to a PMS or AxisRooms.

## Run it (≈ 5 minutes, clean machine, Python 3.10+; developed and tested on 3.13)

```bash
python -m venv .venv && source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -r requirements.txt

python -m revintel.pipeline --provider mock               # no API key needed -> writes outputs/
streamlit run app.py                                      # the UI (also runs the pipeline on first launch)
```

With a real model (this is the intended mode) — OpenAI or Anthropic:

```bash
export OPENAI_API_KEY=sk-...                              # never commit it; .env is git-ignored, see .env.example
python -m revintel.pipeline --provider openai             # 8 model calls (4 hotels x 2 agents); responses cached in outputs/llm_cache/
# or: export ANTHROPIC_API_KEY=sk-ant-...  and  --provider anthropic
# model: REVINTEL_OPENAI_MODEL (default gpt-4o) / REVINTEL_MODEL (Anthropic) - set a newer one if your account has it
streamlit run app.py
```

| `--provider` | Behaviour |
|---|---|
| `openai` / `anthropic` | Real model via a forced function/tool call (`submit_briefing`) with the same schema. Responses are cached on disk by prompt hash; `--refresh` re-calls. |
| `replay` | Only reads `outputs/llm_cache/` — lets a reviewer without a key replay a real run (commit that folder after a live run). |
| `mock` | **Heuristic stand-in, not an LLM**, for offline demo/tests. Every output is labelled `mock-heuristic` and the UI shows a banner. It deliberately makes tempting mistakes (price to full comp parity, a Control Tower flag, repeating an existing restriction) so the post-check has something real to catch. |
| `auto` (default) | Anthropic if `ANTHROPIC_API_KEY` is set, else OpenAI if `OPENAI_API_KEY`, else replay if cached, else mock. |

Tests and evals: `pytest` (44 tests, ~20 s) · `python -m evals.run --provider mock|anthropic` (11 outcome checks on known inputs; provider-agnostic).

> **Not yet exercised against a live API.** Both provider paths are covered by stub-client tests (request shape, caching, replay) but the
> first real call has to be made with your key. Run `python -m evals.run --provider openai` afterwards — those eval cases are the acceptance test.

## Architecture

```
data/*.csv ──► ingest.py + dq.py ─────────────► SQLite  (hotels, bookings[masked], booking_nights, rate_pace, v_hotel_date,
   │            crosswalk via master ONLY;                 dq_findings, tasks)   +  outputs/dq_report.md|json
   │            unresolved -> quarantine; checks
   ▼
facts.py  (code does the arithmetic: remaining inventory, % vs comp, ±15% band, group-confounded pickup,
   │       confirmed compression, consecutive-day runs, rule pre-checks per date)
   ▼
pii.py scan ─► prompts.py ─► LLM  (judgment only: which dates matter, ranking, what to recommend, why)   [llm.py / mock_llm.py]
                                │  structured draft via forced tool call
                                ▼
                       guardrails.py  (post-check; the model is never trusted)
                         • evidence refs must exist in the facts it was given      • §4.2 clamp to ±15% (+ Ops Head exception task)
                         • dates must be in the horizon                            • §4.4 block increase < 10 rooms w/o compression
                         • figures in narrative must appear in the data            • §4.5 visibility-risk note, §4.6 needs 7+ days
                         • action text rebuilt from structured fields              • confidence capped by data-quality warnings
                                ▼
                  contract (pydantic, extra=forbid) ─► agent_outputs.json   (exact house contract)
                  tasks.py (§9 task object, SLA-derived due_at) ─► tasks.json / SQLite
                  audit.json (interventions, provider, tokens, prompt hash) ─► Streamlit UI (app.py)
```

**Why this split:** an LLM is good at weighing messy signals and writing the explanation; it is bad at arithmetic and at *not* saying plausible
things. So code owns every number and every rule; the model owns judgment; code re-checks the model. Every guardrail intervention is
recorded and shown in the UI.

### Layout
```
revintel/  config · ingest · dq · facts · guardrails · contract · tasks · pii · prompts · llm · mock_llm · pipeline · agents/
app.py     Streamlit UI            tests/  pytest            evals/  known-input eval cases         data/  the supplied pack
DESIGN.md  design note             docs/DEMO_SCRIPT.md  5-minute walkthrough script
```

### Stack — why
Python + pandas (the data work is tabular and small, and the team's data tooling is Python); SQLite (a real queryable "unified view" with
zero setup — `v_hotel_date` joins AxisRooms + PMS counts on `hotel_id`); pydantic v2 (the output contract as an enforceable, `extra=forbid` schema);
OpenAI or Anthropic SDK with a forced tool/function call (schema-shaped JSON instead of parsing prose; the post-check is provider-independent); Streamlit (clickable UI in one file).

## What the data actually contains (found by profiling, all in the data-quality report)
* **Identity:** all 4 PMS codes and 4 AxisRooms ids resolve through the master (1,677/1,677 bookings, 40/40 rate rows). No fuzzy matching, no pattern-derived keys. Master quirks reported: blank Kepsla slug (Amber Court); `hotel_name` vs slug mismatch (Downtown/Indiranagar, Westside/Gachibowli).
* **PMS vs AxisRooms:** Nova Whitefield 28 Aug holds **95 bookings on a 92-room hotel** (AxisRooms says 86 sold) → high finding + reconciliation task.
* **Time leakage:** 87 bookings are timestamped *after* 09:00 on 25 Aug; AxisRooms `rooms_sold` tracks the full PMS count, so it is not an as-of snapshot.
* **Group block ZT-OFF-26** (40 rooms, 27–28 Aug): pickup of 32 on 28 Aug is group-driven (§4.3) and only 6 rooms remain (§4.4) → no BAR increase. The note says "contracted 12-Aug" but 39 of 40 rows carry other creation dates.
* **Hyderabad 30 Aug (TechCon):** the one real increase case — 9 rooms left but a confirmed compression event lifts the §4.4 block; capped at +15% (INR 4,945), full comp parity (INR 5,100) needs Ops Head sign-off. 29 and 31 Aug sit inside the event window but are flagged `none`.
* **Amber Court:** notes say "consistently below comp", but the longest run >10% below comp is **3 days** (rule §4.6 needs 7+) → the mandatory flag is *not* raised; a review is recommended instead and the rule-vs-data conflict is reported.
* Unreliable fields (flagged, used with caution): `forecast_rooms` is below rooms already sold on most dates; `channel_mix_ota_pct` and `pickup_last_7d` cannot be reproduced from PMS data.
* Two same-guest/same-dates pairs at Nova are flagged as *possible* duplicates and **not merged**; no exact duplicate records exist. PII is in structured columns only (regex found none in free text).

## Assumptions (decide, document, move on)
1. **`as_of` = 2026-08-25T09:00+05:30**; the system clock is never used.
2. `rooms_available` is **capacity** (equals master room count on every row): remaining = capacity − `rooms_sold`. Out-of-order rooms are not in the data, so remaining is an upper bound.
3. `rate_pace` is treated as "now" even though it is not an as-of snapshot; bookings created after as-of are flagged and excluded from as-of recounts, never used as pickup.
4. Transient sold = `rooms_sold` − PMS `GROUP-CONTRACT` rows for that night.
5. **Confirmed compression** (lifts §4.4) = event flag present **and** occupancy ≥ 80% **and** `pickup_last_7d` ≥ 10 **and** no group block.
6. **High-demand date** = occupancy ≥ 80% **or** event flag **or** group block (restriction agent acts only on these).
7. **Closable discount plans** = `OTA-PROMO`, `ADV21-CP` (inferred from plan names; BAR plans are not discounts, `CORP-NEG`/`GROUP-CONTRACT` are contractual).
8. OTA channel set (MakeMyTrip, Goibibo, Airbnb, Expedia, Booking.com, Agoda) is used **only to reconcile** `channel_mix_ota_pct`, never for decisions.
9. The rules contain **no revenue-task SLA**. Urgency High/Medium/Low → P2/P3/P4 (P1 stays for guest-critical issues); `due_at` = as_of + the §2 resolution SLA (12h/24h/48h), capped at 18:00 the day before the stay date and floored at as_of + 2h.
10. Owner role is **Revenue Manager** for all rate/inventory actions (§7); a >15% exception goes to **Operations Head** (§4.2). "Revenue Control Tower" (§4.6) is not in the §7 matrix, so flag tasks are owned by the Revenue Manager.
11. §7 lists GM "rate adjustments up to 25%" while §4.2 caps BAR at ±15%: §4 governs BAR; the 25% is read as guest-compensation (§3).
12. Evidence refs: AxisRooms rows use `AR#####|YYYY-MM-DD` (no row id exists); bookings use `booking_id`. Rules (`§4.x`) and DQ ids can be cited in agent evidence but are not task evidence (§9 wants source-data identifiers).
13. Confidence = min(model, cap) − penalties, cap = 0.9 − 0.10 per *high* and 0.03 per *warn* data finding for that hotel; never above 0.9.
14. "Approve" in the UI records a name in `outputs/approvals.json`. It pushes nothing.

## Not done / next
See DESIGN.md §5 (task store with cross-day de-duplication, as-of snapshots, tiered triage, live connectors, per-property thresholds).
