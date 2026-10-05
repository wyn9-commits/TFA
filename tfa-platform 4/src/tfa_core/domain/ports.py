"""tfa_core.domain.ports — the boundary the domain owns.

These Protocols are the *only* way the domain and application layers reach the
outside world. Concrete adapters live in `tfa.infrastructure` and are bound to
these ports exactly once, in `tfa_core.container`.

Why Protocols rather than ABCs: adapters do not import the domain to inherit
from it, so the dependency arrow points inward by construction — an adapter
cannot accidentally become a base class that the domain depends on. Structural
typing also means test doubles are plain classes with no registration step,
while still being checked by mypy (a fake that drifts from the interface fails
type-check instead of failing at runtime, which is what happened with the
duck-typed `FakeRepo` in the original test suite).

Naming rule: ports are named for *what the domain needs*, not for the vendor
that happens to provide it. `DocumentAnalyzer`, not `AzureDocumentIntelligence`.
Swapping the vendor must not rename a domain concept.
"""
from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Any, Iterator, Optional, Protocol, Sequence, runtime_checkable

from tfa_core.domain.values import DocumentLayout

# --------------------------------------------------------------------------- #
# Extraction
# --------------------------------------------------------------------------- #


@runtime_checkable
class StructuredLLM(Protocol):
    """A model that returns JSON conforming to a supplied schema.

    Implementations: Azure OpenAI (`response_format=json_schema`) and Claude on
    Foundry (forced tool use). The domain never learns which one it got.
    """

    def extract(
        self,
        system_prompt: str,
        user_parts: list[dict[str, Any]],
        json_schema: dict[str, Any],
        schema_name: str,
        max_output_tokens: int,
    ) -> dict[str, Any]: ...


@runtime_checkable
class DocumentAnalyzer(Protocol):
    """Turns raw document bytes into text + optional page images."""

    def analyze(self, content: bytes, filename: str) -> DocumentLayout: ...


@runtime_checkable
class ExtractionEngine(Protocol):
    """Runs extract -> validate -> repair until accepted or the budget is spent.

    Two implementations exist (sequential loop, LangGraph). Because both are
    behind this port, the pipeline cannot depend on which is configured.
    """

    def extract(self, layout: DocumentLayout, filename: str) -> Any: ...


# --------------------------------------------------------------------------- #
# Storage
# --------------------------------------------------------------------------- #


@runtime_checkable
class BlobStore(Protocol):
    def download(self, container: str, blob_path: str) -> bytes: ...
    def stream(self, container: str, blob_path: str,
               chunk_size: int = ...) -> Iterator[bytes]: ...
    def upload(self, container: str, blob_path: str, data: bytes,
               overwrite: bool = ...) -> None: ...
    def version(self, container: str, blob_path: str) -> str: ...


@runtime_checkable
class FolioWriteStore(Protocol):
    """Write side: used by the ingestion worker."""

    def upsert_document(self, *, header: dict[str, Any],
                        lines: Sequence[dict[str, Any]],
                        actor: str = ...,
                        correlation_id: Optional[str] = ...) -> int: ...


@runtime_checkable
class FolioReadStore(Protocol):
    """Read side: used by the API. Split from the write side (CQRS-lite) because
    the two have different consumers, different scaling profiles, and different
    reasons to change."""

    def list_documents(self, filters: dict[str, Any], limit: int,
                       cursor: Optional[str]) -> tuple[list[dict], Optional[str]]: ...
    def get_document(self, document_id: str) -> Optional[dict]: ...
    def get_blob_location(self, document_id: str) -> Optional[tuple[str, str, str]]: ...
    def review_queue(self, limit: int,
                     cursor: Optional[str]) -> tuple[list[dict], Optional[str]]: ...
    def apply_review(self, document_id: str, actor: str, new_status: str,
                     corrections: Optional[dict[str, Any]] = ...,
                     reason: Optional[str] = ...,
                     correlation_id: Optional[str] = ...) -> bool: ...
    def report_rows(self, filters: dict[str, Any], limit: int) -> list[dict]: ...
    def report_cursor(self, filters: dict[str, Any], limit: int) -> Any:
        """Context manager yielding a live cursor for streaming exports.

        Declared here because the reports router calls it. It was previously
        missing from the port, so an alternative implementation could satisfy
        the Protocol and still fail at runtime — exactly what happened when the
        in-memory demo store was written against this interface.
        """
        ...
    def summary(self, filters: dict[str, Any], group_by: str) -> list[dict]: ...
    def filter_options(self) -> dict[str, list[str]]: ...
    def list_negotiated_rates(self) -> list[dict]: ...
    def upsert_negotiated_rates(self, rates: Sequence[dict], actor: str) -> int: ...
    def upsert_fx_rates(self, rates: Sequence[dict], actor: str) -> int: ...
    def ping(self) -> bool: ...


@runtime_checkable
class StatusStore(Protocol):
    """Per-document processing state. Deliberately separate from the folio store:
    losing status must never mean losing financial data."""

    def mark(self, *, document_id: str, identifier: str, file_name: str,
             status: str, detail: Optional[str] = ...,
             attempts: Optional[int] = ...) -> None: ...
    def get(self, document_id: str) -> Optional[dict]: ...


@runtime_checkable
class WorkQueue(Protocol):
    def send_message(self, content: str) -> Any: ...


# --------------------------------------------------------------------------- #
# Reference data
# --------------------------------------------------------------------------- #


@runtime_checkable
class RateProvider(Protocol):
    """Supplies negotiated rates and FX for reconciliation.

    Today backed by JSON blobs; the SAP feed will replace the adapter without
    the domain noticing. This is the seam that keeps the SAP integration from
    leaking into reconciliation logic.
    """

    def negotiated_rate(self, hotel_name: str,
                        country: Optional[str]) -> Optional[Any]: ...
    def fx_rate(self, currency: str, on: Optional[date]) -> Optional[Any]: ...


@runtime_checkable
class Clock(Protocol):
    """Injected time. Domain code that reads the wall clock directly is not
    testable and not reproducible; audits need both."""

    def today(self) -> date: ...
    def now_iso(self) -> str: ...


# --------------------------------------------------------------------------- #
# Cross-cutting
# --------------------------------------------------------------------------- #


@runtime_checkable
class Cache(Protocol):
    def get(self) -> Any: ...
    def invalidate(self) -> None: ...


class MoneyRounding:
    """Domain constant, not a config knob: financial output is always 2 dp."""

    CENTS = Decimal("0.01")
