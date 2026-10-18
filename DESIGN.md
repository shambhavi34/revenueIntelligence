# DESIGN.md — Revenue Intelligence v0

## 1. Property identity across systems
Three keys, no shared one: master `hotel_id`, Hotelogix `pms_property_code`, AxisRooms `ar_hotel_id`. The master is the **only** crosswalk.
A record joins iff its system key matches exactly one master row after trimming/case-normalising (each normalisation is counted in the report).
There is no fuzzy matching on names and no key derived by pattern (`AR1`+code would work here — and would be an invented join the day it didn't).
Anything else — unknown key, key claimed by two master rows, duplicate hotel+date — goes to a **quarantine table with a reason**, is excluded from every
metric, and appears as a *high* finding. A test feeds deliberately broken rows and asserts `resolved + quarantined == total`.
On the supplied pack everything resolves (1,677/1,677 bookings, 40/40 rate rows); what the report surfaces instead is more interesting:
the Nova 28 Aug **95 bookings in a 92-room hotel**, 87 bookings created after the briefing time, a group block whose contract date contradicts its rows,
a blank Kepsla slug, name/slug mismatches, and two same-guest/same-dates pairs that are flagged as *possible* duplicates but **not merged** (names, channels
and rates differ, so a merge would be an invention; `guest_id` maps to several names in 192 cases, so it is not a person key).

## 2. Keeping the model from inventing things
Prevention + detection, with the model treated as untrusted:
* **It only sees facts we computed.** Per hotel: ~1.9k tokens of JSON with every number pre-derived (remaining rooms, % vs comp, ±15% band, `increase_allowed`,
  group-confounded pickup, confirmed compression, consecutive-day runs) plus short *data warnings* ("forecast_rooms is below rooms_sold on 8/10 dates — unusable").
  No outside knowledge is requested; the prompt says unsupported ⇒ `data_gaps` + lower confidence.
* **Forced tool call** returns schema-shaped JSON; the house contract is a strict pydantic model (`extra=forbid`) — the contract object carries nothing else.
* **Post-check (`guardrails.py`)** re-verifies every proposal against the same code-computed numbers: evidence `ref`s must exist in the facts it was given
  (invented refs are removed, a gap is added, confidence drops); dates must be in the horizon; BAR moves are clamped to ±15% with an Operations Head exception task for the
  overshoot; increases under 10 rooms are dropped unless compression is confirmed; the Control Tower flag is converted to a review unless 7+ consecutive days;
  plans must be closable discount plans; OTA>60% adds a visibility-risk note; **action text is rebuilt from structured fields** so text and number cannot disagree;
  3+-digit figures in the narrative that appear nowhere in the data are flagged. Confidence is capped by data-quality warnings. Every intervention is logged to `audit.json` and shown in the UI.
* **Rules beat data and beat the model:** where they conflict (Amber "consistently below comp" vs the 7-day rule) the rule wins and the conflict is reported.
* Tasks require ≥1 evidence ref that resolves to a real record (§9); the UI resolves refs from SQLite, not from the model's words.
Residual risk: the check catches *numeric and referential* inventions, not a fluent but wrong causal story. Mitigation: low confidence on bad data, human approval on everything, the eval cases below.

## 3. Guest PII
Prompts are built from an allow-list of fields (booking_id, plan, channel, ADR, dates) — names, phones and emails are never read by the facts builder, and `pms_notes`/`special_request`
are never sent (group blocks are extracted structurally). A scanner then checks **every outgoing prompt against every raw PII value in the export** (whole-token n-gram match, linear time) and
refuses to send on a hit — a refactor that leaks a name fails at runtime and in tests (including a positive-control test). Stored data is masked too (`R***t B**********n`, `+91 ****012277` per rules §8; email dropped; free text scrubbed), and an eval asserts no raw
guest PII anywhere in outputs or the SQLite view. Staff names in notes are not guest PII but still stay out of prompts.

## 4. Cost at scale (200 hotels/day)
Measured from this build (chars/4 as a token proxy; `audit.json` records real `usage` after a live run): per call ≈ **900 tokens system (static)** + **~1,900 facts** ≈ 2.8k input; output assumed **~700** (structured JSON, 1–3 actions).
2 agents × 200 hotels = **400 calls/day ≈ 1.1M input + 0.28M output tokens**. At *illustrative* mid-tier pricing ($3 in / $15 out per MTok — placeholder, check your provider's current rates) ≈ **$7–8/day (~$230/month)**.
Scaling trap: facts grow linearly with the horizon (~121 tokens/date): a 60-day window is ~8.9k input tokens/call (~3×) → roughly $15–20/day at the same prices. Levers, in order of impact:
1. **Deterministic triage before the model** — send only hotels/dates with a signal (event, pace break, comp gap, group block, <15 rooms) and summarise quiet dates in one line; most hotel-days need no judgment (est. −60–80%).
2. **Prompt caching** of the static system prompt/rules (cached reads ≈ 10% price; ~1/3 of input) and the on-disk response cache keyed by prompt hash (unchanged facts ⇒ zero tokens on reruns/retries).
3. **Batch API** for the overnight run (≈ −50%); only intraday re-checks need synchronous calls.
4. **Tiering:** a small model drafts, a larger model reviews only High-priority/low-confidence outputs; one call per hotel for both agents if prompts are merged.
5. Cap output (`max_tokens`), shorter keys in facts, drop redundant per-date fields. With 1+2+3 the same fleet is plausibly **~$1–2/day**.

## 5. What breaks first, and what I'd build next
1. **Crosswalk maintenance.** The master is hand-curated and Partner Hotels onboard continuously; quarantine volume is the first alarm. Next: a crosswalk service with an owner, alert on unresolved-rate > 0, and a review queue (still no auto-merge).
2. **As-of integrity.** We found AxisRooms is not a snapshot. Next: timestamped snapshots/event log per source so "pickup", "as of" and "yesterday's tasks" are reproducible.
3. **Tasks across days.** Today's task IDs are per run. Next: a persistent task store with idempotent keys (hotel+date+action), supersede/close yesterday's open tasks, SLA breach alerts, durable approvals with identity (currently a local file).
4. **Semantics at 200 hotels.** `channel_mix_ota_pct`, `forecast_rooms`, plan names and event flags have undocumented, per-property meanings. Next: per-source contract tests, per-property thresholds (my 80%/10-pickup compression and demand definitions are assumptions), comp-set history, out-of-order rooms.
5. **LLM operations:** rate limits/latency (parallelise, batch), model-version drift (evals gate every model change; `replay` lets CI run without a key), prompt versioning in the audit trail.
6. Then: live Hotelogix/AxisRooms connectors, the other departments' agents, and — only behind an approval gate — pushing an approved BAR to AxisRooms.

## 6. Where I used AI coding tools — what I accepted and rejected
I built this with Claude (Anthropic) as the coding assistant: I gave it the brief, `business_rules_and_sops.md`, the data dictionary and the three CSVs, and it profiled the data, proposed the architecture and wrote the first version of the code, tests and docs. I then ran it, hit problems, and had it add an OpenAI provider because that is the API key I have.

**Accepted from the assistant (and why)**
* The split *code computes facts → model judges → code re-checks the model*. Revenue guardrails (±15%, <10 rooms, 7-day comp rule) must not depend on a model obeying a prompt.
* Quarantine-not-drop for unresolved records, and exact-key joins through the master only.
* Rebuilding action text and prices from structured fields, and an evidence-reference registry, so the output cannot cite a record that does not exist.
* A prompt PII scanner that checks every outgoing prompt against every raw name/phone/email in the export.
* An eval harness that checks final outputs on known cases, so it works for any model.

**Rejected, or corrected after review**
* Fuzzy name matching for hotel identity (a wrong join is worse than an unresolved record).
* Trusting the revenue note "consistently below comp" over the 7-day rule: the data shows a longest run of 3 days, so no Control Tower flag, and the conflict is reported.
* Reading `pickup_last_7d` as transient demand on the group-block night.
* Merging the two same-guest/same-date booking pairs, and sending `pms_notes` or guest fields to the model.
* A first PII matcher that flagged "Neha Reddy" inside "Sneha Reddy" and was slow: replaced by a whole-token, linear-time scan. A converted Control Tower flag that still used "flag" wording was also fixed. Both were found by the tests, not by reading.

**What I verified myself**
* I ran the Streamlit app on my own machine with an OpenAI key. The first key was rejected (401 `token_invalidated`), and the app showed that as an explicit agent failure rather than crashing or displaying invented output, which is the failure behaviour I wanted.
* I ran the pipeline and the app, opened the Audit & guardrails tab, and confirmed that the Hyderabad 30 Aug increase was clamped from the model's INR 5,100 to INR 4,945 (+15%, since 4,300 × 1.15 = 4,945), with an Operations Head sign-off task raised for the remainder.
* The 44 unit tests and 11 eval cases were run by the assistant against the mock stand-in and stub clients, not against a live model. I have not yet reproduced a live-model run; that is listed under known limits.

**Known limits**
* The live model calls (OpenAI, and Anthropic) were stub-tested only while building; the first real run is the acceptance test (`python -m evals.run --provider openai`).
* The no-key demo mode uses a rule-based stand-in that is labelled as such in the UI; it is not an LLM.