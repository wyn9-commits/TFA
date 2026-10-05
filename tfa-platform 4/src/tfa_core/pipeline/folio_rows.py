"""tfa_core.pipeline.folio_rows — domain objects -> persistence rows.

This mapping lived inside the SQL adapter, which meant a pure, heavily
business-specific transformation (which extraction fields become which columns,
how nightly and ancillary lines are numbered, which record carries the audit
rollup) could only be tested by importing a database module.

It is application-layer work: it knows the domain deeply and the storage
technology not at all — it emits plain dicts. Keeping it here lets the SQL
adapter stay a thin executor of statements, and lets the mapping be unit-tested
with no driver installed.
"""
from __future__ import annotations

import logging
from typing import Any, Optional, Sequence

from tfa_core.domain.models import FolioExtraction, FolioRecord
from tfa_core.domain.reconciliation import derive_year_and_quarter
from tfa_core.domain.columns import DOCUMENT_COLUMNS, LINE_COLUMNS

logger = logging.getLogger(__name__)


def build_rows(
    *,
    extraction: FolioExtraction,
    records: Sequence[FolioRecord],
    blob_container: str,
    blob_path: str,
    content_md5: str,
    source_system: str = "sharepoint",
    extraction_model: Optional[str] = None,
    extraction_provider: Optional[str] = None,
    prompt_version: Optional[str] = None,
    extraction_attempts: Optional[int] = None,
    ocr_confidence_floor: Optional[float] = None,
    correlation_id: Optional[str] = None,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Maps the domain objects onto `documents` + `folio_lines` rows.

    `records` (one per nightly charge, already reconciled) carries the audit
    rollup; `extraction` carries the raw document facts. Splitting the mapping
    out of the store keeps SQL knowledge in one place and the mapping testable.
    """
    first = records[0]
    year, quarter = derive_year_and_quarter(first.year_month)

    header: dict[str, Any] = {
        "document_id": first.document_id,
        "identifier_name": first.identifier_name,
        "file_name": first.file_name,
        "blob_container": blob_container,
        "blob_path": blob_path,
        "content_md5": content_md5,
        "uploading_person_name": first.uploading_person_name,
        "source_system": source_system,
        "document_type": extraction.document_type.value,
        "contains_multiple_docs": int(extraction.contains_multiple_documents),
        "unreadable_reason": extraction.unreadable_reason,
        "vendor_name": extraction.hotel_name,
        "vendor_address": extraction.hotel_address,
        "country": extraction.country,
        "supplier_tax_id": extraction.supplier_tax_id,
        "customer_name": extraction.customer_name,
        "customer_tax_id": extraction.customer_tax_id,
        "e_invoice_id": extraction.e_invoice_id,
        "invoice_number": extraction.invoice_number,
        "booking_number": extraction.booking_number,
        "reservation_number": extraction.reservation_number,
        "gds_record_locator": extraction.gds_record_locator,
        "references_invoice_no": extraction.references_invoice_number,
        "folio_date": extraction.folio_date,
        "guest_name": extraction.guest_name,
        "travel_route": extraction.travel_route,
        "checkin_date": extraction.checkin_date,
        "checkin_time": extraction.checkin_time,
        "checkout_date": extraction.checkout_date,
        "checkout_time": extraction.checkout_time,
        "number_of_nights": first.number_of_days,
        "currency": extraction.currency,
        "total_amount": extraction.total_amount,
        "total_amount_from_words": extraction.total_amount_from_words,
        "informational_tax_amt": extraction.informational_tax_amount,
        "year_month": first.year_month,
        "reconciliation_year": int(year) if year else None,
        "reconciliation_quarter": quarter,
        "matched_hotel_name": first.matched_hotel_name,
        "negotiated_rate_usd": first.negotiated_rate,
        "fx_usd_per_unit": first.currency_multiplier_rate,
        "fx_rate_date": extraction.folio_date,
        "rate_issue_found": int(first.issue_found.value == "Yes"),
        "nights_overcharged": first.number_of_nights_overcharged,
        "nights_undercharged": first.number_of_nights_undercharged,
        "usd_total_overcharged": first.usd_total_overcharged_amount,
        "usd_total_undercharged": first.usd_total_undercharged_amount,
        "usd_net_difference": first.usd_total_amount_differ,
        "review_status": first.review_status.value,
        "review_reason": first.extraction_warnings,
        "low_confidence_fields": ";".join(extraction.low_confidence_fields) or None,
        "extraction_model": extraction_model,
        "extraction_provider": extraction_provider,
        "prompt_version": prompt_version,
        "extraction_attempts": extraction_attempts,
        "ocr_confidence_floor": ocr_confidence_floor,
        "correlation_id": correlation_id,
    }

    lines: list[dict[str, Any]] = []
    line_no = 0
    negotiated = first.negotiated_rate

    for record, charge in zip(records, extraction.nightly_charges):
        line_no += 1
        usd = record.usd_per_day_price
        variance = (usd - negotiated) if (usd is not None and negotiated is not None) else None
        lines.append({
            "document_id": first.document_id,
            "line_no": line_no,
            "line_kind": "nightly",
            "category": "room",
            "stay_date": charge.stay_date,
            "nights": charge.nights,
            "description": charge.description,
            "english_description": record.english_description,
            "amount": charge.amount,
            "per_night_amount": record.original_per_day_price,
            "usd_per_night_amount": usd,
            "negotiated_rate_usd": negotiated,
            "variance_usd": variance,
            "is_overcharged": int(variance is not None and variance > 0),
        })

    fx = first.currency_multiplier_rate
    for charge in extraction.ancillary_charges:
        line_no += 1
        lines.append({
            "document_id": first.document_id,
            "line_no": line_no,
            "line_kind": "ancillary",
            "category": charge.category,
            "stay_date": None,
            "nights": 1,
            "description": charge.description,
            "english_description": charge.english_description,
            "amount": charge.amount,
            "per_night_amount": None,
            "usd_per_night_amount": (charge.amount * fx) if fx is not None else None,
            "negotiated_rate_usd": None,
            "variance_usd": None,
            "is_overcharged": 0,
        })

    return header, lines
