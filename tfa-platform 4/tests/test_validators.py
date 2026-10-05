from datetime import date
from decimal import Decimal

from tfa_core.domain.validation import validate_extraction
from tfa_core.domain.models import AncillaryCharge, FolioExtraction, NightlyCharge


def _folio(**overrides):
    base = dict(
        document_type="hotel_folio",
        hotel_name="Hotel Test",
        currency="ARS",
        checkin_date=date(2026, 3, 1),
        checkout_date=date(2026, 3, 4),
        nightly_charges=[
            NightlyCharge(description="Habitación", english_description="Room", amount=Decimal("100")),
            NightlyCharge(description="Habitación", english_description="Room", amount=Decimal("100")),
            NightlyCharge(description="Habitación", english_description="Room", amount=Decimal("100")),
        ],
        ancillary_charges=[
            AncillaryCharge(description="IVA", english_description="VAT", amount=Decimal("63"), category="tax"),
        ],
        total_amount=Decimal("363"),
    )
    base.update(overrides)
    return FolioExtraction(**base)


def test_clean_folio_passes():
    r = validate_extraction(_folio())
    assert not r.fatal and not r.needs_review


def test_total_mismatch_is_fatal():
    r = validate_extraction(_folio(total_amount=Decimal("500")))
    assert r.fatal and "sum to" in r.fatal[0]


def test_rounding_within_tolerance_passes():
    r = validate_extraction(_folio(total_amount=Decimal("363.90")))
    assert not r.fatal


def test_inverted_dates_are_fatal():
    r = validate_extraction(_folio(checkin_date=date(2026, 3, 4), checkout_date=date(2026, 3, 1)))
    assert any("before checkin" in m for m in r.fatal)


def test_night_count_mismatch_needs_review():
    f = _folio()
    f.nightly_charges.pop()
    f.total_amount = Decimal("263")
    r = validate_extraction(f)
    assert not r.fatal and r.needs_review


def test_low_confidence_critical_field_needs_review():
    r = validate_extraction(_folio(low_confidence_fields=["total_amount"]))
    assert r.needs_review


def test_missing_total_needs_review_not_fatal():
    r = validate_extraction(_folio(total_amount=None))
    assert not r.fatal and r.needs_review
