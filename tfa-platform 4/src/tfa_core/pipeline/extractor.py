"""tfa_core.pipeline.extractor — Stage 2: schema-validated LLM extraction.

Design for accuracy (each point addresses a POC failure mode):

1. The model receives BOTH the Document Intelligence markdown (deterministic
   OCR incl. tables) AND high-DPI page images. Text anchors numbers exactly;
   images resolve layout ambiguity. Legacy sent one 150-DPI image, no text.
2. Structured outputs: the provider guarantees schema-shaped JSON; Pydantic
   then enforces types (dates parse, Decimals parse). Legacy did raw
   json.loads and hoped.
3. Repair loop: if Pydantic or the cross-field validators reject the result,
   the errors are fed back to the model verbatim for up to N attempts.
   Legacy had "no validation or retries" (its own docstring said so).
4. Anti-hallucination contract: absent fields must be null and uncertain
   fields must be listed in low_confidence_fields; those route to human
   review instead of polluting the reconciliation table.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass

from pydantic import ValidationError

from tfa_core.domain.values import DocumentLayout
from tfa_core.domain.validation import validate_extraction
from tfa_core.domain.ports import StructuredLLM
from tfa_core.domain.models import FolioExtraction

logger = logging.getLogger(__name__)

SYSTEM_PROMPT = """\
You are a meticulous financial-document analyst extracting data from corporate
travel documents for reconciliation. Documents may be in Spanish, Portuguese,
English, or other languages, and come in several types — classify first:

- hotel_folio: hotel stay invoice with room-night charges (includes Argentine
  Factura A/B/C from hotels).
- air_ticket_invoice: an invoice for air travel — e.g. a Colombian "Factura
  Electrónica de Venta" from a travel agency (look for Rutas, Pasajero,
  Tarifa, Tasa Aeroportuaria, CUFE, NIT) or an English "TAX INVOICE" from a
  corporate travel agency (look for Ticket N°, Routing like LIM/BOG/SAL,
  Flight Itinerary, Excl Amount / Airport Tax / VAT columns).
- agency_fee_invoice: agency service fees only.
- ap_system_document: an INTERNAL Accounts Payable cover page from systems
  like FS²/Serrala/SAP/Taulia — look for "FS² Record #", G/L Account Numbers,
  Cost Centers, Company Code, approval History Details tables. Extract the
  vendor as hotel_name, the invoice metadata (number, dates, currency, total,
  tax), and the G/L line amounts as ancillary_charges (category 'other',
  additive tax as 'tax'). Ignore the workflow-history table rows.
- credit_note: a Nota de Crédito / credit memo / refund reversing a prior
  invoice. Record amounts as NEGATIVE numbers, and put the original invoice's
  number (if printed) in references_invoice_number.
- other_travel_invoice: travel-related but fits none of the above — extract
  everything generically (vendor, dates, tax IDs, every amount line as an
  ancillary charge with your best category, the total). Do NOT force it into
  a wrong type; generic-but-correct beats specific-but-wrong.
- not_an_invoice: the file has no billable content you can extract — a
  boarding pass, an email printout, a blank or fully illegible scan, a photo
  of something else. Set unreadable_reason to one sentence saying what the
  file actually is. Fill only the fields you can genuinely read; it will be
  routed to a human.

You are given:
1. OCR_MARKDOWN — machine-extracted text of the ENTIRE document, including
   tables. Numbers and identifiers here are exact; prefer them when the image
   is ambiguous.
2. Page images — use these to understand layout, resolve column alignment in
   tables, and read anything the OCR garbled.

Rules — follow every one:
- Extract values EXACTLY as printed. Never round, reformat, or invent.
- Amounts: use the document's own decimal convention, judged per document.
  Spanish/Portuguese-locale documents: "773.102,88" = 773102.88 and
  "260.000,00" = 260000.00. English-locale documents: "1,056.00" = 1056.00.
- COLLAPSED NIGHTS: hotels sometimes bill a whole stay as ONE line, e.g.
  "Habitación single durante tres noches ... IN 29/09 OUT 02/10" with
  cantidad 1. Keep it as ONE nightly_charges entry with amount = the line
  TOTAL and nights = the number of nights it covers (3 in that example).
  Never invent per-night splits the document doesn't show.
- INCLUDED vs ADDITIVE TAX: if a tax is stated as *contained within* the
  total — e.g. Argentine Factura B "IVA Contenido" under "Régimen de
  Transparencia Fiscal al Consumidor (Ley 27.743)" — put it in
  informational_tax_amount and DO NOT add it as an ancillary charge (the
  line items already include it, and charges must sum to the total).
  Taxes shown as separate additive lines (IVA on a Factura A, VAT columns,
  "Tax:" in an AP header) DO go in ancillary_charges as category 'tax'.
- MULTIPLE DOCUMENTS IN ONE FILE: if the file contains more than one distinct
  invoice/folio (several scanned together), extract ONLY the first one and
  set contains_multiple_documents=true. Never merge amounts across documents.
  A multi-page SINGLE invoice ("Page 1 of 2") is NOT multiple documents.
- CURRENCY: use the printed ISO code. If only a symbol is printed ("$"),
  infer the ISO code from the country/language (Argentina -> ARS, Mexico ->
  MXN, Colombia -> COP, USA -> USD) and add 'currency' to
  low_confidence_fields.
- SPREADSHEET INPUT: when OCR_MARKDOWN contains "## Sheet:" sections with no
  page images, the source is an Excel workbook — treat cell values as exact
  and apply all the same rules.
- Reference numbers: "Booking File" / "Order N°" / booking codes (e.g.
  GKFRDQ) -> booking_number; agency "Dossier"/"Document N°" -> invoice_number
  when it is the invoice's own number.
- HOTEL FOLIOS: room-night charges (habitación / alojamiento / diária / room /
  lodging / tarifa) go in nightly_charges — one entry per night. Taxes (IVA,
  ISS), city/tourism taxes, fees, minibar, restaurant, parking, and
  late-checkout / early-checkin charges go in ancillary_charges with the
  right category. late_checkout / early_checkin lines matter downstream —
  categorise them precisely.
- AIR/AGENCY INVOICES: nightly_charges stays EMPTY. Every amount line goes in
  ancillary_charges: the base fare as category 'airfare', Tasa Aeroportuaria
  as 'airport_tax', IVA as 'tax', Tarifa Admin as 'admin_fee', its IVA as
  'tax'. Record Rutas in travel_route, Pasajero in guest_name, Entrada/Salida
  in checkin_date/checkout_date, the CUFE in e_invoice_id, and both NIT
  numbers (issuer -> supplier_tax_id, billed customer -> customer_tax_id).
- If the total is also written out in words (e.g. "SON: TRESCIENTOS OCHENTA Y
  CUATRO MIL CIENTO VEINTIDOS PESOS 00/100"), copy it verbatim into
  total_amount_in_words AND convert it to a number in
  total_amount_from_words. This is cross-checked against the numeric total.
- english_description must be a faithful translation, not a paraphrase.
- If a field is not present in the document, set it to null. NEVER guess.
- If you can read a field but are not certain (smudged, conflicting values,
  ambiguous date format), still extract your best reading AND add the field
  name to low_confidence_fields.
- Dates: resolve DD/MM vs MM/DD from document language and context; if truly
  ambiguous, choose the reading consistent with checkin < checkout and flag
  the field as low confidence.
"""


@dataclass
class ExtractionOutcome:
    extraction: FolioExtraction
    attempts: int
    warnings: list[str]           # non-fatal validator messages
    needs_review: bool


class ExtractionFailed(Exception):
    """All repair attempts exhausted; document goes to poison queue / review."""


class LLMExtractor:
    def __init__(self, llm: StructuredLLM, max_attempts: int = 3,
                 max_output_tokens: int = 16000):
        self._llm = llm
        self._max_attempts = max_attempts
        self._max_tokens = max_output_tokens
        # strict structured outputs require additionalProperties: false — our
        # models set extra="forbid", so model_json_schema() already complies.
        self._schema = FolioExtraction.model_json_schema()

    def extract(self, layout: DocumentLayout, filename: str) -> ExtractionOutcome:
        feedback: str | None = None
        last_error: str = ""

        for attempt in range(1, self._max_attempts + 1):
            user_parts = self._build_user_parts(layout, filename, feedback)
            try:
                raw = self._llm.extract(
                    system_prompt=SYSTEM_PROMPT,
                    user_parts=user_parts,
                    json_schema=self._schema,
                    schema_name="folio_extraction",
                    max_output_tokens=self._max_tokens,
                )
            except Exception as e:  # transport / provider errors are retryable
                last_error = f"LLM call failed: {e}"
                logger.warning("%s attempt %d: %s", filename, attempt, last_error)
                continue

            try:
                extraction = FolioExtraction.model_validate(raw)
            except ValidationError as ve:
                last_error = f"Schema validation errors:\n{ve}"
                feedback = last_error
                logger.warning("%s attempt %d rejected by schema.", filename, attempt)
                continue

            report = validate_extraction(extraction)
            if report.fatal:
                last_error = "Consistency check failures:\n- " + "\n- ".join(report.fatal)
                feedback = (
                    last_error
                    + "\nRe-examine the document and return a corrected extraction. "
                      "If the document genuinely contains the inconsistency, keep the "
                      "printed values and add the affected fields to low_confidence_fields."
                )
                logger.warning("%s attempt %d rejected by validators: %s",
                               filename, attempt, report.fatal)
                continue

            needs_review = report.needs_review or bool(extraction.low_confidence_fields)
            return ExtractionOutcome(
                extraction=extraction,
                attempts=attempt,
                warnings=report.warnings,
                needs_review=needs_review,
            )

        raise ExtractionFailed(f"{filename}: extraction failed after "
                               f"{self._max_attempts} attempts. Last error: {last_error}")

    # ------------------------------------------------------------------ #

    def _build_user_parts(self, layout: DocumentLayout, filename: str,
                          feedback: str | None) -> list[dict]:
        parts: list[dict] = [{
            "type": "text",
            "text": (
                f"FILENAME: {filename}\n"
                f"PAGES: {layout.page_count}\n"
                f"DETECTED_LANGUAGES: {', '.join(layout.languages) or 'unknown'}\n\n"
                f"OCR_MARKDOWN:\n{layout.markdown_text}"
            ),
        }]
        for b64 in layout.page_images_b64:
            parts.append({
                "type": "image_url",
                "image_url": {"url": f"data:image/png;base64,{b64}"},
            })
        if feedback:
            parts.append({
                "type": "text",
                "text": (
                    "PREVIOUS ATTEMPT WAS REJECTED. Fix these problems:\n"
                    f"{feedback}"
                ),
            })
        return parts


def outcome_summary(outcome: ExtractionOutcome) -> str:
    return json.dumps({
        "attempts": outcome.attempts,
        "needs_review": outcome.needs_review,
        "warnings": outcome.warnings,
        "low_confidence_fields": outcome.extraction.low_confidence_fields,
    })
