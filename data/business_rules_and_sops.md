# Acme Hospitality — Operating Rules, SLAs and Guardrails

*Extract from the brand operations manual. This is the authoritative source of business
rules for any agent you build. Where the data and this document disagree, this document
wins — flag the disagreement rather than silently choosing.*

---

## 1. Room readiness

| Rule | Value |
|---|---|
| Standard check-in | 14:00 local |
| Standard check-out | 11:00 local |
| Early check-in | Subject to availability. Guaranteed only for Acme Black / Nova Rewards Gold, or where pre-approved by the GM |
| Sellable status | Only rooms in status **Inspected** may be assigned to an arriving guest. `Clean` means cleaned but not yet supervisor-checked |
| Departure room clean time | 45 minutes per room (deep clean) |
| Stayover room clean time | 20 minutes per room |
| Room attendant productive minutes | 07:00–15:00 shift = 480 min, less 60 min break/briefing = **420 productive minutes** |
| OOO rooms | Excluded from sellable inventory until `expected_ready_ts` |

**Escalation:** if projected ready rooms at 14:00 < arrivals due before 16:00, the GM and
the Ops Head must be notified **before 10:00** the same day.

---

## 2. Guest issue severity and SLA

| Severity | Definition | First response SLA | Resolution SLA | Owner |
|---|---|---|---|---|
| **P1 — Critical** | Safety, security, no water/power, guest threatening public escalation, in-house guest with 2+ unresolved contacts | 15 min | 4 hours | Duty Manager + GM |
| **P2 — High** | Room not usable as sold (AC, hot water, major noise), repeat contact by same guest, VIP request unanswered | 30 min | 12 hours | Duty Manager |
| **P3 — Medium** | Single service failure, amenity missing, in-room technology | 2 hours | 24 hours | Department Head |
| **P4 — Low** | Information request, preference note | 4 hours | 48 hours | Front Office |

**Repeat-contact rule:** any guest who contacts about the same issue **three or more times**
without a logged resolution is automatically **P1**, regardless of the underlying issue.

**Pre-checkout rule:** an in-house guest with an unresolved P1/P2 issue departing within
24 hours must receive a duty-manager service-recovery contact **before departure**. This is
the single highest-leverage negative-review prevention action.

---

## 3. Service recovery and compensation limits

| Action | Limit | Approval |
|---|---|---|
| Apology + amenity (fruit, beverage, dessert) | Up to INR 800 | Duty Manager |
| Complimentary meal | Up to INR 2,000 | Duty Manager |
| Room-night rate adjustment | Up to 25% of one night | GM |
| Full room-night waiver | 1 night | GM + Ops Head |
| Anything above | — | Ops Head + Finance |
| Loyalty points goodwill | Up to 5,000 points | Duty Manager |

**No AI agent may commit compensation to a guest.** Agents recommend; a named human approves.

---

## 4. Rate and revenue guardrails

- BAR may be moved by an agent **recommendation only**. All rate changes require Revenue
  Manager approval before pushing to AxisRooms.
- Maximum single-move BAR change: **±15%** of current BAR. Anything larger is an exception
  requiring Ops Head sign-off.
- **Group-contracted rooms are excluded** from pickup-based demand signals. Pickup driven by
  a contracted block is *not* evidence of transient demand.
- Do not recommend a BAR increase when remaining saleable transient inventory is **below 10
  rooms** unless there is a confirmed compression event — the revenue upside does not justify
  the OTA ranking and conversion risk.
- Do not recommend closing the lowest rate plan when `channel_mix_ota_pct` exceeds 60% without
  flagging visibility risk.
- Comp-set position: a property priced more than 10% below comp-set average for 7+ consecutive
  days must be flagged to the Revenue Control Tower.

---

## 5. Review and reputation

| Rule | Value |
|---|---|
| Response SLA — rating ≤ 2 (or ≤ 4/10) | 24 hours |
| Response SLA — rating 3 (or 5–7/10) | 48 hours |
| Response SLA — rating ≥ 4 (or ≥ 8/10) | 72 hours, template permitted |
| Tone | Warm, specific, never defensive. Never dispute the guest's account publicly |
| Never in a public response | Guest full name, room number, booking ID, compensation offered, blame on a named staff member |
| Repeat theme rule | The same complaint theme appearing in 3+ reviews in 30 days is an operational defect, not a guest-service issue — it must become a corrective task, not just a reply |

**Draft review responses are never auto-published.** They are queued for GM approval.

---

## 6. Audit and compliance

| Rule | Value |
|---|---|
| Brand score threshold | Acme ≥ 85%, Nova Hotels ≥ 85%, Partner Hotels partner ≥ 75% |
| High-severity finding | Corrective task due within 3 days |
| Medium-severity finding | Corrective task due within 7 days |
| Overdue High finding | Auto-escalate to Ops Head, property flagged **At Risk** |
| Repeat finding | Same finding description recurring in 2+ consecutive audits = **systemic**. Escalate to Ops Head and Brand Compliance regardless of severity. 3+ consecutive = property review |

---

## 7. Escalation matrix

| Role | Receives |
|---|---|
| Front Office Executive | P3/P4 guest requests |
| Duty Manager | P1/P2 guest issues, service recovery |
| Housekeeping Supervisor | Room readiness, cleaning queue |
| Chief Engineer | Engineering work orders |
| General Manager | Readiness shortfall, VIP handling, rate adjustments up to 25%, review responses |
| Revenue Manager | All rate and inventory actions |
| Operations Head | Overdue High audit findings, At Risk properties, repeated SLA breaches, owner escalations |
| Owner Success Manager | Owner communication unanswered > 48 hours, capex disputes |

---

## 8. Data handling rules

- Guest **PII** — full name, phone number, email address, physical address — must be minimised
  in any prompt sent to a model. Use guest ID and a masked reference (e.g. `Mr. B****n`,
  `+91 ****012277`) wherever the model does not strictly need the raw value.
- No guest PII may appear in an owner report, a board report or a public review response.
- Every AI-generated recommendation must carry: the data it used, the reasoning summary, the
  action proposed, the person notified, and the outcome. An action without an audit trail is a
  defect.
- Model outputs must not assert facts absent from the supplied data. If the data is
  insufficient, the correct output is a stated gap and a lowered confidence score — not a
  plausible guess.

---

## 9. Task object contract

Every recommendation that requires human work becomes a task. Minimum fields:

```
task_id, hotel_id, department, title, description, priority (P1..P4),
owner_role, created_at, due_at (derived from SLA), status
(Open | In Progress | Blocked | Closed), source_agent, evidence_refs[],
requires_human_approval (bool), approved_by, closure_note
```

`evidence_refs[]` must point at real record identifiers from the source data — booking_id,
message_id, review_id, finding_id, call_id, room_no. A task with no evidence reference is
not actionable and will be rejected by the Ops Head.
