/* =============================================================================
   INCIDENT-2025-08 remediation.

   Defect: document identity was derived from blob metadata (content_md5 with
   an ETag fallback). content_md5 is never populated by our writer, so the
   ETag branch always ran, and an ETag changes on every write. Each re-sync or
   reprocess of the SAME file therefore inserted an ADDITIONAL row in
   dbo.documents, inflating reconciliation totals linearly with reprocess count.

   Code fix: identity is now sha256(content). This migration cleans historical
   duplicates and adds the constraint that makes recurrence impossible.

   RUN ORDER: deploy code first (stops new duplicates), then this migration.
   ========================================================================== */

SET XACT_ABORT ON;
BEGIN TRANSACTION;

/* 1. Snapshot the damage before touching anything. ----------------------- */
IF OBJECT_ID('dbo.incident_2025_08_duplicates') IS NULL
CREATE TABLE dbo.incident_2025_08_duplicates (
    identifier_name  NVARCHAR(200),
    file_name        NVARCHAR(400),
    document_id      CHAR(32),
    ingested_at_utc  DATETIME2(3),
    usd_net_difference DECIMAL(19,4),
    kept             BIT,
    captured_at_utc  DATETIME2(3) NOT NULL DEFAULT SYSUTCDATETIME()
);

;WITH ranked AS (
    SELECT document_id, identifier_name, file_name, ingested_at_utc,
           usd_net_difference,
           ROW_NUMBER() OVER (PARTITION BY identifier_name, file_name
                              ORDER BY ingested_at_utc DESC, doc_seq DESC) AS rn,
           COUNT(*)   OVER (PARTITION BY identifier_name, file_name) AS copies
    FROM dbo.documents
)
INSERT INTO dbo.incident_2025_08_duplicates
    (identifier_name, file_name, document_id, ingested_at_utc, usd_net_difference, kept)
SELECT identifier_name, file_name, document_id, ingested_at_utc, usd_net_difference,
       CASE WHEN rn = 1 THEN 1 ELSE 0 END
FROM ranked
WHERE copies > 1;

/* 2. Keep the most recent extraction per physical file; delete the rest.
      folio_lines clears via ON DELETE CASCADE.
      Reviewed/corrected rows win over auto-approved ones so human work is
      never discarded. ------------------------------------------------------ */
;WITH ranked AS (
    SELECT document_id,
           ROW_NUMBER() OVER (
               PARTITION BY identifier_name, file_name
               ORDER BY CASE WHEN review_status = 'reviewed' THEN 0 ELSE 1 END,
                        ingested_at_utc DESC, doc_seq DESC) AS rn
    FROM dbo.documents
)
DELETE d
FROM dbo.documents d
JOIN ranked r ON r.document_id = d.document_id
WHERE r.rn > 1;

/* 3. Make recurrence structurally impossible. ----------------------------- */
IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name = 'UQ_documents_identity')
CREATE UNIQUE INDEX UQ_documents_identity
    ON dbo.documents(identifier_name, file_name);

COMMIT TRANSACTION;
GO

INSERT INTO dbo.schema_migrations (version, description)
SELECT '002', 'INCIDENT-2025-08: dedupe documents, enforce one row per physical file'
WHERE NOT EXISTS (SELECT 1 FROM dbo.schema_migrations WHERE version = '002');
GO
