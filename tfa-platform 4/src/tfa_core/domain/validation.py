"""tfa_core.domain.validation — deterministic consistency checks.

These run AFTER schema validation and decide three things:
- fatal: extraction is internally inconsistent → feed errors back to the LLM
  for a repair attempt.
- needs_review: extraction is plausible but a money/date field is uncertain →
  persist with review_status=needs_review, never silently auto-approve.
- warnings: informational; recorded alongside the row.

This is the layer the POC lacked entirely: nothing ever checked that the
numbers added up before rows landed in the reconciliation table.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal

from tfa_core.domain.models import DocumentType, FolioExtraction

# Money fields whose low-confidence flag forces human review.
_CRITICAL_FIELDS = {
    "total_amount", "currency", "checkin_date", "checkout_date",
    "nightly_charges", "invoice_number",
}

# Tolerance for total reconciliation: folios round line items; allow the
# greater of 1 currency unit or 0.5% of the printed total.
_ABS_TOL = Decimal("1.00")
_REL_TOL = Decimal("0.005")


@dataclass
class ValidationReport:
    fatal: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    needs_review: bool = False


def validate_extraction(x: FolioExtraction) -> ValidationReport:
    r = ValidationReport()

    # Escape hatches first: files that aren't (single, extractable) invoices
    # never run the invoice arithmetic, and always go to a human.
    if x.document_type == DocumentType.NOT_AN_INVOICE:
        r.warnings.append(
            "Classified as not an invoice: "
            + (x.unreadable_reason or "no reason given")
        )
        r.needs_review = True
        return r
    if x.contains_multiple_documents:
        r.warnings.append(
            "File contains multiple documents; only the first was extracted. "
            "Needs manual splitting."
        )
        r.needs_review = True
    if x.unreadable_reason:
        r.warnings.append(f"Legibility concern: {x.unreadable_reason}")
        r.needs_review = True
    if x.document_type == DocumentType.CREDIT_NOTE:
        # Credit notes reverse charges: totals and lines should be negative.
        if x.total_amount is not None and x.total_amount > 0:
            r.warnings.append(
                "Credit note with a positive total; confirm sign convention."
            )
            r.needs_review = True

    _check_dates(x, r)
    _check_totals(x, r)
    _check_words_total(x, r)
    _check_nights(x, r)
    _check_confidence(x, r)

    return r


def _check_words_total(x: FolioExtraction, r: ValidationReport) -> None:
    """Many LatAm e-invoices print the total in words ('SON: ... PESOS').
    The model converts the words to a number; if that disagrees with the
    numeric total, one of the two was misread — a free, strong cross-check."""
    if x.total_amount_from_words is None or x.total_amount is None:
        return
    if abs(x.total_amount_from_words - x.total_amount) > _ABS_TOL:
        r.fatal.append(
            f"Numeric total {x.total_amount} disagrees with the amount written "
            f"in words ({x.total_amount_in_words!r} -> {x.total_amount_from_words}). "
            "Re-read both; one of them was misextracted."
        )


def _check_dates(x: FolioExtraction, r: ValidationReport) -> None:
    if x.checkin_date and x.checkout_date:
        if x.checkout_date < x.checkin_date:
            r.fatal.append(
                f"checkout_date {x.checkout_date} is before checkin_date "
                f"{x.checkin_date}. Re-read the dates; consider DD/MM vs MM/DD."
            )
        elif (x.checkout_date - x.checkin_date).days > 60:
            r.warnings.append(
                f"Stay length {(x.checkout_date - x.checkin_date).days} nights "
                "is unusually long; verify dates."
            )
            r.needs_review = True
    if x.folio_date and x.checkin_date and x.folio_date < x.checkin_date:
        r.warnings.append(
            f"folio_date {x.folio_date} precedes checkin_date {x.checkin_date} "
            "(possible pro-forma invoice)."
        )


def _check_totals(x: FolioExtraction, r: ValidationReport) -> None:
    if x.total_amount is None:
        r.warnings.append("No printed total on folio; total reconciliation skipped.")
        r.needs_review = True
        return
    line_sum = (
        sum((c.amount for c in x.nightly_charges), Decimal("0"))
        + sum((c.amount for c in x.ancillary_charges), Decimal("0"))
    )
    tol = max(_ABS_TOL, (abs(x.total_amount) * _REL_TOL).quantize(Decimal("0.01")))
    diff = abs(line_sum - x.total_amount)
    if diff > tol:
        r.fatal.append(
            f"Line items sum to {line_sum} but printed total is {x.total_amount} "
            f"(difference {diff}, tolerance {tol}). A charge line is missing, "
            "duplicated, or misread — re-extract ALL charge lines including "
            "taxes and fees."
        )


def _check_nights(x: FolioExtraction, r: ValidationReport) -> None:
    if x.document_type != DocumentType.HOTEL_FOLIO:
        return  # air/agency invoices have no room nights
    if not x.nightly_charges:
        r.warnings.append("Hotel folio has no room-night charge lines; confirm "
                          "the room charges were not missed.")
        r.needs_review = True
        return
    if x.checkin_date and x.checkout_date:
        expected = (x.checkout_date - x.checkin_date).days
        actual = sum(n.nights for n in x.nightly_charges)
        if expected > 0 and actual != expected:
            # Not always fatal: day-use rooms, comped nights.
            r.warnings.append(
                f"Nightly charge lines cover {actual} night(s) for a "
                f"{expected}-night stay. Confirm nights were not collapsed "
                "without setting `nights`, or split."
            )
            r.needs_review = True


def _check_confidence(x: FolioExtraction, r: ValidationReport) -> None:
    flagged = set(x.low_confidence_fields)
    critical = flagged & _CRITICAL_FIELDS
    if critical:
        r.warnings.append(
            "Low confidence on critical field(s): " + ", ".join(sorted(critical))
        )
        r.needs_review = True
    elif flagged:
        r.warnings.append("Low confidence on: " + ", ".join(sorted(flagged)))
