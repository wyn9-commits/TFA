"""tfa_core.adapters.blob_cosmos — persistence with Managed Identity only.

Replaces:
- pymssql + SQL_USERNAME/SQL_PASSWORD  -> pyodbc + Entra access token
- track_folios_file.txt in a blob      -> Cosmos DB per-document status items
  (atomic, queryable, no read-modify-write race between web app and worker)
"""
from __future__ import annotations

import logging
import struct
from datetime import datetime, timezone
from typing import Any, Optional

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import pyodbc
    from azure.core.credentials import TokenCredential

from tfa_core.domain.models import SQL_COLUMNS, FolioRecord
from tfa_core.domain.reconciliation import derive_year_and_quarter

logger = logging.getLogger(__name__)

_SQL_SCOPE = "https://database.windows.net/.default"
_SQL_COPT_SS_ACCESS_TOKEN = 1256  # msodbcsql.h


# --------------------------------------------------------------------------- #
# SQL
# --------------------------------------------------------------------------- #

class FolioSqlRepository:
    def __init__(self, server: str, database: str, table: str,
                 credential: "TokenCredential"):
        self._conn_str = (
            "Driver={ODBC Driver 18 for SQL Server};"
            f"Server=tcp:{server},1433;Database={database};"
            "Encrypt=yes;TrustServerCertificate=no;Connection Timeout=30;"
        )
        self._table = table
        self._credential = credential

    def _connect(self) -> "pyodbc.Connection":
        import pyodbc
        token = self._credential.get_token(_SQL_SCOPE).token.encode("utf-16-le")
        token_struct = struct.pack(f"<I{len(token)}s", len(token), token)
        return pyodbc.connect(
            self._conn_str,
            attrs_before={_SQL_COPT_SS_ACCESS_TOKEN: token_struct},
        )

    def upsert_document(self, document_id: str, records: list[FolioRecord]) -> int:
        """Idempotent write: delete-then-insert inside one transaction keyed by
        document_id, so queue redeliveries never duplicate rows."""
        cols = SQL_COLUMNS + ["document_id"]
        placeholders = ", ".join("?" for _ in cols)
        collist = ", ".join(cols)
        insert = f"INSERT INTO {self._table} ({collist}) VALUES ({placeholders})"
        with self._connect() as conn:
            cur = conn.cursor()
            cur.execute(f"DELETE FROM {self._table} WHERE document_id = ?", document_id)
            for rec in records:
                cur.execute(insert, self._row(rec) + [document_id])
            conn.commit()
            return len(records)

    @staticmethod
    def _row(rec: FolioRecord) -> list[Any]:
        d = rec.model_dump(mode="json")
        year, quarter = derive_year_and_quarter(rec.year_month)
        d["year"], d["year_quarter"] = year, quarter
        return [d.get(col) for col in SQL_COLUMNS]

    def ddl(self) -> str:
        """CREATE TABLE statement generated from the column registry —
        schema drift between code and DB becomes impossible to miss."""
        typemap = {
            "original_per_day_price": "DECIMAL(18,2)", "negotiated_rate": "DECIMAL(18,2)",
            "currency_multiplier_rate": "DECIMAL(18,6)", "usd_per_day_price": "DECIMAL(18,2)",
            "usd_late_early_unit_price": "DECIMAL(18,2)",
            "usd_total_overcharged_amount": "DECIMAL(18,2)",
            "usd_total_undercharged_amount": "DECIMAL(18,2)",
            "usd_total_amount_differ": "DECIMAL(18,2)",
            "number_of_days": "INT", "number_of_nights_overcharged": "INT",
            "number_of_nights_undercharged": "INT",
            "folio_date": "DATE", "checkin": "DATE", "checkout": "DATE",
            "extraction_warnings": "NVARCHAR(2000)",
        }
        cols = ",\n    ".join(
            f"[{c}] {typemap.get(c, 'NVARCHAR(400)')} NULL" for c in SQL_COLUMNS
        )
        return (
            f"CREATE TABLE {self._table} (\n"
            "    [id] BIGINT IDENTITY(1,1) PRIMARY KEY,\n"
            "    [document_id] CHAR(32) NOT NULL,\n"
            f"    {cols},\n"
            "    [ingested_at_utc] DATETIME2 NOT NULL DEFAULT SYSUTCDATETIME()\n"
            ");\n"
            f"CREATE INDEX IX_{self._table}_document_id ON {self._table}(document_id);\n"
            f"CREATE INDEX IX_{self._table}_identifier ON {self._table}(identifier_name);\n"
            f"CREATE INDEX IX_{self._table}_year_month ON {self._table}(year_month);"
        )


# --------------------------------------------------------------------------- #
# Cosmos processing status (replaces the tracking .txt file)
# --------------------------------------------------------------------------- #

class ProcessingStatusRepository:
    """One item per document; partition key = /identifier."""

    def __init__(self, endpoint: str, database: str, container: str,
                 credential: "TokenCredential"):
        from azure.cosmos import CosmosClient, PartitionKey
        client = CosmosClient(endpoint, credential=credential)
        db = client.create_database_if_not_exists(database)
        self._container = db.create_container_if_not_exists(
            id=container, partition_key=PartitionKey(path="/identifier"),
        )

    def mark(self, *, document_id: str, identifier: str, file_name: str,
             status: str, detail: Optional[str] = None,
             attempts: Optional[int] = None) -> None:
        item = {
            "id": document_id,
            "identifier": identifier,
            "file_name": file_name,
            "status": status,           # queued | processing | succeeded | needs_review | failed
            "detail": detail,
            "attempts": attempts,
            "updated_at_utc": datetime.now(timezone.utc).isoformat(),
        }
        self._container.upsert_item(item)

    def pending_for_identifier(self, identifier: str) -> list[dict]:
        query = "SELECT * FROM c WHERE c.identifier = @id AND c.status IN ('queued','processing')"
        return list(self._container.query_items(
            query, parameters=[{"name": "@id", "value": identifier}],
            partition_key=identifier,
        ))


# --------------------------------------------------------------------------- #
# Blob
# --------------------------------------------------------------------------- #

class BlobRepository:
    def __init__(self, account_url: str, credential: TokenCredential):
        self._svc = BlobServiceClient(account_url=account_url, credential=credential)

    def download(self, container: str, blob_path: str) -> bytes:
        """Returns the blob contents.

        Returns bytes ONLY, deliberately. The previous signature returned
        (content, "md5-or-etag") for use as an idempotency key:

            md5 = (props.content_settings.content_md5 or b"").hex() or props.etag...

        The Azure SDK only populates `content_md5` when the uploader supplies
        it, and ours does not — so the ETag branch was the only branch ever
        taken. An ETag changes on EVERY write, including an overwrite with
        byte-identical content, so each re-sync minted a new document_id and
        inserted an ADDITIONAL row. Three reprocesses produced three rows and
        three times the reported overcharge.

        Identity now comes from the bytes themselves: see
        `tfa_core.domain.identity.content_hash`. Returning a single value removes the
        footgun rather than documenting it.
        """
        bc = self._svc.get_blob_client(container, blob_path)
        return bc.download_blob(max_concurrency=4).readall()

    def version(self, container: str, blob_path: str) -> str:
        """Storage version, for cache invalidation ONLY — never for identity."""
        props = self._svc.get_blob_client(container, blob_path).get_blob_properties()
        return f"{props.etag}:{props.last_modified.isoformat()}"

    def upload(self, container: str, blob_path: str, data: bytes,
               overwrite: bool = False) -> None:
        self._svc.get_blob_client(container, blob_path).upload_blob(
            data, overwrite=overwrite,
        )
