import pandas as pd

from conftest import HYD, NOV

from revintel import config as C
from revintel import ingest
from revintel.pii import mask_name, mask_phone


def test_real_data_resolves_and_reports(ds):
    assert len(ds.bookings) == 1677 and len(ds.bookings_quarantine) == 0
    assert len(ds.rate) == 40 and len(ds.rate_quarantine) == 0
    assert set(ds.bookings.hotel_id) == set(ds.master.hotel_id)
    cats = {(f.category, f.severity) for f in ds.findings}
    assert ("over_capacity", "high") in cats and ("time_leakage", "warn") in cats and ("possible_duplicate", "warn") in cats


def test_findings_name_the_records_and_what_was_done(ds):
    late = next(f for f in ds.findings if f.category == "time_leakage")
    assert "87" in late.summary and late.refs and late.action_taken
    over = next(f for f in ds.findings if f.category == "over_capacity")
    assert over.hotel_id == NOV and "95" in over.summary and "92" in over.summary
    assert all(f.action_taken for f in ds.findings)


def test_group_block_is_detected_and_excluded_from_transient(ds):
    g = ds.bookings[ds.bookings.is_group]
    assert len(g) == 40 and set(g.group_code) == {"ZT-OFF-26"} and set(g.hotel_id) == {NOV}


def test_possible_duplicates_are_flagged_not_merged(ds):
    pairs = [f for f in ds.findings if f.category == "possible_duplicate"]
    assert len(pairs) == 2
    assert all("NOT merged" in f.action_taken for f in pairs)
    assert not ds.bookings.dq_excluded.any()


def test_unresolved_and_duplicate_records_are_quarantined_never_dropped(tmp_path, monkeypatch):
    """Feed deliberately broken data: nothing may vanish, nothing may be guessed."""
    d = tmp_path / "data"
    d.mkdir()
    for f in ("property_master.csv", "bookings.csv", "rate_pace.csv"):
        (d / f).write_text((C.DATA_DIR / f).read_text())
    b = pd.read_csv(d / "bookings.csv", dtype=str, keep_default_na=False)
    unknown = b.iloc[0].copy(); unknown["booking_id"] = "BK-X1"; unknown["pms_property_code"] = "9999"       # unknown PMS code
    clash = b.iloc[1].copy(); clash["adr_inr"] = "1"                                                          # same id, different content
    exact = b.iloc[2].copy()                                                                                  # identical copy
    baddate = b.iloc[3].copy(); baddate["booking_id"] = "BK-X2"; baddate["departure_date"] = baddate["arrival_date"]
    b2 = pd.concat([b, pd.DataFrame([unknown, clash, exact, baddate])], ignore_index=True)
    b2.to_csv(d / "bookings.csv", index=False)
    r = pd.read_csv(d / "rate_pace.csv", dtype=str, keep_default_na=False)
    spaced = r.iloc[0].copy(); spaced["ar_hotel_id"] = " ar11042 "; spaced["stay_date"] = "2026-09-10"       # same hotel, messy format
    ghost = r.iloc[1].copy(); ghost["ar_hotel_id"] = "AR99999"                                                # no such hotel
    dup = r.iloc[2].copy()                                                                                    # same hotel+date twice
    pd.concat([r, pd.DataFrame([spaced, ghost, dup])], ignore_index=True).to_csv(d / "rate_pace.csv", index=False)

    monkeypatch.setattr(C, "DATA_DIR", d)
    out = ingest.build(write_db=False)
    assert len(out.bookings) + len(out.bookings_quarantine) == len(b2)                 # no silent drop
    assert len(out.rate) + len(out.rate_quarantine) == len(r) + 3
    reasons = set(out.bookings_quarantine.quarantine_reason)
    assert {"no_crosswalk_match", "conflicting_duplicate_booking_id", "invalid_dates"} <= reasons
    assert out.bookings.dq_excluded.sum() == 1                                          # identical copy excluded from counts, kept in table
    assert set(out.rate_quarantine.quarantine_reason) == {"no_crosswalk_match", "duplicate_hotel_stay_date"}
    assert (out.rate.ar_hotel_id == "AR11042").sum() == 11                              # whitespace/case normalised, not guessed
    assert any(f.category == "unresolved_key" and f.severity == "high" for f in out.findings)
    assert "BK-X1" in " ".join(" ".join(f.refs) for f in out.findings if f.category == "unresolved_key")


def test_pii_masking_formats():
    assert mask_phone("+91 9845012277") == "+91 ****012277"        # format from business_rules section 8
    assert mask_name("Rohit Balakrishnan") == "R***t B" + "*" * 10 + "n"


def test_stored_view_has_no_raw_pii(ds):
    assert "guest_email" not in ds.bookings.columns and "guest_name" not in ds.bookings.columns
    assert ds.bookings.guest_phone_masked.str.startswith("+91 ****").all()
