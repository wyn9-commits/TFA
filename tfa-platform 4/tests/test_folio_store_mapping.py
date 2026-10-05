"""Pins C1: the worker's write path must produce rows the API can read.

`build_rows` is pure, so the domain→schema mapping is testable without SQL.
Column lists are checked against db/migrations so a schema change that forgets
the mapping fails here rather than silently writing nothing readable.
"""
from __future__ import annotations

import re
from datetime import date
from decimal import Decimal
from pathlib import Path

from tfa_core.domain.reconciliation import FxRate, NegotiatedRate, build_records
from tfa_core.domain.models import AncillaryCharge, FolioExtraction, NightlyCharge
from tfa_core.adapters.sql_store import (
    DOCUMENT_COLUMNS,
    LINE_COLUMNS,
    build_rows,
)

MIGRATION = Path(__file__).resolve().parents[1] / "db" / "migrations" / "001_initial_schema.sql"


def _extraction():
    return FolioExtraction(
        document_type="hotel_folio",
        hotel_name="ECO BOX INT GROUP S.A.",
        country="Argentina",
        currency="ARS",
        invoice_number="00003-00001507",
        checkin_date=date(2025, 9, 29),
        checkout_date=date(2025, 10, 2),
        nightly_charges=[NightlyCharge(
            description="Habitación single durante tres noches",
            english_description="Single room for three nights",
            amount=Decimal("773102.88"), nights=3)],
        ancillary_charges=[AncillaryCharge(
            description="Consumos", english_description="Consumptions",
            amount=Decimal("91476.00"), category="food_beverage")],
        total_amount=Decimal("864578.88"),
        informational_tax_amount=Decimal("150050.88"),
    )


def _rows():
    x = _extraction()
    records = build_records(
        x, file_name="factura.pdf", identifier_name="ar_batch",
        uploading_person_name="Ops", blob_md5="abc",
        rate=NegotiatedRate(hotel_name="ECO BOX INT GROUP S.A.", country="Argentina",
                            nightly_rate_usd=Decimal("180")),
        fx=FxRate(currency="ARS", usd_per_unit=Decimal("0.0008")),
        needs_review=False, warnings=[],
    )
    return build_rows(
        extraction=x, records=records, blob_container="travel-folios",
        blob_path="ar_batch/factura.pdf", content_md5="abc",
        extraction_model="gpt-5.6", extraction_provider="azure_openai",
        prompt_version="2025-08-v1", extraction_attempts=1,
        ocr_confidence_floor=0.99,
    )


def _declared_columns(table: str) -> set[str]:
    sql = MIGRATION.read_text()
    body = sql.split(f"CREATE TABLE dbo.{table} (", 1)[1].split("\n);", 1)[0]
    cols = set()
    for line in body.splitlines():
        m = re.match(r"\s{4}(\w+)\s+[A-Z]", line)
        if m:
            cols.add(m.group(1))
    return cols


def test_document_columns_exist_in_migration():
    declared = _declared_columns("documents")
    missing = set(DOCUMENT_COLUMNS) - declared
    assert not missing, f"columns not in dbo.documents: {sorted(missing)}"


def test_line_columns_exist_in_migration():
    declared = _declared_columns("folio_lines")
    missing = set(LINE_COLUMNS) - declared
    assert not missing, f"columns not in dbo.folio_lines: {sorted(missing)}"


def test_header_carries_document_facts_and_audit_rollup():
    header, _ = _rows()
    assert header["vendor_name"] == "ECO BOX INT GROUP S.A."
    assert header["document_type"] == "hotel_folio"
    assert header["year_month"] == "2025-10"
    assert header["reconciliation_year"] == 2025
    assert header["reconciliation_quarter"] == "2025-Q4"
    assert header["informational_tax_amt"] == Decimal("150050.88")
    assert header["rate_issue_found"] == 1
    assert header["nights_overcharged"] == 3


def test_provenance_recorded_for_ai_governance():
    header, _ = _rows()
    assert header["extraction_model"] == "gpt-5.6"
    assert header["prompt_version"] == "2025-08-v1"
    assert header["extraction_attempts"] == 1
    assert header["ocr_confidence_floor"] == 0.99


def test_lines_cover_nightly_and_ancillary_with_stable_numbering():
    _, lines = _rows()
    assert [l["line_kind"] for l in lines] == ["nightly", "ancillary"]
    assert [l["line_no"] for l in lines] == [1, 2]
    nightly = lines[0]
    assert nightly["nights"] == 3
    assert nightly["amount"] == Decimal("773102.88")
    assert nightly["per_night_amount"] == Decimal("257700.96")
    assert nightly["is_overcharged"] == 1


def test_every_line_references_its_document():
    header, lines = _rows()
    assert all(l["document_id"] == header["document_id"] for l in lines)
