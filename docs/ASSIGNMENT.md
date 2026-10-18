# Take-home assignment — AI Tools & Application Developer
## Acme Hospitality · "Revenue Intelligence v0"

**Time budget: 4–6 hours.** We mean it. We would rather see a small thing done thoughtfully
than a large thing done carelessly. If you run out of time, ship what works and write down
what you would have done next — that section is scored.

**Return by:** [insert date — 5 calendar days from receipt]

---

## 1. Context

Acme Hospitality operates and manages hotels across three brands — Acme, Nova Hotels,
and Partner Hotels (a franchise/partner product for independent hotels). Operations today run on
a stack of disconnected systems: Hotelogix (PMS), AxisRooms (channel & revenue), Kepsla
(reviews), Guestara (guest messaging), SafetyCulture (audits), and a remote reception call
centre.

Nobody has a single view, especially the Revenue Management team. By the time a sudden spike
in demand is noticed, the hotel might already be sold out at a suboptimal rate. Alternatively,
if the comp-set drops their prices, our hotels might lose vital bookings because no one reacted
fast enough.

We are building an **Agentic GM**: a layer that reads all of this live, spots what is about to
go wrong, and turns it into accountable tasks with owners and deadlines. Not a chatbot — an
operating brain.

You are being asked to build the first, smallest useful slice of it, focused entirely on the **Revenue Department**.

---

## 2. What you are building

**A Revenue Intelligence Briefing for the Cluster Revenue Manager, for the morning of 25 August 2026.**

Imagine the Revenue Manager opens one screen at 09:15 with a coffee. In sixty seconds they need to
know: which of my four hotels requires immediate rate adjustments, where we should place restrictions, and why.

### Required — the core build

**2.1 Data layer**

Ingest the relevant files in `data/` (primarily `rate_pace.csv`, `bookings.csv`, and `property_master.csv`).
Produce a unified, queryable view keyed on a single internal `hotel_id` or property code.

Your pipeline must also emit a **data quality report** — records you could not resolve,
duplicates you found, ambiguous joins, and what you did about each. Dropping rows silently is
a failure; inventing a join is a worse failure.

**2.2 Two agents**

Build these two. Both must use an LLM for the judgment parts — not just `if` statements.

| Agent | Mission |
|---|---|
| **Pricing Optimization Agent** | Recommend Best Available Rate (BAR) adjustments by analyzing pickup pace, forecasted occupancy, comp-set average rates, and remaining capacity. |
| **Channel & Inventory Restriction Agent** | Prevent revenue leakage by recommending OTA restrictions (e.g. MinLOS, closing discount rates) on high-demand dates, based on channel mix, event flags, and block bookings. |

Every agent output must conform to this contract — it is the house standard and adherence is
scored:

```json
{
  "agent": "pricing_optimization",
  "hotel_id": "ACM-BLR-IND-01",
  "as_of": "2026-08-25T09:00:00+05:30",
  "summary": "...",
  "identified_opportunities": [{"date": "2026-08-27", "priority": "High", "why": "..."}],
  "recommended_actions": [{"action": "Increase BAR to INR 6500", "owner_role": "Revenue Manager", "due_at": "...", "rationale": "..."}],
  "evidence": [{"source": "rate_pace.csv", "ref": "2026-08-27", "detail": "..."}],
  "confidence": 0.0,
  "requires_human_approval": true,
  "data_gaps": ["..."]
}
```

**2.3 Action layer**

Every recommendation that needs a human becomes a **task record** with an owner role, a
priority, an SLA-derived due time, a status, and evidence references pointing at real record
IDs. The task schema is in `data/business_rules_and_sops.md` §9.

A recommendation that does not become a trackable task is just an opinion.

**2.4 Interface**

A working UI showing the briefing for 25 Aug 2026: a hotel selector, each hotel's summary,
its ranked revenue opportunities, the generated task list, and — importantly — the **evidence behind each
recommendation**, so the Revenue Manager can check the machine's work without opening other systems.

Web app, Streamlit, whatever you like. It must run and be clickable. A notebook is not a UI.

### Optional — only if the core is solid

- A small **evaluation harness**: a handful of test cases asserting your agents behave
  correctly on known inputs, so a regression is caught before a bad rate is pushed live.

We would rather see the required part done well plus an honest "here's what I'd do next"
than half-working agents. Reaching for the optional parts at the expense of the core
is a scoring negative, not a positive.

---

## 3. The rules the system operates under

`data/business_rules_and_sops.md` is the authoritative source for SLAs, rate guardrails,
and escalation. Your agents are expected to follow it (specifically **§4 Rate and revenue guardrails**). Where the data and the rules disagree, flag it.

Three constraints that are non-negotiable:

1. **No invented facts.** Every claim in an agent's output must trace to a record in the
   supplied data. If the data does not support a conclusion, the correct output is a stated
   gap and a lower confidence score.
2. **Recommend, don't act.** Nothing in this build actually changes a rate in the PMS. Human approval gates are part of the design.
3. **Minimise guest PII in prompts.** The data contains names, phone numbers and email
   addresses in free text. Think about what actually needs to reach the model.

---

## 4. Deliverables

1. **A git repository** (or zip) with your code.
2. **A `README.md`** — how to run it, in a way that works on a clean machine. State any
   assumption you made. If we cannot run it in ten minutes we will score what we can read.
3. **A short screen recording (max 5 minutes)** walking through the briefing you generated.
   Loom, or an mp4. Show us the output, not your IDE.
4. **A design note (1–2 pages, `DESIGN.md`)** covering:
   - How you handled property identity across systems, and what you did with what didn't resolve.
   - How you keep the model from inventing things.
   - **Cost at scale**: this runs daily across 200 hotels. Estimate the token cost of your
     design and say what you would change to control it.
   - What breaks first as this grows, and what you would build next.
   - Where you used AI coding tools, what you accepted from them, and what you rejected.

---

## 5. Ground rules

**Use AI coding assistants.** Claude, Cursor, Copilot, whatever you work with — we use them
every day and we want to see how you work with them. Tell us where you used them in your
design note. Using them well is a positive signal. Shipping code you cannot explain is not.

You may use any language, framework, model provider and libraries you like. Justify the
choices briefly in your README.

If an API key is needed to run your code, do **not** commit it. Use an environment variable
and document it. Committed secrets are an automatic mark-down.

If you find the data ambiguous, make a reasonable decision, write it down, and move on. Do
not email us to ask — how you handle ambiguity is part of what we are looking at.

---

## 6. How this is assessed

Weighted, out of 100:

| Area | Weight |
|---|---|
| Data engineering & entity resolution | 20 |
| Agent design & adherence to the output contract | 20 |
| Grounding — does it avoid asserting things the data doesn't support | 15 |
| Action layer & operational realism | 10 |
| Interface usefulness to an actual Revenue Manager | 10 |
| Engineering quality — runs cleanly, structured, no committed secrets | 10 |
| Design note & scale thinking | 10 |
| Demo & communication | 5 |

We care more about judgment than volume. The dataset contains situations that look
straightforward and are not. Reading carefully will serve you better than writing quickly.

---

## 7. What happens next

Shortlisted candidates get a 45-minute conversation where we walk through your code together
and discuss the decisions in your design note. We will ask you to change something live.

Questions about the *process* (timing, format) are welcome. Questions about *how to interpret
the data* will be answered with "make a call and document it."

Good luck.
