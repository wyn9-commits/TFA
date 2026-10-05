"""Tests built from three real SharePoint samples:
- ECO BOX INT GROUP S.A. (Argentina) Factura B 00001507 — collapsed 3-night
  line + included IVA (Ley 27.743).
- Wings Corporate Travel (US/Ecuador) TAX INVOICE 417953 — USD air ticket,
  English number format.
- Serrala/FS² AP cover page 0014956573 (Mexico) — internal AP document,
  additive tax.
"""
from datetime import date
from decimal import Decimal

from tfa_core.domain.reconciliation import (
    FxRate, NegotiatedRate, RateDirectory, build_records,
)
from tfa_core.domain.validation import validate_extraction
from tfa_core.domain.models import (
    AncillaryCharge, FolioExtraction, NightlyCharge, RateIssue,
)


# --------------------------------------------------------------------------- #
# Argentina — Factura B, collapsed nights, IVA Contenido (included tax)
# --------------------------------------------------------------------------- #

def _argentina_factura_b():
    return FolioExtraction(
        document_type="hotel_folio",
        hotel_name="ECO BOX INT GROUP S.A.",
        hotel_address="Lote 1 Mz 362 S/N 0 - Añelo, Neuquén",
        country="Argentina",
        supplier_tax_id="30716051540",
        customer_name="HALLIBURTON ARGENTINA SOCIEDAD DE RESPONSABILIDAD LIMITADA",
        customer_tax_id="30503610988",
        invoice_number="00003-00001507",
        booking_number="GKFRDQ",
        folio_date=date(2025, 10, 15),
        guest_name="Ruiz, Federico",
        checkin_date=date(2025, 9, 29),
        checkout_date=date(2025, 10, 2),
        currency="ARS",
        nightly_charges=[
            NightlyCharge(
                description="Habitación single durante tres noches",
                english_description="Single room for three nights",
                amount=Decimal("773102.88"),
                nights=3,
            ),
        ],
        ancillary_charges=[
            AncillaryCharge(description="Consumos", english_description="Consumptions",
                            amount=Decimal("91476.00"), category="food_beverage"),
        ],
        total_amount=Decimal("864578.88"),
        informational_tax_amount=Decimal("150050.88"),  # IVA Contenido — NOT additive
    )


def test_argentina_collapsed_nights_and_included_iva_pass():
    r = validate_extraction(_argentina_factura_b())
    # 773102.88 + 91476.00 == 864578.88 without adding IVA Contenido
    assert not r.fatal
    # 3-night stay covered by nights=3 on one line → no review flag
    assert not r.needs_review


def test_argentina_iva_wrongly_added_as_line_fails_sum():
    x = _argentina_factura_b()
    x.ancillary_charges.append(AncillaryCharge(
        description="IVA Contenido", english_description="Included VAT",
        amount=Decimal("150050.88"), category="tax"))
    r = validate_extraction(x)
    assert r.fatal  # sum now exceeds printed total — repair loop corrects the model


def test_argentina_per_night_audit_expands_collapsed_line():
    directory = RateDirectory([NegotiatedRate(
        hotel_name="ECO BOX INT GROUP S.A.", country="Argentina",
        nightly_rate_usd=Decimal("180"))])
    fx = FxRate(currency="ARS", usd_per_unit=Decimal("0.0008"))
    recs = build_records(
        _argentina_factura_b(), file_name="factura_b.pdf",
        identifier_name="ar_batch", uploading_person_name="Ops", blob_md5="m",
        rate=directory.match("ECO BOX INT GROUP S.A.", "Argentina"), fx=fx,
        needs_review=False, warnings=[],
    )
    # One line, expanded to 3 audited nights: per-night ARS 257700.96 → USD 206.16
    rec = recs[0]
    assert rec.original_per_day_price == Decimal("257700.96")
    assert rec.usd_per_day_price == Decimal("206.16")
    assert rec.number_of_nights_overcharged == 3
    assert rec.overcharged_days == "Day-1;Day-2;Day-3"
    assert rec.usd_total_overcharged_amount == Decimal("78.48")  # 3 × 26.16
    assert rec.issue_found == RateIssue.OVERCHARGED
    assert "(3 nights)" in rec.english_description


# --------------------------------------------------------------------------- #
# Ecuador/US — Wings TAX INVOICE, USD, English number format
# --------------------------------------------------------------------------- #

def _wings_usd_invoice():
    return FolioExtraction(
        document_type="air_ticket_invoice",
        hotel_name="Wings Corporate Travel Inc.",
        country="United States",
        supplier_tax_id="20-4738312",
        customer_name="Halliburton Latin America",
        customer_tax_id="1790528782001",
        invoice_number="417953",
        booking_number="2366206",
        folio_date=date(2025, 7, 24),
        guest_name="Aguilar Gonzalez Edgar David",
        travel_route="LIM/BOG/SAL/BOG/LIM",
        checkin_date=date(2025, 7, 28),
        checkout_date=date(2025, 7, 30),
        currency="USD",
        nightly_charges=[],
        ancillary_charges=[
            AncillaryCharge(description="Excl Amount", english_description="Fare (excl. taxes)",
                            amount=Decimal("1056.00"), category="airfare"),
            AncillaryCharge(description="Airport Tax", english_description="Airport tax",
                            amount=Decimal("276.59"), category="airport_tax"),
        ],
        total_amount=Decimal("1332.59"),
    )


def test_wings_usd_invoice_sums_and_passes():
    r = validate_extraction(_wings_usd_invoice())
    assert not r.fatal and not r.needs_review


# --------------------------------------------------------------------------- #
# Mexico — Serrala/FS² AP cover page, additive tax
# --------------------------------------------------------------------------- #

def _serrala_ap_document():
    return FolioExtraction(
        document_type="ap_system_document",
        hotel_name="PROMOTORA TURISTICA INTRA GRIJALVA",
        hotel_address="COL. TABASCO 2000, VILLAHERMOSA - TAB, 86035",
        country="Mexico",
        invoice_number="147869",
        folio_date=date(2025, 10, 2),
        currency="MXN",
        nightly_charges=[],
        ancillary_charges=[
            AncillaryCharge(description="Travel - Accommodations",
                            english_description="Travel - Accommodations",
                            amount=Decimal("5205.00"), category="other"),
            AncillaryCharge(description="Travel - Accommodations",
                            english_description="Travel - Accommodations",
                            amount=Decimal("156.15"), category="other"),
            AncillaryCharge(description="Tax", english_description="Tax",
                            amount=Decimal("832.80"), category="tax"),
        ],
        total_amount=Decimal("6193.95"),
    )


def test_serrala_ap_document_additive_tax_sums():
    r = validate_extraction(_serrala_ap_document())
    # 5205.00 + 156.15 + 832.80 == 6193.95 — additive tax belongs in the lines
    assert not r.fatal and not r.needs_review


def test_ap_document_skips_hotel_nights_logic():
    x = _serrala_ap_document()
    r = validate_extraction(x)
    assert not any("room-night" in w for w in r.warnings)
