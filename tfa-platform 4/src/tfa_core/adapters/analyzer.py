"""tfa_core.adapters.analyzer — one `DocumentAnalyzer` for all types.

The ingestion use case previously branched on file extension and called
adapter-specific methods (`analyze_pdf`, `analyze_image`, `analyze_excel`).
That is a routing detail of *how documents are read*, not part of the business
workflow: adding a new format meant editing a use case.

This composite satisfies the single `DocumentAnalyzer` port and dispatches
internally, so the use case asks one question — "give me the layout" — and new
formats are registered here without touching application code.
"""
from __future__ import annotations

import logging
from typing import Callable, Optional

from tfa_core.domain.errors import UnsupportedDocumentType
from tfa_core.domain.values import DocumentLayout

logger = logging.getLogger(__name__)

PDF_SUFFIXES = (".pdf",)
IMAGE_SUFFIXES = (".png", ".jpg", ".jpeg")
SPREADSHEET_SUFFIXES = (".xlsx", ".xlsm", ".xls")
CSV_SUFFIXES = (".csv",)


class CompositeDocumentAnalyzer:
    """Routes by file extension to the right reader."""

    def __init__(self, layout_extractor, excel_reader: Optional[Callable] = None):
        self._layout = layout_extractor
        if excel_reader is None:
            from tfa_core.adapters.excel_reader import analyze_excel
            excel_reader = analyze_excel
        self._excel = excel_reader

    def analyze(self, content: bytes, filename: str) -> DocumentLayout:
        lower = filename.lower()
        if lower.endswith(PDF_SUFFIXES):
            return self._layout.analyze_pdf(content, filename)
        if lower.endswith(IMAGE_SUFFIXES):
            media = "image/png" if lower.endswith(".png") else "image/jpeg"
            return self._layout.analyze_image(content, filename, media)
        if lower.endswith(SPREADSHEET_SUFFIXES):
            return self._excel(content, filename)
        if lower.endswith(CSV_SUFFIXES):
            from tfa_core.adapters.csv_reader import analyze_csv
            return analyze_csv(content, filename)
        raise UnsupportedDocumentType(f"Unsupported file type: {filename}")

__all__ = ["CompositeDocumentAnalyzer", "UnsupportedDocumentType"]
