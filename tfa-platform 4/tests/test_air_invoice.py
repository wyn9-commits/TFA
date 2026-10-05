"""Validated against the real sample: Wings Global Travel factura WGT000032
(Clic Air, BOG/EJA, 1/08/2025) — the numbers below are from that document."""
from datetime import date
from decimal import Decimal

from tfa_core.domain.reconciliation import build_records
from tfa_core.domain.validation import validate_extraction
from tfa_core.domain.models import AncillaryCharge, FolioExtraction, RateIssue


def _sample_invoice(**overrides):
    base = dict(
        document_type="air_ticket_invoice",
        hotel_name="WINGS GLOBAL TRAVEL S A S",
        country="Colombia",
        supplier_tax_id="901887383-0",
        customer_name="HALLIBURTON LATIN AMERICA S R L SUCURSAL COLOMBIA",
        customer_tax_id="860051812",
        invoice_number="WGT000032",
        folio_date=date(2025, 8, 1),
        guest_name="JHONNY ALEJANDRO GUTIERREZ",
        travel_route="BOG/EJA",
        checkin_date=date(2025, 7, 23),
        checkout_date=date(2025, 7, 23),
        currency="COP",
        nightly_charges=[],
        ancillary_charges=[
            AncillaryCharge(description="Tarifa", english_description="Fare",
                            amount=Decimal("260000.00"), category="airfare"),
            AncillaryCharge(description="Iva(19,00 %)", english_description="VAT (19%)",
                            amount=Decimal("49400.00"), category="tax"),
            AncillaryCharge(description="Tasa Aer", english_description="Airport tax",
                            amount=Decimal("22600.00"), category="airport_tax"),
            AncillaryCharge(description="Tar Admin", english_description="Admin fee",
                            amount=Decimal("43800.00"), category="admin_fee"),
            AncillaryCharge(description="Iva de TA", english_description="VAT on admin fee",
                            amount=Decimal("8322.00"), category="tax"),
        ],
        total_amount=Decimal("384122.00"),
        total_amount_in_words="TRESCIENTOS OCHENTA Y CUATRO MIL CIENTO VEINTIDOS PESOS 00/100 M/cte",
        total_amount_from_words=Decimal("384122.00"),
    )
    base.update(overrides)
    return FolioExtraction(**base)


def test_sample_invoice_sums_and_passes():
    r = validate_extraction(_sample_invoice())
    assert not r.fatal
    assert not r.needs_review  # same-day flight, no nights expected


def test_words_total_mismatch_is_fatal():
    r = validate_extraction(_sample_invoice(total_amount_from_words=Decimal("384000")))
    assert any("written in words" in m for m in r.fatal)


def test_no_nightly_charges_ok_for_air_but_flagged_for_hotel():
    air = validate_extraction(_sample_invoice())
    assert not air.needs_review
    hotel = validate_extraction(_sample_invoice(document_type="hotel_folio"))
    assert hotel.needs_review


def test_air_invoice_builds_header_record_without_rate_audit():
    recs = build_records(
        _sample_invoice(), file_name="0032_01-08-2025.pdf",
        identifier_name="2025_08_batch", uploading_person_name="Ops",
        blob_md5="xyz", rate=None, fx=None, needs_review=False, warnings=[],
    )
    assert len(recs) == 1               # header row, no nightly lines
    assert recs[0].document_type == "air_ticket_invoice"
    assert recs[0].travel_route == "BOG/EJA"
    assert recs[0].e_invoice_id is None
    assert recs[0].issue_found == RateIssue.NONE
    assert recs[0].customer_tax_id == "860051812"
