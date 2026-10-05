"""tfa_core.domain.columns — the persistence column contract.

Which fields a document and its lines expose to storage is an application
concern; the SQL adapter is only responsible for *executing* statements over
them. Keeping the lists here means the mapper and its tests do not need a
database module in scope, and `db/migrations/` remains the schema authority
(a test asserts every name here exists there).
"""
from __future__ import annotations

DOCUMENT_COLUMNS = [
    "document_id", "identifier_name", "file_name", "blob_container", "blob_path",
    "content_md5", "uploading_person_name", "source_system", "document_type",
    "contains_multiple_docs", "unreadable_reason", "vendor_name", "vendor_address",
    "country", "supplier_tax_id", "customer_name", "customer_tax_id", "e_invoice_id",
    "invoice_number", "booking_number", "reservation_number", "gds_record_locator",
    "references_invoice_no", "folio_date", "guest_name", "travel_route",
    "checkin_date", "checkin_time", "checkout_date", "checkout_time",
    "number_of_nights", "currency", "total_amount", "total_amount_from_words",
    "informational_tax_amt", "year_month", "reconciliation_year",
    "reconciliation_quarter", "matched_hotel_name", "negotiated_rate_usd",
    "fx_usd_per_unit", "fx_rate_date", "rate_issue_found", "nights_overcharged",
    "nights_undercharged", "usd_total_overcharged", "usd_total_undercharged",
    "usd_net_difference", "review_status", "review_reason", "low_confidence_fields",
    "extraction_model", "extraction_provider", "prompt_version",
    "extraction_attempts", "ocr_confidence_floor", "correlation_id",
]

LINE_COLUMNS = [
    "document_id", "line_no", "line_kind", "category", "stay_date", "nights",
    "description", "english_description", "amount", "per_night_amount",
    "usd_per_night_amount", "negotiated_rate_usd", "variance_usd", "is_overcharged",
]
