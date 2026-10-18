# 5-minute walkthrough (screen recording) — show the output, not the IDE

Before recording: `export OPENAI_API_KEY=...`, `python -m revintel.pipeline --provider openai`, `streamlit run app.py`.
(If you record in mock mode say so up front — the banner is on screen.)

| Time | Show | Say |
|---|---|---|
| 0:00 | **Cluster overview**, "Your 60 seconds" | "It's 09:15 on 25 Aug. Top of the page is what I'd do first: the Hyderabad TechCon rate move, the Nova restriction, the inventory reconciliation. Four hotels, each with both agents' summaries." |
| 0:45 | **Acme Westside → Briefing** | "TechCon on 30 Aug: 85.9% occupancy, pickup 21, no group block, BAR 15.7% under comp. Only 9 rooms left — normally §4.4 would block an increase, but this is a confirmed compression event. The model asked for comp parity, INR 5,100; the guardrail capped it at +15%, INR 4,945, and raised an Operations Head sign-off task for the rest." |
| 1:45 | **Tasks & evidence** → open the increase task | "Every task has an owner, priority, SLA-derived due time and evidence. These records are resolved from the database, not from the model's text — I can check the machine's work without opening AxisRooms." Click **Audit & guardrails**: show the `clamped_to_15pct` row. |
| 2:30 | **Nova Whitefield** | "28 Aug looks hot — 93.5%, pickup 32. But pickup sits on a 40-room group block (§4.3) and only 6 rooms remain (§4.4), so no price increase. MinLOS2 is already on, so we don't repeat it. And the data quality tab: the PMS holds 95 bookings in a 92-room hotel — that's a reconciliation task, and it lowers the agents' confidence." |
| 3:30 | **The Amber Court** | "Notes say 'consistently below comp'. The rule needs 7 consecutive days; the longest run is 3. So no Control Tower flag — a pricing review instead, and the conflict is reported." |
| 4:10 | **Data quality tab** (any hotel) | "Nothing dropped silently: 1,677/1,677 resolved, 87 bookings timestamped after the briefing time, possible duplicates flagged but not merged, unreliable fields called out." |
| 4:40 | Terminal: `pytest` / `python -m evals.run` | "Guardrails and PII are tested with adversarial model outputs; 11 eval cases are the acceptance test for any model change. Recommend, don't act — approving a task pushes nothing." |
