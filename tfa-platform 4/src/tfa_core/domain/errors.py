"""tfa_core.domain.errors — failures the business cares about.

Distinguishing *retryable* from *terminal* is a domain decision, not a
transport detail: a corrupt file will fail identically on all five queue
deliveries, so retrying it wastes budget and delays the poison-queue signal an
operator needs.
"""
from __future__ import annotations


class DomainError(Exception):
    """Base class. Never raised directly."""


class TerminalError(DomainError):
    """Retrying cannot succeed. Callers mark the document failed immediately."""


class RetryableError(DomainError):
    """A transient condition; the caller should let the platform redeliver."""


class UnsupportedDocumentType(TerminalError):
    """The platform has no reader for this file type."""


class ExtractionFailed(RetryableError):
    """The repair budget was exhausted without an accepted extraction."""
