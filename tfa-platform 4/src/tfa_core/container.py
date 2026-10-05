"""Composition root.

The only module allowed to construct a concrete adapter. Everything else
receives its collaborators, which is what makes the pipeline testable without
credentials and a vendor swap a one-line change here rather than a cascade.
"""
from __future__ import annotations

import logging
from typing import Optional

from tfa_core.config import Settings, get_credential, get_settings

logger = logging.getLogger(__name__)


def build_pipeline(settings: Optional[Settings] = None):
    """Wires a FolioPipeline against the real Azure services."""
    from tfa_core.adapters.analyzer import CompositeDocumentAnalyzer
    from tfa_core.adapters.blob_cosmos import (
        BlobRepository, ProcessingStatusRepository,
    )
    from tfa_core.adapters.document_intelligence import LayoutExtractor
    from tfa_core.adapters.llm_provider import build_llm
    from tfa_core.adapters.sql_store import FolioSqlRepository
    from tfa_core.pipeline.extractor import LLMExtractor
    from tfa_core.pipeline.ingest import FolioPipeline

    s = settings or get_settings()
    credential = get_credential()

    layout = LayoutExtractor(
        s.docintel_endpoint, credential, s.docintel_model,
        render_dpi=s.render_dpi, max_pages=s.max_pages_per_doc,
    )
    llm = build_llm(s.llm_provider, s.llm_endpoint, s.llm_deployment,
                    s.llm_api_version, credential)

    if s.extraction_engine == "graph":
        from tfa_core.pipeline.graph_engine import GraphExtractor
        extractor = GraphExtractor(
            llm, max_attempts=s.llm_extraction_max_attempts,
            max_output_tokens=s.llm_max_output_tokens,
        )
    else:
        extractor = LLMExtractor(
            llm, max_attempts=s.llm_extraction_max_attempts,
            max_output_tokens=s.llm_max_output_tokens,
        )

    logger.info("Pipeline wired: %s engine, %s deployment",
                s.extraction_engine, s.llm_deployment)

    return FolioPipeline(
        settings=s,
        blobs=BlobRepository(s.storage_account_url, credential),
        layout=layout,
        extractor=extractor,
        sql=FolioSqlRepository(s.sql_server, s.sql_database, s.sql_table, credential),
        status=ProcessingStatusRepository(
            s.cosmos_endpoint, s.cosmos_database, s.cosmos_status_container,
            credential),
        analyzer=CompositeDocumentAnalyzer(layout),
    )
