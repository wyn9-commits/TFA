"""tfa_core.adapters.sql_store — worker-side persistence (normalized schema).

Replaces `FolioSqlRepository`, which wrote a flat `tfa_folio_lines` table built
from the column registry while the migration created `dbo.documents` +
`dbo.folio_lines` and the API read those. The write path and read path did not
meet: every ingested document was invisible to the API. This module closes
that gap.

Also removes the generated `ddl()`: `db/migrations/` is now the only schema
authority. `FOLIO_COLUMNS` keeps its legitimate job — Excel headers and the
data-dictionary sheet.

Guarantees:
- **Idempotent**: keyed on `document_id`; re-delivery replaces rows rather
  than duplicating them (ON DELETE CASCADE clears child lines).
- **Atomic**: header, lines, and audit row commit together or not at all.
"""
from __future__ import annotations

import logging
from typing import Any, Optional, Sequence

from tfa_core.adapters.sql_engine import ConnectionPool, rows_as_dicts

logger = logging.getLogger(__name__)

from tfa_core.domain.columns import DOCUMENT_COLUMNS, LINE_COLUMNS


class FolioStore:
    """Persists one extracted document (header + lines) atomically."""

    def __init__(self, pool: ConnectionPool):
        self._pool = pool

    def upsert_document(
        self,
        *,
        header: dict[str, Any],
        lines: Sequence[dict[str, Any]],
        actor: str = "system:worker",
        correlation_id: Optional[str] = None,
    ) -> int:
        document_id = header["document_id"]
        doc_params = [header.get(col) for col in DOCUMENT_COLUMNS]
        doc_sql = (
            f"INSERT INTO dbo.documents ({', '.join(DOCUMENT_COLUMNS)}) "
            f"VALUES ({', '.join('?' * len(DOCUMENT_COLUMNS))})"
        )
        line_sql = (
            f"INSERT INTO dbo.folio_lines ({', '.join(LINE_COLUMNS)}) "
            f"VALUES ({', '.join('?' * len(LINE_COLUMNS))})"
        )

        with self._pool.lease() as conn:
            cur = conn.cursor()
            # Cascade clears folio_lines; this is what makes redelivery safe.
            cur.execute("DELETE FROM dbo.documents WHERE document_id = ?", document_id)
            cur.execute(doc_sql, doc_params)
            if lines:
                cur.fast_executemany = True
                cur.executemany(
                    line_sql,
                    [[line.get(col) for col in LINE_COLUMNS] for line in lines],
                )
            cur.execute(
                "INSERT INTO dbo.audit_log (actor, action, entity_type, entity_id, "
                "after_json, correlation_id) VALUES (?,?,?,?,?,?)",
                actor, "ingested", "document", document_id,
                f'{{"lines":{len(lines)},"review_status":"{header.get("review_status")}"}}',
                correlation_id,
            )
            conn.commit()

        logger.info("Persisted %s (%d line(s), status=%s).",
                    document_id, len(lines), header.get("review_status"))
        return len(lines)

    def get_document(self, document_id: str) -> Optional[dict[str, Any]]:
        with self._pool.lease() as conn:
            cur = conn.cursor()
            cur.execute("SELECT * FROM dbo.documents WHERE document_id = ?", document_id)
            docs = rows_as_dicts(cur)
        return docs[0] if docs else None



# The domain -> row mapping lives in tfa_core.pipeline.folio_rows.
# Re-exported for callers that have not migrated yet.
def build_rows(*args, **kwargs):  # pragma: no cover - thin delegation
    from tfa_core.pipeline.folio_rows import build_rows as _impl
    return _impl(*args, **kwargs)
