from datetime import date
from decimal import Decimal

from tfa_core.domain.reconciliation import (
    FxRate, NegotiatedRate, RateDirectory, build_records, derive_year_and_quarter,
)
from tfa_core.domain.models import FolioExtraction, NightlyCharge, RateIssue, ReviewStatus


def _extraction():
    return FolioExtraction(
        document_type="hotel_folio",
        hotel_name="Hilton Buenos Aires",
        country="Argentina",
        currency="ARS",
        checkin_date=date(2026, 2, 10),
        checkout_date=date(2026, 2, 12),
        nightly_charges=[
            NightlyCharge(description="Habitación", english_description="Room", amount=Decimal("150000")),
            NightlyCharge(description="Habitación", english_description="Room", amount=Decimal("110000")),
        ],
        total_amount=Decimal("260000"),
    )


def _records(rate_usd="120"):
    directory = RateDirectory([
        NegotiatedRate(hotel_name="Hilton Buenos Aires", country="Argentina",
                       nightly_rate_usd=Decimal(rate_usd)),
    ])
    rate = directory.match("hilton buenos aires", "Argentina")
    fx = FxRate(currency="ARS", usd_per_unit=Decimal("0.001"))
    return build_records(
        _extraction(), file_name="f.pdf", identifier_name="2026_02_12_batch1",
        uploading_person_name="Test_User", blob_md5="abc",
        rate=rate, fx=fx, needs_review=False, warnings=[],
    )


def test_overcharge_detected_per_night():
    recs = _records(rate_usd="120")
    # Night 1: 150 USD vs 120 → +30 over. Night 2: 110 vs 120 → 10 under.
    assert recs[0].issue_found == RateIssue.OVERCHARGED
    assert recs[0].overcharged_days == "Day-1"
    assert recs[0].undercharged_days == "Day-2"
    assert recs[0].usd_total_overcharged_amount == Decimal("30.00")
    assert recs[0].usd_total_undercharged_amount == Decimal("10.00")
    assert recs[0].usd_total_amount_differ == Decimal("20.00")


def test_one_record_per_night_plus_shared_header():
    recs = _records()
    assert len(recs) == 2
    assert recs[0].year_month == "2026-02"
    assert recs[0].number_of_days == 2
    assert recs[0].review_status == ReviewStatus.AUTO_APPROVED
    assert recs[0].document_id == recs[1].document_id


def test_fuzzy_hotel_match():
    directory = RateDirectory([
        NegotiatedRate(hotel_name="Hilton Buenos Aires Hotel & Residences",
                       country="Argentina", nightly_rate_usd=Decimal("120")),
    ])
    assert directory.match("HILTON BUENOS AIRES HOTEL RESIDENCES", "Argentina") is not None


def test_year_quarter_derivation():
    assert derive_year_and_quarter("2026-02") == ("2026", "2026-Q1")
    assert derive_year_and_quarter("2025-11") == ("2025", "2025-Q4")
    assert derive_year_and_quarter(None) == (None, None)
