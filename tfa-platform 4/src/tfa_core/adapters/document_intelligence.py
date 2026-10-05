"""tfa_core.adapters.document_intelligence — Stage 1 of the pipeline.

Runs Azure Document Intelligence prebuilt-layout over the WHOLE document
(the legacy pipeline only ever looked at page 1 as a 150-DPI screenshot).
Output is markdown-with-tables text that anchors the Stage-2 LLM: the LLM
reads deterministic OCR text + sees page images, instead of squinting at a
single low-res raster. This combination is what drives extraction accuracy.
"""
from __future__ import annotations

import base64
import logging
from dataclasses import dataclass, field

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from azure.ai.documentintelligence.models import AnalyzeResult
    from azure.core.credentials import TokenCredential

logger = logging.getLogger(__name__)


@dataclass
class DocumentLayout:
    """Everything Stage 2 needs about the source document."""
    markdown_text: str                     # full-document markdown incl. tables
    page_count: int
    page_images_b64: list[str] = field(default_factory=list)   # PNG per page
    languages: list[str] = field(default_factory=list)
    ocr_confidence_floor: float = 1.0      # lowest word confidence seen


class LayoutExtractor:
    def __init__(self, endpoint: str, credential: "TokenCredential",
                 model_id: str = "prebuilt-layout",
                 render_dpi: int = 300, max_pages: int = 25):
        from azure.ai.documentintelligence import DocumentIntelligenceClient
        self._client = DocumentIntelligenceClient(endpoint=endpoint, credential=credential)
        self._model_id = model_id
        self._dpi = render_dpi
        self._max_pages = max_pages

    def analyze_pdf(self, pdf_bytes: bytes, filename: str) -> DocumentLayout:
        result = self._analyze(pdf_bytes)
        images = self._render_pages(pdf_bytes, filename)
        floor = self._confidence_floor(result)
        langs = sorted({(l.locale or "") for l in (result.languages or []) if l.locale})
        layout = DocumentLayout(
            markdown_text=result.content or "",
            page_count=len(result.pages or []),
            page_images_b64=images,
            languages=langs,
            ocr_confidence_floor=floor,
        )
        logger.info(
            "Layout for %s: %d pages, %d chars markdown, langs=%s, ocr_floor=%.2f",
            filename, layout.page_count, len(layout.markdown_text), langs, floor,
        )
        return layout

    def analyze_image(self, image_bytes: bytes, filename: str,
                      media_type: str = "image/png") -> DocumentLayout:
        """PNG/JPEG folios (the platform also ingests images, not just PDFs)."""
        result = self._analyze(image_bytes)
        b64 = base64.b64encode(image_bytes).decode()
        return DocumentLayout(
            markdown_text=result.content or "",
            page_count=1,
            page_images_b64=[b64],
            languages=sorted({(l.locale or "") for l in (result.languages or []) if l.locale}),
            ocr_confidence_floor=self._confidence_floor(result),
        )

    # ------------------------------------------------------------------ #

    def _analyze(self, content: bytes) -> "AnalyzeResult":
        from azure.ai.documentintelligence.models import DocumentContentFormat
        poller = self._client.begin_analyze_document(
            self._model_id,
            body=content,
            output_content_format=DocumentContentFormat.MARKDOWN,
        )
        return poller.result()

    def _render_pages(self, pdf_bytes: bytes, filename: str) -> list[str]:
        import fitz  # PyMuPDF
        images: list[str] = []
        with fitz.open(stream=pdf_bytes, filetype="pdf") as doc:
            n = min(doc.page_count, self._max_pages)
            if doc.page_count > self._max_pages:
                logger.warning("%s has %d pages; rendering first %d only.",
                               filename, doc.page_count, self._max_pages)
            for i in range(n):
                pix = doc.load_page(i).get_pixmap(dpi=self._dpi)
                images.append(base64.b64encode(pix.tobytes("png")).decode())
        return images

    @staticmethod
    def _confidence_floor(result: "AnalyzeResult") -> float:
        floor = 1.0
        for page in result.pages or []:
            for word in page.words or []:
                if word.confidence is not None and word.confidence < floor:
                    floor = word.confidence
        return floor
