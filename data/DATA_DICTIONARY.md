# Data Dictionary — Acme Agentic GM sample dataset

All data in this folder is **synthetic**. Names, phone numbers, email addresses and
review text are fabricated. No real guest, owner or employee data is present.

The files are shaped like real exports from the systems named below, **including their
inconsistencies**. The systems were bought at different times by different teams and do
not share a common property key. Reconciling them is part of the exercise.

**Briefing date for this exercise: 25 August 2026 (Tuesday), 09:00 IST.**
Treat that as "now". Data after that timestamp exists only where it is forward-looking
(future arrivals, forward rate/pace).

---

## Files

| File | Stands in for | Rows | Grain |
|---|---|---|---|
| `property_master.csv` | Zoho CRM + manual master | 4 | one row per hotel |
| `bookings.csv` | Hotelogix PMS | 1677 | one row per booking (11 Aug – 3 Sep window) |
| `rate_pace.csv` | AxisRooms | 40 | one row per hotel per stay date |
| `business_rules_and_sops.md` | Brand operations manual | — | authoritative business rules |

---

## The join problem

There is no single property key. Each system uses its own:

| System | Key field | Example |
|---|---|---|
| Property master | `hotel_id` | `ACM-BLR-IND-01` |
| Hotelogix (bookings) | `pms_property_code` | `1042` |
| AxisRooms (rate_pace) | `ar_hotel_id` | `AR11042` |

`property_master.csv` carries most of the crosswalk. It does not carry all of it, and it
is not perfectly clean. **Do not assume every record joins.** Records that cannot be
confidently resolved must be surfaced, not dropped and not guessed.

---

## Date and time formats

They differ by system. This is deliberate and matches reality.

| File | Format | Example |
|---|---|---|
| `bookings.csv` | `YYYY-MM-DD` / `YYYY-MM-DD HH:MM` | `2026-08-25` |
| `rate_pace.csv` | `YYYY-MM-DD HH:MM` / `YYYY-MM-DD` | `2026-08-24 19:45` |

All local times are IST (UTC+05:30).

---

## Field notes worth reading

**`bookings.csv`**
- `status` — `Confirmed` (future arrival), `In-House` (currently staying).
- `pms_notes` — free-text operational log, pipe-separated entries. **It frequently contains
  the resolution of an issue that a guest raised elsewhere.** Ignoring it will make you wrong.
- `special_request` — free text captured at booking.
- `loyalty_tier` — blank for most guests. `Acme Black` is the top tier.
- `stay_count` — lifetime stays with the group.

**`rate_pace.csv`**
- `pickup_last_7d` — net rooms picked up in the last 7 days for that stay date.
- `group_block_rooms` / `group_rate_inr` — contracted group inventory on that date.
- `event_flag` — known demand driver, or `none`.
- `notes` — free-text revenue commentary. Read it.

---

## Known data quality issues

We are telling you these exist. We are **not** telling you where, or how many. Finding them,
handling them and reporting them is part of what is being assessed.

- Property identifiers do not fully reconcile across systems.
- At least one system references a property using a name that does not exactly match the master.
- At least one export contains a duplicated record.
- Some records carry guest PII in free-text fields.

Your pipeline should produce a **data quality report** alongside its output. Silently
dropping rows is the worst possible outcome; silently inventing joins is the second worst.
