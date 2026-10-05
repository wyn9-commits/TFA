"""tfa_core.domain.models
=================
SINGLE SOURCE OF TRUTH for the folio data model.

The legacy app kept three drifting copies of the column list (app.py Excel map,
script.js sqlColumnOrder, and the implicit SQL schema). Everything now derives
from these Pydantic models:

- The LLM structured-output JSON schema (FolioExtraction.model_json_schema())
- SQL column names + Excel export headers (FOLIO_COLUMNS)
- Validation rules that gate persistence (see extraction/validators.py)
"""
from __future__ import annotations

from datetime import date, time
from decimal import Decimal
from enum import Enum
from typing import Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator


# --------------------------------------------------------------------------- #
# Stage-2 LLM extraction target (what the model must return, per document)
# --------------------------------------------------------------------------- #

class NightlyCharge(BaseModel):
    """One room-night charge line."""
    model_config = ConfigDict(extra="forbid")

    stay_date: Optional[date] = Field(
        None, description="Calendar date of this night if printed on the folio (ISO 8601)."
    )
    description: str = Field(..., description="Original charge description, verbatim, source language.")
    english_description: str = Field(..., description="Faithful English translation of the description.")
    amount: Decimal = Field(
        ...,
        description="TOTAL amount of this charge line in the folio's original "
                    "currency (covering all nights of the line).",
    )
    nights: int = Field(
        1, ge=1,
        description="Number of nights this line covers. 1 for per-night lines; "
                    "N when the folio collapses a stay into one line (e.g. "
                    "'Habitación single durante tres noches' -> nights=3).",
    )


class AncillaryCharge(BaseModel):
    """Non-room-night lines: taxes, fees, minibar, late checkout, etc."""
    model_config = ConfigDict(extra="forbid")

    description: str
    english_description: str
    amount: Decimal
    category: str = Field(
        ...,
        description=(
            "One of: tax, fee, late_checkout, early_checkin, food_beverage, "
            "parking, airfare, airport_tax, admin_fee, other"
        ),
    )


class DocumentType(str, Enum):
    HOTEL_FOLIO = "hotel_folio"
    AIR_TICKET_INVOICE = "air_ticket_invoice"     # agency/airline e-invoice for flights
    AGENCY_FEE_INVOICE = "agency_fee_invoice"     # travel-agency service fees only
    AP_SYSTEM_DOCUMENT = "ap_system_document"     # internal AP cover page (FS2/Serrala/SAP) wrapping an invoice
    CREDIT_NOTE = "credit_note"                   # Nota de Crédito / refund reversing a prior invoice
    OTHER_TRAVEL_INVOICE = "other_travel_invoice" # travel-related but fits no type above — extract generically
    NOT_AN_INVOICE = "not_an_invoice"             # no billable content (boarding pass, email, blank scan...)


class FieldConfidence(str, Enum):
    HIGH = "high"        # printed clearly, unambiguous
    MEDIUM = "medium"    # inferred from context or partially legible
    LOW = "low"          # guessed / conflicting evidence — must be human-reviewed


class FolioExtraction(BaseModel):
    """The complete structured record the LLM must emit for one folio document.

    Every Optional field must be null when the information is genuinely absent —
    never invented. `low_confidence_fields` lists any field the model was not
    certain about; documents with low-confidence money/date fields are routed to
    human review instead of silently persisted (root cause of POC bad data).
    """
    model_config = ConfigDict(extra="forbid")

    document_type: DocumentType = Field(
        ...,
        description=(
            "hotel_folio: hotel stay invoice with room-night charges. "
            "air_ticket_invoice: invoice for air travel (fare, airport taxes, "
            "admin fees) — e.g. Colombian 'Factura Electrónica de Venta' from a "
            "travel agency for a flight. agency_fee_invoice: agency service "
            "fees only. other_travel_invoice: anything else travel-related."
        ),
    )

    hotel_name: str = Field(
        ...,
        description=(
            "For hotel folios: the hotel name. For air/agency invoices: the "
            "issuing vendor name (e.g. the travel agency or airline)."
        ),
    )
    hotel_address: Optional[str] = None
    country: Optional[str] = Field(None, description="ISO country name in English, e.g. 'Argentina'.")

    supplier_tax_id: Optional[str] = Field(
        None, description="Issuer's tax ID (NIT / CUIT / RUC / CNPJ) as printed."
    )
    customer_name: Optional[str] = Field(
        None, description="Billed-to company name (e.g. 'HALLIBURTON LATIN AMERICA S R L')."
    )
    customer_tax_id: Optional[str] = Field(None, description="Billed-to tax ID (C.C/NIT).")
    e_invoice_id: Optional[str] = Field(
        None, description="Fiscal e-invoice unique code if present (e.g. Colombian CUFE), verbatim."
    )

    folio_date: Optional[date] = None
    invoice_number: Optional[str] = None
    booking_number: Optional[str] = None
    reservation_number: Optional[str] = Field(None, description="Hotel's own reservation no. (Nro de Reserva).")
    gds_record_locator: Optional[str] = None

    guest_name: Optional[str] = Field(
        None, description="Guest (hotel) or passenger (air) name — 'Pasajero'."
    )
    travel_route: Optional[str] = Field(
        None, description="Air invoices only: route as printed, e.g. 'BOG/EJA'."
    )
    checkin_date: Optional[date] = Field(
        None, description="Hotel check-in, or travel start ('Entrada') for air invoices."
    )
    checkin_time: Optional[time] = None
    checkout_date: Optional[date] = Field(
        None, description="Hotel check-out, or travel end ('Salida') for air invoices."
    )
    checkout_time: Optional[time] = None

    currency: str = Field(..., description="ISO 4217 code as printed, e.g. 'ARS', 'COP', 'USD'.")
    nightly_charges: list[NightlyCharge] = Field(default_factory=list)
    ancillary_charges: list[AncillaryCharge] = Field(default_factory=list)
    total_amount: Optional[Decimal] = Field(None, description="Grand total printed on the folio.")
    total_amount_in_words: Optional[str] = Field(
        None,
        description="If the total is also written in words (e.g. 'SON: TRESCIENTOS "
                    "OCHENTA Y CUATRO MIL CIENTO VEINTIDOS PESOS 00/100'), verbatim.",
    )
    total_amount_from_words: Optional[Decimal] = Field(
        None,
        description="The numeric value of total_amount_in_words, converted by you. "
                    "Null when no written-words total exists.",
    )
    informational_tax_amount: Optional[Decimal] = Field(
        None,
        description="Tax amount that is INCLUDED in the listed charges rather "
                    "than additive — e.g. Argentine Factura B 'IVA Contenido' "
                    "(Ley 27.743). Record it here, NOT as an ancillary charge. "
                    "Null when all taxes are additive line items.",
    )
    references_invoice_number: Optional[str] = Field(
        None,
        description="Credit notes only: the number of the original invoice "
                    "this document reverses/adjusts, if printed.",
    )
    contains_multiple_documents: bool = Field(
        False,
        description="True when the file contains MORE THAN ONE distinct "
                    "invoice/folio (e.g. several invoices scanned together). "
                    "Extract ONLY the FIRST document and set this flag; the "
                    "file will be routed to human review for splitting.",
    )
    unreadable_reason: Optional[str] = Field(
        None,
        description="For not_an_invoice OR documents too illegible to extract "
                    "reliably: one sentence saying what the file actually is "
                    "or why it cannot be read.",
    )

    low_confidence_fields: list[str] = Field(
        default_factory=list,
        description="Names of any fields above whose value is a guess (confidence below high).",
    )

    @field_validator("currency")
    @classmethod
    def _upper_currency(cls, v: str) -> str:
        return v.strip().upper()


# --------------------------------------------------------------------------- #
# Post-reconciliation record (extraction + rate-audit results) → SQL row
# --------------------------------------------------------------------------- #

class RateIssue(str, Enum):
    NONE = "No"
    OVERCHARGED = "Yes"


class ReviewStatus(str, Enum):
    AUTO_APPROVED = "auto_approved"
    NEEDS_REVIEW = "needs_review"
    REVIEWED = "reviewed"
    REJECTED = "rejected"


class FolioRecord(BaseModel):
    """One SQL row = one service line, denormalised (matches legacy reporting shape)."""
    model_config = ConfigDict(extra="forbid")

    # Provenance
    file_name: str
    identifier_name: str
    uploading_person_name: str
    document_id: str  # deterministic hash of blob path + content md5

    # Folio header
    folio_hotel_name: str
    hotel_address: Optional[str]
    country: Optional[str]
    folio_date: Optional[date]
    invoice_number: Optional[str]
    booking_number: Optional[str]
    nro_reserv: Optional[str]
    gds_record_locator: Optional[str]
    document_type: str
    supplier_tax_id: Optional[str]
    customer_name: Optional[str]
    customer_tax_id: Optional[str]
    e_invoice_id: Optional[str]
    travel_route: Optional[str]
    guest: Optional[str]
    checkin: Optional[date]
    checkin_timestamp: Optional[str]
    checkout: Optional[date]
    checkout_timestamp: Optional[str]
    currency: str
    number_of_days: Optional[int]
    year_month: Optional[str]  # reconciliation month YYYY-MM (checkout month)

    # Line item
    english_description: Optional[str]
    original_per_day_price: Optional[Decimal]

    # Rate audit
    matched_hotel_name: Optional[str]
    negotiated_rate: Optional[Decimal]
    currency_multiplier_rate: Optional[Decimal]
    usd_per_day_price: Optional[Decimal]
    late_early_english_description: Optional[str]
    usd_late_early_unit_price: Optional[Decimal]
    issue_found: RateIssue = RateIssue.NONE
    overcharged_days: Optional[str]
    undercharged_days: Optional[str]
    number_of_nights_overcharged: int = 0
    number_of_nights_undercharged: int = 0
    usd_total_overcharged_amount: Decimal = Decimal("0")
    usd_total_undercharged_amount: Decimal = Decimal("0")
    usd_total_amount_differ: Decimal = Decimal("0")

    # Quality gates (new)
    review_status: ReviewStatus = ReviewStatus.AUTO_APPROVED
    extraction_warnings: Optional[str] = None  # ';'-joined validator messages


# --------------------------------------------------------------------------- #
# Column registry — SQL column, Excel header, Excel description, all in one place
# --------------------------------------------------------------------------- #

FOLIO_COLUMNS: list[tuple[str, str, str]] = [
    # (sql_column, excel_header, excel_description)
    ("file_name", "File Name", "The original name of the uploaded PDF file."),
    ("folio_hotel_name", "Folio Hotel Name", "The full hotel name as extracted directly from the folio document."),
    ("hotel_address", "Hotel Address", "The full hotel address as extracted directly from the folio document."),
    ("country", "Country", "The country where the hotel is located, as extracted from the folio."),
    ("folio_date", "Folio Date", "The date the folio or invoice was issued."),
    ("invoice_number", "Invoice Number", "The official invoice or folio number."),
    ("booking_number", "Booking Number", "The booking reference number, often an internal number from a travel agency."),
    ("nro_reserv", "Reservation Number (Nro)", "The hotel's specific reservation number (Número de Reserva)."),
    ("gds_record_locator", "GDS Record Locator", "The Global Distribution System (GDS) record locator, a universal travel industry identifier."),
    ("document_type", "Document Type", "hotel_folio, air_ticket_invoice, agency_fee_invoice, or other_travel_invoice."),
    ("supplier_tax_id", "Supplier Tax ID", "Issuer's tax ID (NIT/CUIT/RUC/CNPJ) as printed."),
    ("customer_name", "Billed Customer", "Billed-to company name as printed on the invoice."),
    ("customer_tax_id", "Customer Tax ID", "Billed-to company tax ID (C.C/NIT)."),
    ("e_invoice_id", "E-Invoice ID (CUFE)", "Fiscal e-invoice unique code (e.g. Colombian CUFE), verbatim."),
    ("travel_route", "Route", "Air invoices: route as printed (e.g. BOG/EJA)."),
    ("guest", "Guest Name", "The name of the guest or passenger."),
    ("checkin", "Check-in Date", "The date the guest checked into the hotel."),
    ("checkin_timestamp", "Check-in Timestamp", "The time the guest checked into the hotel."),
    ("checkout", "Check-out Date", "The date the guest checked out of the hotel."),
    ("checkout_timestamp", "Check-out Timestamp", "The time the guest checked out of the hotel."),
    ("currency", "Original Currency", "The currency used for the charges on the folio (e.g., ARS for Argentine Peso)."),
    ("number_of_days", "Number of Days", "The total number of nights for the stay."),
    ("english_description", "Description (English)", "The English translation of the service description."),
    ("year", "Year", "The calendar year of the stay, derived from the reconciliation month."),
    ("year_quarter", "Quarter", "The calendar quarter of the stay (e.g., 2024-Q1), derived from the reconciliation month."),
    ("year_month", "Reconciliation Month", "The primary month used for financial reconciliation, typically the checkout month (YYYY-MM)."),
    ("original_per_day_price", "Original Unit Price", "The price per night in folio currency."),
    ("matched_hotel_name", "Matched Negotiated Hotel", "The official hotel name from our records that was matched to this folio."),
    ("negotiated_rate", "Negotiated Rate (USD)", "The pre-negotiated nightly rate in USD for this hotel, if available."),
    ("currency_multiplier_rate", "Currency Multiplier Rate", "The actual exchange rate applied during processing to convert folio currency to USD."),
    ("usd_per_day_price", "Unit Price (USD)", "The price per night converted to USD."),
    ("late_early_english_description", "Late/Early English Description", "Late Check-out or Early Check-in description if availed by any guest."),
    ("usd_late_early_unit_price", "Late/Early Unit Price (USD)", "Charges for Late Check-out or Early Check-in in USD."),
    ("issue_found", "Rate Issue Found?", "'Yes' if any nightly rate in USD exceeds the negotiated rate; 'No' otherwise."),
    ("overcharged_days", "Overcharged Days", "Days of the stay (e.g., Day-1;Day-3) where the nightly rate was higher than the negotiated rate."),
    ("undercharged_days", "Undercharged Days", "Days of the stay where the nightly rate was lower than the negotiated rate."),
    ("number_of_nights_undercharged", "Number of Undercharged Nights", "The total count of nights where the rate was lower than the negotiated rate."),
    ("number_of_nights_overcharged", "Number of Overcharged Nights", "The total count of nights where the rate was higher than the negotiated rate."),
    ("usd_total_overcharged_amount", "Total Overcharged Amount (USD)", "The total sum of all overcharges for the entire stay, in USD."),
    ("usd_total_undercharged_amount", "Total Undercharged Amount (USD)", "The total sum of all undercharges for the entire stay, in USD."),
    ("usd_total_amount_differ", "Net Difference (USD)", "The final net financial difference for the stay (Overcharged - Undercharged), in USD."),
    ("identifier_name", "Upload Identifier", "The unique batch identifier provided during the file upload process."),
    ("uploading_person_name", "Uploaded By", "The name of the user who uploaded this folio."),
    ("review_status", "Review Status", "auto_approved when all validators passed; needs_review when a human must confirm."),
    ("extraction_warnings", "Extraction Warnings", "Validator messages explaining why a record needs review."),
]

SQL_COLUMNS = [c[0] for c in FOLIO_COLUMNS]
EXCEL_HEADERS = {c[0]: c[1] for c in FOLIO_COLUMNS}
EXCEL_DESCRIPTIONS = {c[1]: c[2] for c in FOLIO_COLUMNS}
