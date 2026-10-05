"""tfa_core.domain.values — framework-free value objects.

`DocumentLayout` previously lived inside the Azure Document Intelligence
adapter, so every module that needed the *concept* of "a document's text and
page images" imported an Azure module — including `domain.services.prompting`.
That is a dependency-rule violation: the domain pointed outward at a vendor
adapter. The type is a plain value object with no Azure semantics, so it
belongs here; the adapter now imports it, not the other way round.
"""
from __future__ import annotations

from dataclasses import dataclass, field

@dataclass
class DocumentLayout:
    """Everything Stage 2 needs about the source document.

    `page_images_b64` is populated ONLY when the images add information the OCR
    text does not (see `needs_page_images`). For a born-digital e-invoice the
    Document Intelligence markdown is already exact, so attaching 12 page
    images costs ~14 MB of RAM per document and ~18k image tokens per request
    while changing nothing about the answer.
    """
    markdown_text: str                     # full-document markdown incl. tables
    page_count: int
    page_images_b64: list[str] = field(default_factory=list)   # PNG per page
    languages: list[str] = field(default_factory=list)
    ocr_confidence_floor: float = 1.0      # lowest word confidence seen
    images_omitted: bool = False           # true when text-only was sufficient


# Re-exported identity + pagination helpers live in
# tfa_core.adapters.sql_engine for now; they are pure and will
# move here once the SQL adapter no longer needs them at import time.

# Identity rules live in tfa_core.domain.identity; re-exported so callers can treat
# "value objects" as one import surface.
from tfa_core.domain.identity import compute_document_id, content_hash  # noqa: E402,F401
