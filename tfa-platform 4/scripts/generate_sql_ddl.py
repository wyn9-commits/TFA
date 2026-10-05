#!/usr/bin/env python3
"""Generate the SQL migration for the current column registry.

    PYTHONPATH=src python scripts/generate_sql_ddl.py > migration.sql

Emits: CREATE TABLE + indexes (from FolioSqlRepository.ddl(), i.e. the code
registry is the schema authority), the needs_review triage view, and the
Entra role grants for the platform Managed Identity. Run against the DB as
the Entra admin group after each `infra` deploy that changes the schema.
"""
import sys

sys.path.insert(0, "src")

from unittest.mock import MagicMock  # noqa: E402

from tfa_core.adapters.blob_cosmos import FolioSqlRepository  # noqa: E402

UAMI_NAME = sys.argv[1] if len(sys.argv) > 1 else "id-tfa-dev-southcentralus"

repo = FolioSqlRepository.__new__(FolioSqlRepository)
repo._table = "tfa_folio_lines"
repo._credential = MagicMock()

print("-- Generated from tfa_core.domain.models.FOLIO_COLUMNS — do not hand-edit.\n")
print(repo.ddl())
print(f"""
-- Human triage queue
CREATE VIEW vw_needs_review AS
SELECT id, document_id, file_name, identifier_name, folio_hotel_name,
       invoice_number, currency, year_month, extraction_warnings,
       ingested_at_utc
FROM tfa_folio_lines
WHERE review_status = 'needs_review';
GO

-- Managed Identity access (run as Entra admin; name = the UAMI resource name)
CREATE USER [{UAMI_NAME}] FROM EXTERNAL PROVIDER;
ALTER ROLE db_datareader ADD MEMBER [{UAMI_NAME}];
ALTER ROLE db_datawriter ADD MEMBER [{UAMI_NAME}];
GRANT EXECUTE TO [{UAMI_NAME}];
""")
