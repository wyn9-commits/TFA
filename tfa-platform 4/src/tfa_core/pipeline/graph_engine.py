"""tfa_core.pipeline.graph_engine — the extraction loop as a LangGraph.

Functionally equivalent to LLMExtractor.extract()'s loop, restructured as a
StateGraph so that:
- every attempt, validator verdict, and repair feedback is a distinct traced
  node in LangSmith (instead of iterations of an opaque while-loop);
- the graph can be checkpointed/resumed and visualised;
- eval runs target the exact same graph the pipeline executes.

    extract ──> validate ──(fatal & attempts left)──> prepare_repair ──> extract
                   │
                   └──(clean | attempts exhausted)──> END

The pipeline can use either engine; `TFA_EXTRACTION_ENGINE=graph|loop`
(default: loop) selects it. Both share the prompt, schema, and validators —
there is exactly one source of truth for each.
"""
from __future__ import annotations

import logging
from typing import Any, Optional, TypedDict

from langgraph.graph import END, StateGraph

from tfa_core.domain.values import DocumentLayout
from tfa_core.pipeline.extractor import (
    SYSTEM_PROMPT, ExtractionFailed, ExtractionOutcome,
)
from tfa_core.domain.validation import validate_extraction
from tfa_core.domain.ports import StructuredLLM
from tfa_core.pipeline.tracing import traced
from tfa_core.domain.models import FolioExtraction

logger = logging.getLogger(__name__)


class ExtractionState(TypedDict, total=False):
    # inputs
    layout: DocumentLayout
    filename: str
    max_attempts: int
    max_output_tokens: int
    # working state
    attempt: int
    feedback: Optional[str]
    raw: Optional[dict[str, Any]]
    extraction: Optional[FolioExtraction]
    fatal: list[str]
    warnings: list[str]
    needs_review: bool
    last_error: str


def _build_user_parts(layout: DocumentLayout, filename: str,
                      feedback: Optional[str]) -> list[dict]:
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
        parts.append({"type": "image_url",
                      "image_url": {"url": f"data:image/png;base64,{b64}"}})
    if feedback:
        parts.append({"type": "text",
                      "text": f"PREVIOUS ATTEMPT WAS REJECTED. Fix these problems:\n{feedback}"})
    return parts


def build_extraction_graph(llm: StructuredLLM):
    schema = FolioExtraction.model_json_schema()

    def extract_node(state: ExtractionState) -> ExtractionState:
        attempt = state.get("attempt", 0) + 1
        with traced("extract_attempt",
                    {"filename": state["filename"], "attempt": attempt},
                    run_type="llm") as out:
            try:
                raw = llm.extract(
                    system_prompt=SYSTEM_PROMPT,
                    user_parts=_build_user_parts(
                        state["layout"], state["filename"], state.get("feedback")),
                    json_schema=schema,
                    schema_name="folio_extraction",
                    max_output_tokens=state["max_output_tokens"],
                )
                out["ok"] = True
                return {"attempt": attempt, "raw": raw, "last_error": ""}
            except Exception as e:
                out["ok"] = False
                out["error"] = str(e)
                return {"attempt": attempt, "raw": None,
                        "last_error": f"LLM call failed: {e}"}

    def validate_node(state: ExtractionState) -> ExtractionState:
        with traced("validate", {"filename": state["filename"],
                                 "attempt": state["attempt"]}) as out:
            if state.get("raw") is None:
                out["verdict"] = "llm_error"
                return {"fatal": [state["last_error"]], "extraction": None}
            try:
                extraction = FolioExtraction.model_validate(state["raw"])
            except Exception as ve:
                out["verdict"] = "schema_rejected"
                return {"fatal": [f"Schema validation errors:\n{ve}"],
                        "extraction": None,
                        "last_error": f"Schema validation errors:\n{ve}"}
            report = validate_extraction(extraction)
            out["verdict"] = "fatal" if report.fatal else "clean"
            out["fatal_count"] = len(report.fatal)
            return {
                "extraction": extraction,
                "fatal": report.fatal,
                "warnings": report.warnings,
                "needs_review": report.needs_review or bool(extraction.low_confidence_fields),
                "last_error": ("Consistency check failures:\n- " + "\n- ".join(report.fatal))
                              if report.fatal else "",
            }

    def prepare_repair_node(state: ExtractionState) -> ExtractionState:
        feedback = state["last_error"] + (
            "\nRe-examine the document and return a corrected extraction. "
            "If the document genuinely contains the inconsistency, keep the "
            "printed values and add the affected fields to low_confidence_fields."
        )
        return {"feedback": feedback}

    def route_after_validate(state: ExtractionState) -> str:
        if not state.get("fatal"):
            return END
        if state["attempt"] >= state["max_attempts"]:
            return END
        return "prepare_repair"

    g = StateGraph(ExtractionState)
    g.add_node("extract", extract_node)
    g.add_node("validate", validate_node)
    g.add_node("prepare_repair", prepare_repair_node)
    g.set_entry_point("extract")
    g.add_edge("extract", "validate")
    g.add_conditional_edges("validate", route_after_validate,
                            {END: END, "prepare_repair": "prepare_repair"})
    g.add_edge("prepare_repair", "extract")
    return g.compile()


class GraphExtractor:
    """Drop-in replacement for LLMExtractor, backed by the graph."""

    def __init__(self, llm: StructuredLLM, max_attempts: int = 3,
                 max_output_tokens: int = 16000):
        self._graph = build_extraction_graph(llm)
        self._max_attempts = max_attempts
        self._max_tokens = max_output_tokens

    def extract(self, layout: DocumentLayout, filename: str) -> ExtractionOutcome:
        final: ExtractionState = self._graph.invoke({
            "layout": layout,
            "filename": filename,
            "max_attempts": self._max_attempts,
            "max_output_tokens": self._max_tokens,
            "attempt": 0,
        })
        if final.get("fatal") or final.get("extraction") is None:
            raise ExtractionFailed(
                f"{filename}: extraction failed after {final.get('attempt', 0)} "
                f"attempts. Last error: {final.get('last_error', 'unknown')}")
        return ExtractionOutcome(
            extraction=final["extraction"],
            attempts=final["attempt"],
            warnings=final.get("warnings", []),
            needs_review=final.get("needs_review", False),
        )
