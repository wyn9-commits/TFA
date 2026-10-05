"""evals/evaluators.py — field-level scoring for folio extraction.

Pure functions (dict-in, score-out) so they unit-test without LangSmith and
plug directly into `langsmith.evaluate()` in run_eval.py.

Metrics (what "accuracy" means for this platform, in priority order):
1. money_exact      — every monetary field exact to the cent. The metric that
                      failed the POC; anything less than exact is a miss.
2. sum_check        — line items reconcile to the printed total.
3. doc_type_correct — classification accuracy (wrong type ⇒ wrong downstream
                      handling even if fields are right).
4. field_accuracy   — micro-average over scalar fields (dates, IDs, names).
5. review_routing   — needs_review docs are actually flagged (safety metric:
                      a confident wrong answer is worse than a flagged one).
"""
from __future__ import annotations

from decimal import Decimal, InvalidOperation
from typing import Any

MONEY_FIELDS = [
    "total_amount", "informational_tax_amount", "total_amount_from_words",
]
SCALAR_FIELDS = [
    "hotel_name", "country", "invoice_number", "booking_number",
    "guest_name", "currency", "checkin_date", "checkout_date",
    "supplier_tax_id", "customer_tax_id", "travel_route", "folio_date",
]


def _dec(v: Any) -> Decimal | None:
    if v is None:
        return None
    try:
        return Decimal(str(v))
    except (InvalidOperation, ValueError):
        return None


def _norm(v: Any) -> str:
    return " ".join(str(v).strip().lower().split()) if v is not None else ""


def money_exact(outputs: dict, reference: dict) -> dict:
    """1.0 iff every labeled monetary value matches to the cent."""
    checked = 0
    hits = 0
    for f in MONEY_FIELDS:
        ref = _dec(reference.get(f))
        if ref is None:
            continue
        checked += 1
        got = _dec(outputs.get(f))
        if got is not None and abs(got - ref) < Decimal("0.005"):
            hits += 1
    # charge-line totals
    for key in ("nightly_charges", "ancillary_charges"):
        ref_lines = reference.get(key) or []
        got_lines = outputs.get(key) or []
        for i, ref_line in enumerate(ref_lines):
            checked += 1
            if i < len(got_lines):
                ra, ga = _dec(ref_line.get("amount")), _dec(got_lines[i].get("amount"))
                if ra is not None and ga is not None and abs(ga - ra) < Decimal("0.005"):
                    hits += 1
    score = (hits / checked) if checked else 1.0
    return {"key": "money_exact", "score": score,
            "comment": f"{hits}/{checked} monetary values exact"}


def sum_check(outputs: dict, reference: dict) -> dict:
    """1.0 iff extracted line items sum to the extracted total (±1.0 abs)."""
    total = _dec(outputs.get("total_amount"))
    if total is None:
        return {"key": "sum_check", "score": 0.0, "comment": "no total extracted"}
    s = Decimal("0")
    for key in ("nightly_charges", "ancillary_charges"):
        for line in outputs.get(key) or []:
            amt = _dec(line.get("amount"))
            if amt is not None:
                s += amt
    ok = abs(s - total) <= max(Decimal("1.0"), abs(total) * Decimal("0.005"))
    return {"key": "sum_check", "score": 1.0 if ok else 0.0,
            "comment": f"lines={s} total={total}"}


def doc_type_correct(outputs: dict, reference: dict) -> dict:
    ref = reference.get("document_type")
    got = outputs.get("document_type")
    score = 1.0 if (ref is not None and got == ref) else 0.0
    return {"key": "doc_type_correct", "score": score,
            "comment": f"got={got} expected={ref}"}


def field_accuracy(outputs: dict, reference: dict) -> dict:
    """Micro-average exact-match over labeled scalar fields."""
    checked = 0
    hits = 0
    misses: list[str] = []
    for f in SCALAR_FIELDS:
        if f not in reference or reference[f] is None:
            continue
        checked += 1
        if _norm(outputs.get(f)) == _norm(reference[f]):
            hits += 1
        else:
            misses.append(f)
    score = (hits / checked) if checked else 1.0
    return {"key": "field_accuracy", "score": score,
            "comment": f"{hits}/{checked}; missed: {', '.join(misses) or '-'}"}


def review_routing(outputs: dict, reference: dict) -> dict:
    """Safety: docs labeled as requiring review must be flagged.
    Scores only when the label exists. Over-flagging clean docs costs
    throughput, not correctness, so it scores 0.5 rather than 0."""
    ref = reference.get("expect_needs_review")
    if ref is None:
        return {"key": "review_routing", "score": 1.0, "comment": "unlabeled"}
    got = bool(outputs.get("needs_review"))
    if ref and got:
        score, why = 1.0, "correctly flagged"
    elif ref and not got:
        score, why = 0.0, "MISSED review flag (confident wrong answer risk)"
    elif not ref and got:
        score, why = 0.5, "over-flagged (throughput cost only)"
    else:
        score, why = 1.0, "correctly clean"
    return {"key": "review_routing", "score": score, "comment": why}


ALL_EVALUATORS = [money_exact, sum_check, doc_type_correct,
                  field_accuracy, review_routing]
