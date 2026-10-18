"""Data layer: load -> resolve identity -> quarantine the unresolved -> flag anomalies -> SQLite view.

Identity strategy
-----------------
`property_master.csv` is the only crosswalk. A record joins only if its system key matches exactly one master
row (after trimming whitespace / upper-casing). Anything else goes to a quarantine table with a reason.
We never match on hotel *names* and never derive keys by pattern (e.g. "AR1" + PMS code), even where
that would happen to work.
"""
from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import dataclass, field

import pandas as pd

from . import config as C
from . import dq as DQ
from .pii import mask_name, mask_phone, pii_values, scrub_text

CONTRACT_RE = re.compile(r"contracted\s+(\d{1,2}-[A-Za-z]{3}-\d{4})", re.I)
BLOCK_RE = re.compile(r"block\s+([A-Z0-9][A-Z0-9-]{3,})")


@dataclass
class Dataset:
    master: pd.DataFrame
    bookings: pd.DataFrame
    bookings_quarantine: pd.DataFrame
    rate: pd.DataFrame
    rate_quarantine: pd.DataFrame
    nights: pd.DataFrame
    recon: pd.DataFrame
    findings: list[DQ.Finding]
    raw_pii: dict = field(repr=False, default_factory=dict)  # in-memory only, for leak scanning

    def hotel_ids(self) -> list[str]:
        return list(self.master["hotel_id"])


def _read(name: str) -> pd.DataFrame:
    return pd.read_csv(C.DATA_DIR / name, dtype=str, keep_default_na=False)


def _norm_pms(x: str) -> str:
    s = str(x).strip()
    return re.sub(r"\.0$", "", s)


def _norm_ar(x: str) -> str:
    return re.sub(r"\s+", "", str(x)).upper()


def build(write_db: bool = True) -> Dataset:
    master = _read("property_master.csv")
    raw_b = _read("bookings.csv")
    raw_r = _read("rate_pace.csv")
    findings: list[DQ.Finding] = []

    # ---- crosswalk ---------------------------------------------------
    findings += DQ.check_master(master)
    master["pms_key"] = master.pms_property_code.map(_norm_pms)
    master["ar_key"] = master.ar_hotel_id.map(_norm_ar)
    pms_x = master.groupby("pms_key")["hotel_id"].apply(list).to_dict()
    ar_x = master.groupby("ar_key")["hotel_id"].apply(list).to_dict()

    def resolve(key, xwalk):
        hits = xwalk.get(key, [])
        if len(hits) == 1:
            return hits[0], ""
        return None, "ambiguous_crosswalk" if len(hits) > 1 else "no_crosswalk_match"

    # ---- bookings ----------------------------------------------------
    b = raw_b.copy()
    norm_changes = {"bookings.pms_property_code": int((b.pms_property_code != b.pms_property_code.map(_norm_pms)).sum())}
    b["pms_key"] = b.pms_property_code.map(_norm_pms)
    res = b.pms_key.map(lambda k: resolve(k, pms_x))
    b["hotel_id"] = res.map(lambda t: t[0])
    b["quarantine_reason"] = res.map(lambda t: t[1])
    for c in ("arrival_date", "departure_date"):
        b[c + "_p"] = pd.to_datetime(b[c], errors="coerce")
    bad_dates = b.arrival_date_p.isna() | b.departure_date_p.isna() | (b.departure_date_p <= b.arrival_date_p)
    b.loc[bad_dates & (b.quarantine_reason == ""), "quarantine_reason"] = "invalid_dates"

    # same booking_id twice: identical copy -> exclude later copy; conflicting copy -> quarantine
    b["dq_excluded"] = False
    b["dup_of"] = ""
    raw_cols = list(raw_b.columns)
    seen = {}
    for i, row in b.iterrows():
        bid = row.booking_id
        if bid not in seen:
            seen[bid] = i
            continue
        if all(row[c] == b.loc[seen[bid], c] for c in raw_cols):
            b.loc[i, "dq_excluded"] = True
            b.loc[i, "dup_of"] = bid
        elif b.loc[i, "quarantine_reason"] == "":
            b.loc[i, "quarantine_reason"] = "conflicting_duplicate_booking_id"

    q_b = b[b.quarantine_reason != ""].copy()
    ok = b[b.quarantine_reason == ""].copy()

    ok["adr_inr"] = pd.to_numeric(ok.adr_inr, errors="coerce").astype("Int64")
    ok["adults"] = pd.to_numeric(ok.adults, errors="coerce").astype("Int64")
    ok["stay_count"] = pd.to_numeric(ok.stay_count, errors="coerce").astype("Int64")
    ok["created_at_ist"] = ok.booking_created_at
    ok["created_ts"] = pd.to_datetime(ok.booking_created_at, errors="coerce")
    as_of_naive = pd.Timestamp(C.AS_OF.replace(tzinfo=None))
    ok["created_after_as_of"] = ok.created_ts > as_of_naive
    ok["is_group"] = ok.rate_plan.eq("GROUP-CONTRACT")
    txt = (ok.special_request + " | " + ok.pms_notes)
    ok["group_code"] = txt.map(lambda s: (BLOCK_RE.search(s) or [None, None])[1])
    ok["contracted_on"] = txt.map(lambda s: (lambda m: pd.to_datetime(m.group(1), format="%d-%b-%Y").date().isoformat() if m else None)(CONTRACT_RE.search(s)))
    # PII: mask / drop / scrub
    ok["guest_name_masked"] = ok.guest_name.map(mask_name)
    ok["guest_phone_masked"] = ok.guest_phone.map(mask_phone)
    ok["special_request"] = [scrub_text(t, [n]) for t, n in zip(ok.special_request, ok.guest_name)]
    ok["pms_notes"] = [scrub_text(t, [n]) for t, n in zip(ok.pms_notes, ok.guest_name)]
    ok["arrival_date"] = ok.arrival_date_p.dt.date.astype(str)
    ok["departure_date"] = ok.departure_date_p.dt.date.astype(str)

    keep_b = ["booking_id", "hotel_id", "guest_id", "guest_name_masked", "guest_phone_masked", "channel", "room_type", "rate_plan",
              "adr_inr", "arrival_date", "departure_date", "adults", "status", "room_no", "created_at_ist", "created_after_as_of",
              "special_request", "pms_notes", "loyalty_tier", "stay_count", "is_group", "group_code", "contracted_on", "dq_excluded", "dup_of"]
    bookings = ok[keep_b].reset_index(drop=True)
    pii = pii_values(raw_b)

    # ---- rate pace ---------------------------------------------------
    r = raw_r.copy()
    norm_changes["rate_pace.ar_hotel_id"] = int((r.ar_hotel_id != r.ar_hotel_id.map(_norm_ar)).sum())
    r["ar_hotel_id"] = r.ar_hotel_id.map(_norm_ar)
    res = r.ar_hotel_id.map(lambda k: resolve(k, ar_x))
    r["hotel_id"] = res.map(lambda t: t[0])
    r["quarantine_reason"] = res.map(lambda t: t[1])
    dup_r = r.duplicated(["ar_hotel_id", "stay_date"], keep="first")
    r.loc[dup_r & (r.quarantine_reason == ""), "quarantine_reason"] = "duplicate_hotel_stay_date"
    q_r = r[r.quarantine_reason != ""].copy()
    rate = r[r.quarantine_reason == ""].drop(columns="quarantine_reason").copy()
    for c in ("bar_inr", "comp_set_avg_inr", "rooms_available", "rooms_sold", "pickup_last_7d", "forecast_rooms",
              "group_block_rooms", "group_rate_inr", "channel_mix_ota_pct"):
        rate[c] = pd.to_numeric(rate[c]).astype(int)
    rate["occupancy_pct"] = pd.to_numeric(rate.occupancy_pct).astype(float)
    rate = rate.reset_index(drop=True)

    # ---- booking-night grain ----------------------------------------
    live = bookings[~bookings.dq_excluded]
    rows = []
    for x in live.itertuples():
        for d in pd.date_range(x.arrival_date, pd.Timestamp(x.departure_date) - pd.Timedelta(days=1)):
            rows.append((x.hotel_id, d.date().isoformat(), x.booking_id, x.status, bool(x.is_group), bool(x.created_after_as_of),
                         x.channel, x.rate_plan, int(x.adr_inr), x.created_at_ist))
    nights = pd.DataFrame(rows, columns=["hotel_id", "stay_date", "booking_id", "status", "is_group", "created_after_as_of",
                                         "channel", "rate_plan", "adr_inr", "created_at_ist"])
    nights["created_at"] = pd.to_datetime(nights.created_at_ist)

    # ---- findings ----------------------------------------------------
    findings += DQ.check_resolution(len(raw_b), len(bookings), len(raw_r), len(rate), norm_changes, q_b, q_r)
    findings += DQ.check_duplicates(bookings)
    findings += DQ.check_status_and_text(raw_b, bookings)
    findings += DQ.check_late_bookings(bookings)
    recon, f2 = DQ.reconcile_rate_vs_pms(master, rate, nights, bookings)
    findings += f2
    findings += DQ.check_events_and_groups(rate, bookings, master)

    ds = Dataset(master.drop(columns=["pms_key", "ar_key"]), bookings, q_b.drop(columns=["guest_name", "guest_phone", "guest_email"], errors="ignore"),
                 rate, q_r, nights, recon, findings, raw_pii=pii)
    if write_db:
        write_sqlite(ds)
    return ds


def finalise_findings(findings: list[DQ.Finding]) -> list[DQ.Finding]:
    order = {"high": 0, "warn": 1, "info": 2}
    findings = sorted(findings, key=lambda f: (order[f.severity], f.category))
    for i, f in enumerate(findings, 1):
        f.id = f"DQ-{i:03d}"
    return findings


def write_sqlite(ds: Dataset) -> None:
    C.OUT_DIR.mkdir(parents=True, exist_ok=True)
    if C.DB_PATH.exists():
        C.DB_PATH.unlink()
    con = sqlite3.connect(C.DB_PATH)
    m = ds.master.copy()
    m["rooms"] = m.rooms.astype(int)
    m.to_sql("hotels", con, index=False)
    ds.bookings.assign(adr_inr=ds.bookings.adr_inr.astype(int), is_group=ds.bookings.is_group.astype(int),
                       created_after_as_of=ds.bookings.created_after_as_of.astype(int),
                       dq_excluded=ds.bookings.dq_excluded.astype(int)).to_sql("bookings", con, index=False)
    ds.bookings_quarantine.to_sql("bookings_quarantine", con, index=False)
    ds.rate.to_sql("rate_pace", con, index=False)
    ds.rate_quarantine.to_sql("rate_pace_quarantine", con, index=False)
    ds.nights.drop(columns="created_at").assign(is_group=ds.nights.is_group.astype(int),
                                                 created_after_as_of=ds.nights.created_after_as_of.astype(int)).to_sql("booking_nights", con, index=False)
    pd.DataFrame([f.to_dict() | {"refs": json.dumps(f.refs)} for f in ds.findings]).to_sql("dq_findings", con, index=False)
    con.executescript("""
    CREATE VIEW v_hotel_date AS
    SELECT r.hotel_id, h.hotel_name, h.brand, h.city, h.rooms AS capacity, r.ar_hotel_id, r.stay_date,
           r.bar_inr, r.comp_set_avg_inr, r.rooms_sold, h.rooms - r.rooms_sold AS remaining, r.occupancy_pct,
           r.pickup_last_7d, r.forecast_rooms, r.restrictions, r.group_block_rooms, r.group_rate_inr,
           r.channel_mix_ota_pct, r.event_flag, r.notes,
           (SELECT COUNT(*) FROM booking_nights n WHERE n.hotel_id=r.hotel_id AND n.stay_date=r.stay_date) AS pms_rooms_all,
           (SELECT COUNT(*) FROM booking_nights n WHERE n.hotel_id=r.hotel_id AND n.stay_date=r.stay_date AND n.created_after_as_of=0) AS pms_rooms_as_of,
           (SELECT COUNT(*) FROM booking_nights n WHERE n.hotel_id=r.hotel_id AND n.stay_date=r.stay_date AND n.is_group=1) AS pms_group_rooms
    FROM rate_pace r JOIN hotels h USING (hotel_id);
    """)
    con.commit()
    con.close()
