"""tfa_core.domain.prompting — the single definition of how we ask.

Before this module the extract→validate→repair algorithm existed twice: once
as a `while` loop in `llm_extractor.py` and once as nodes in
`graph/extraction_graph.py`, each with its own copy of `_build_user_parts` and
its own repair-feedback wording. Two copies of the same algorithm drift, and
the eval gate only ever exercised one of them.

Both engines now orchestrate these primitives instead of reimplementing them:

    PromptBuilder  — assembles system prompt + user parts (+ repair feedback)
    AttemptPolicy  — decides retry/stop and owns the feedback wording
    parse_and_validate — schema parse + consistency checks in one place

Pure: no I/O, no provider knowledge, trivially unit-testable.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from enum import Enum
from typing import Any, Optional

from pydantic import ValidationError

from tfa_core.domain.values import DocumentLayout
from tfa_core.domain.validation import ValidationReport, validate_extraction
from tfa_core.domain.models import FolioExtraction

logger = logging.getLogger(__name__)

SCHEMA_NAME = "folio_extraction"

#: Bump when SYSTEM_PROMPT changes. Recorded per document so any historical
#: extraction can be explained/reproduced during a financial audit.
PROMPT_VERSION = "2025-08-v1"

REPAIR_SUFFIX = (
    "\nRe-examine the document and return a corrected extraction. "
    "If the document genuinely contains the inconsistency, keep the printed "
    "values and add the affected fields to low_confidence_fields."
)


class Verdict(str, Enum):
    ACCEPTED = "accepted"          # clean, or clean-with-warnings
    RETRYABLE = "retryable"        # schema/consistency failure, repairable
    TRANSPORT_ERROR = "transport"  # provider/network failure, repairable


@dataclass
class AttemptResult:
    verdict: Verdict
    extraction: Optional[FolioExtraction] = None
    report: Optional[ValidationReport] = None
    error: str = ""

    @property
    def needs_review(self) -> bool:
        if self.extraction is None or self.report is None:
            return False
        return self.report.needs_review or bool(self.extraction.low_confidence_fields)

    @property
    def warnings(self) -> list[str]:
        return list(self.report.warnings) if self.report else []


class PromptBuilder:
    """Builds the provider-agnostic message payload for one attempt."""

    def __init__(self, system_prompt: str, prompt_version: str = PROMPT_VERSION):
        self.system_prompt = system_prompt
        self.prompt_version = prompt_version

    def user_parts(self, layout: DocumentLayout, filename: str,
                   feedback: Optional[str] = None) -> list[dict[str, Any]]:
        parts: list[dict[str, Any]] = [{
            "type": "text",
            "text": (
                f"FILENAME: {filename}\n"
                f"PAGES: {layout.page_count}\n"
                f"DETECTED_LANGUAGES: {', '.join(layout.languages) or 'unknown'}\n\n"
                f"OCR_MARKDOWN:\n{layout.markdown_text}"
            ),
        }]
        for image_b64 in layout.page_images_b64:
            parts.append({
                "type": "image_url",
                "image_url": {"url": f"data:image/png;base64,{image_b64}"},
            })
        if feedback:
            parts.append({
                "type": "text",
                "text": f"PREVIOUS ATTEMPT WAS REJECTED. Fix these problems:\n{feedback}",
            })
        return parts


def parse_and_validate(raw: dict[str, Any]) -> AttemptResult:
    """Schema parse → consistency validation. One implementation, both engines."""
    try:
        extraction = FolioExtraction.model_validate(raw)
    except ValidationError as exc:
        return AttemptResult(
            verdict=Verdict.RETRYABLE,
            error=f"Schema validation errors:\n{exc}",
        )

    report = validate_extraction(extraction)
    if report.fatal:
        return AttemptResult(
            verdict=Verdict.RETRYABLE,
            extraction=extraction,
            report=report,
            error="Consistency check failures:\n- " + "\n- ".join(report.fatal),
        )
    return AttemptResult(verdict=Verdict.ACCEPTED, extraction=extraction, report=report)


class AttemptPolicy:
    """Owns retry budget and repair-feedback wording."""

    def __init__(self, max_attempts: int = 3):
        if max_attempts < 1:
            raise ValueError("max_attempts must be >= 1")
        self.max_attempts = max_attempts

    def should_retry(self, attempt: int, result: AttemptResult) -> bool:
        if result.verdict is Verdict.ACCEPTED:
            return False
        return attempt < self.max_attempts

    @staticmethod
    def feedback_for(result: AttemptResult) -> Optional[str]:
        """Transport failures carry no model-actionable feedback: retry the
        same prompt rather than telling the model it did something wrong."""
        if result.verdict is Verdict.TRANSPORT_ERROR or not result.error:
            return None
        return result.error + REPAIR_SUFFIX

    @staticmethod
    def failure_message(filename: str, attempts: int, last_error: str) -> str:
        return (f"{filename}: extraction failed after {attempts} attempts. "
                f"Last error: {last_error}")
