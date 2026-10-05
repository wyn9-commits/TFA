"""Evaluator + graph tests. Evaluators are pure functions; the graph is
exercised end-to-end with a scripted fake LLM (bad extraction first, repaired
extraction second) to prove the repair edge fires."""
from decimal import Decimal

from evals.evaluators import (
    doc_type_correct, field_accuracy, money_exact, review_routing, sum_check,
)

REF = {
    "document_type": "air_ticket_invoice",
    "invoice_number": "WGT000032",
    "currency": "COP",
    "total_amount": "384122.00",
    "ancillary_charges": [{"amount": "260000.00"}, {"amount": "49400.00"},
                          {"amount": "22600.00"}, {"amount": "43800.00"},
                          {"amount": "8322.00"}],
    "nightly_charges": [],
    "expect_needs_review": False,
}
PERFECT = {**REF, "needs_review": False}


def test_perfect_output_scores_one_everywhere():
    for ev in (money_exact, sum_check, doc_type_correct, field_accuracy):
        assert ev(PERFECT, REF)["score"] == 1.0
    assert review_routing(PERFECT, REF)["score"] == 1.0


def test_money_exact_catches_one_cent():
    bad = {**PERFECT, "total_amount": "384122.01"}
    assert money_exact(bad, REF)["score"] < 1.0


def test_sum_check_fails_on_missing_line():
    bad = {**PERFECT, "ancillary_charges": REF["ancillary_charges"][:-1]}
    assert sum_check(bad, REF)["score"] == 0.0


def test_missed_review_flag_scores_zero_overflag_scores_half():
    ref = {**REF, "expect_needs_review": True}
    assert review_routing({**PERFECT, "needs_review": False}, ref)["score"] == 0.0
    assert review_routing({**PERFECT, "needs_review": True}, REF)["score"] == 0.5


def test_graph_repair_loop_recovers_from_bad_first_attempt():
    from tfa_core.adapters.document_intelligence import DocumentLayout
    from tfa_core.pipeline.graph_engine import GraphExtractor

    good = {
        "document_type": "air_ticket_invoice", "hotel_name": "Wings",
        "currency": "USD", "nightly_charges": [],
        "ancillary_charges": [
            {"description": "Fare", "english_description": "Fare",
             "amount": "1056.00", "category": "airfare"},
            {"description": "Airport Tax", "english_description": "Airport tax",
             "amount": "276.59", "category": "airport_tax"}],
        "total_amount": "1332.59",
    }
    bad = {**good, "ancillary_charges": good["ancillary_charges"][:1]}  # sum fails

    class ScriptedLLM:
        def __init__(self):
            self.calls = 0
            self.saw_feedback = False
        def extract(self, system_prompt, user_parts, json_schema, schema_name,
                    max_output_tokens):
            self.calls += 1
            if any("PREVIOUS ATTEMPT WAS REJECTED" in p.get("text", "")
                   for p in user_parts if p.get("type") == "text"):
                self.saw_feedback = True
            return bad if self.calls == 1 else good

    llm = ScriptedLLM()
    layout = DocumentLayout(markdown_text="TAX INVOICE ...", page_count=1)
    outcome = GraphExtractor(llm, max_attempts=3).extract(layout, "t.pdf")
    assert llm.calls == 2                      # repair edge fired exactly once
    assert llm.saw_feedback                    # validator errors were fed back
    assert outcome.attempts == 2
    assert outcome.extraction.total_amount == Decimal("1332.59")
