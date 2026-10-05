"""tfa_core.pipeline.ingest — end-to-end processing of one document.

blob event -> queue message -> THIS -> SQL row(s) + status item

Idempotent: document_id is derived from blob path + content hash, and the SQL
write is delete-then-insert on that key, so at-least-once queue delivery is
safe. Failures raise, letting the Functions host retry and eventually
dead-letter to the poison queue with the status item marked 'failed'.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional

from tfa_core.pipeline.extractor import ExtractionFailed, LLMExtractor
from tfa_core.domain.reconciliation import (
    FxRate, NegotiatedRate, RateDirectory, build_records,
)

logger = logging.getLogger(__name__)

_IMAGE_SUFFIXES = (".png", ".jpg", ".jpeg")


@dataclass
class WorkItem:
    """Queue message payload."""
    container: str
    blob_path: str            # "<identifier>-<user>/<file>"
    identifier: str
    uploading_person_name: str
    file_name: str


@dataclass
class RateContext:
    """Injected per-run: negotiated rates + FX. Sourced from SQL/SAP feed."""
    directory: RateDirectory
    fx_by_currency: dict[str, FxRate]


class FolioPipeline:
    """Orchestrates one document through the domain rules.

    Collaborators arrive through the constructor rather than being built here.
    That is what lets the whole ingestion path run against fakes in tests with
    no credentials and no network — and it keeps this module free of vendor
    imports, which the structure tests enforce.

    Wiring lives in `tfa_core.container.build_pipeline()`.
    """

    def __init__(self, *, settings, blobs, layout, extractor, sql, status,
                 analyzer=None):
        self._settings = settings
        self._blobs = blobs
        self._layout = layout
        self._extractor = extractor
        self._sql = sql
        self._status = status
        self._analyzer = analyzer

    def process(self, item: WorkItem, rates: RateContext) -> str:
        """Returns the resulting status string."""
        doc_key = f"{item.identifier}/{item.file_name}"
        content = self._blobs.download(item.container, item.blob_path)
        # Identity from the CONTENT, never from storage metadata.
        blob_md5 = content_hash(content)
        logger.info("Processing %s (%d bytes)", doc_key, len(content))

        lower = item.file_name.lower()
        if lower.endswith(".pdf"):
            layout = self._layout.analyze_pdf(content, item.file_name)
        elif lower.endswith(_IMAGE_SUFFIXES):
            media = "image/png" if lower.endswith(".png") else "image/jpeg"
            layout = self._layout.analyze_image(content, item.file_name, media)
        elif lower.endswith((".xlsx", ".xlsm", ".xls", ".csv")):
            if self._analyzer is None:
                self._status.mark(
                    document_id=blob_md5, identifier=item.identifier,
                    file_name=item.file_name, status="failed",
                    detail="No spreadsheet analyzer configured.")
                return "failed"
            layout = self._analyzer.analyze(content, item.file_name)
        else:
            self._status.mark(document_id=blob_md5, identifier=item.identifier,
                              file_name=item.file_name, status="failed",
                              detail=f"Unsupported file type: {item.file_name}")
            return "failed"

        self._status.mark(document_id=blob_md5, identifier=item.identifier,
                          file_name=item.file_name, status="processing")

        try:
            outcome = self._extractor.extract(layout, item.file_name)
        except ExtractionFailed as e:
            self._status.mark(document_id=blob_md5, identifier=item.identifier,
                              file_name=item.file_name, status="failed",
                              detail=str(e))
            raise  # let the Functions host retry / poison-queue it

        x = outcome.extraction
        is_hotel = x.document_type.value == "hotel_folio"
        rate = rates.directory.match(x.hotel_name, x.country) if is_hotel else None
        fx = rates.fx_by_currency.get(x.currency)
        warnings = list(outcome.warnings)
        if is_hotel and rate is None:
            warnings.append(f"No negotiated rate matched for '{x.hotel_name}'.")
        if fx is None and x.currency != "USD":
            warnings.append(f"No FX rate available for {x.currency}; USD fields null.")
        if x.currency == "USD" and fx is None:
            fx = FxRate(currency="USD", usd_per_unit=__import__("decimal").Decimal("1"))

        records = build_records(
            x,
            file_name=item.file_name,
            identifier_name=item.identifier,
            uploading_person_name=item.uploading_person_name,
            blob_md5=blob_md5,
            rate=rate,
            fx=fx,
            needs_review=outcome.needs_review,
            warnings=warnings,
        )
        document_id = records[0].document_id
        n = self._sql.upsert_document(document_id, records)

        status = "needs_review" if outcome.needs_review else "succeeded"
        self._status.mark(document_id=document_id, identifier=item.identifier,
                          file_name=item.file_name, status=status,
                          detail="; ".join(warnings) or None,
                          attempts=outcome.attempts)
        logger.info("%s -> %d row(s), status=%s, attempts=%d",
                    doc_key, n, status, outcome.attempts)
        return status
