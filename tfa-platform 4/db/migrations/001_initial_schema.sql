/* =============================================================================
   TFA Platform — initial schema (Azure SQL)
   Forward-only migration. Run before first app deploy of v2.0.0.

   Design notes:
   - `documents` is the aggregate root; `folio_lines` are its children.
   - `document_id` (sha256 of identifier/filename/content) is the idempotency
     key: reprocessing a file replaces its rows atomically, never duplicates.
   - Extraction provenance (model, prompt version, attempts) is stored so any
     historical row is reproducible and explainable to a financial auditor.
   - Money is DECIMAL(19,4) everywhere. Never FLOAT.
   ========================================================================== */

SET ANSI_NULLS ON;
SET QUOTED_IDENTIFIER ON;
GO

-- ---------------------------------------------------------------------------
-- Schema version tracking (forward-only migrations)
-- ---------------------------------------------------------------------------
IF OBJECT_ID('dbo.schema_migrations') IS NULL
CREATE TABLE dbo.schema_migrations (
    version         VARCHAR(20)   NOT NULL PRIMARY KEY,
    description     NVARCHAR(200) NOT NULL,
    applied_at_utc  DATETIME2(3)  NOT NULL DEFAULT SYSUTCDATETIME()
);
GO

-- ---------------------------------------------------------------------------
-- documents — one row per source file
-- ---------------------------------------------------------------------------
IF OBJECT_ID('dbo.documents') IS NULL
CREATE TABLE dbo.documents (
    document_id             CHAR(32)       NOT NULL CONSTRAINT PK_documents PRIMARY KEY NONCLUSTERED,
    doc_seq                 BIGINT         IDENTITY(1,1) NOT NULL,

    -- Provenance
    identifier_name         NVARCHAR(200)  NOT NULL,
    file_name               NVARCHAR(400)  NOT NULL,
    blob_container          NVARCHAR(100)  NOT NULL,
    blob_path               NVARCHAR(1000) NOT NULL,
    content_md5             VARCHAR(64)    NULL,
    uploading_person_name   NVARCHAR(200)  NULL,
    source_system           VARCHAR(30)     NOT NULL DEFAULT 'sharepoint',  -- sharepoint | api_upload
    sharepoint_item_id      NVARCHAR(200)  NULL,

    -- Classification
    document_type           VARCHAR(40)    NOT NULL,   -- hotel_folio | air_ticket_invoice | ...
    contains_multiple_docs  BIT            NOT NULL DEFAULT 0,
    unreadable_reason       NVARCHAR(500)  NULL,

    -- Vendor / customer
    vendor_name             NVARCHAR(400)  NULL,       -- hotel or agency
    vendor_address          NVARCHAR(600)  NULL,
    country                 NVARCHAR(100)  NULL,
    supplier_tax_id         NVARCHAR(60)   NULL,
    customer_name           NVARCHAR(400)  NULL,
    customer_tax_id         NVARCHAR(60)   NULL,
    e_invoice_id            NVARCHAR(200)  NULL,       -- CUFE / CAE / UUID

    -- Invoice header
    invoice_number          NVARCHAR(120)  NULL,
    booking_number          NVARCHAR(120)  NULL,
    reservation_number      NVARCHAR(120)  NULL,
    gds_record_locator      NVARCHAR(60)   NULL,
    references_invoice_no   NVARCHAR(120)  NULL,       -- credit notes
    folio_date              DATE           NULL,
    guest_name              NVARCHAR(300)  NULL,
    travel_route            NVARCHAR(200)  NULL,
    checkin_date            DATE           NULL,
    checkin_time            TIME(0)        NULL,
    checkout_date           DATE           NULL,
    checkout_time           TIME(0)        NULL,
    number_of_nights        INT            NULL,

    -- Money (document currency)
    currency                CHAR(3)        NULL,
    total_amount            DECIMAL(19,4)  NULL,
    total_amount_from_words DECIMAL(19,4)  NULL,
    informational_tax_amt   DECIMAL(19,4)  NULL,       -- IVA Contenido etc. (NOT additive)

    -- Reconciliation period
    year_month              CHAR(7)        NULL,       -- YYYY-MM (checkout month)
    reconciliation_year     SMALLINT       NULL,
    reconciliation_quarter  CHAR(7)        NULL,       -- YYYY-Qn

    -- Rate audit rollup (document level)
    matched_hotel_name      NVARCHAR(400)  NULL,
    negotiated_rate_usd     DECIMAL(19,4)  NULL,
    fx_usd_per_unit         DECIMAL(19,8)  NULL,
    fx_rate_date            DATE           NULL,
    rate_issue_found        BIT            NOT NULL DEFAULT 0,
    nights_overcharged      INT            NOT NULL DEFAULT 0,
    nights_undercharged     INT            NOT NULL DEFAULT 0,
    usd_total_overcharged   DECIMAL(19,4)  NOT NULL DEFAULT 0,
    usd_total_undercharged  DECIMAL(19,4)  NOT NULL DEFAULT 0,
    usd_net_difference      DECIMAL(19,4)  NOT NULL DEFAULT 0,

    -- Quality / workflow
    review_status           VARCHAR(20)    NOT NULL DEFAULT 'auto_approved',
                            -- auto_approved | needs_review | reviewed | rejected
    review_reason           NVARCHAR(2000) NULL,
    low_confidence_fields   NVARCHAR(1000) NULL,       -- ';' joined
    reviewed_by             NVARCHAR(200)  NULL,
    reviewed_at_utc         DATETIME2(3)   NULL,

    -- Extraction provenance (AI governance)
    extraction_model        NVARCHAR(100)  NULL,
    extraction_provider     VARCHAR(30)    NULL,
    prompt_version          VARCHAR(20)    NULL,
    extraction_attempts     TINYINT        NULL,
    ocr_confidence_floor    DECIMAL(5,4)   NULL,
    correlation_id          VARCHAR(64)    NULL,

    ingested_at_utc         DATETIME2(3)   NOT NULL DEFAULT SYSUTCDATETIME(),
    updated_at_utc          DATETIME2(3)   NOT NULL DEFAULT SYSUTCDATETIME(),

    CONSTRAINT CK_documents_review_status CHECK (
        review_status IN ('auto_approved','needs_review','reviewed','rejected')),
    CONSTRAINT CK_documents_doc_type CHECK (
        document_type IN ('hotel_folio','air_ticket_invoice','agency_fee_invoice',
                          'ap_system_document','credit_note','other_travel_invoice',
                          'not_an_invoice'))
);
GO

IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name='CIX_documents_seq')
CREATE UNIQUE CLUSTERED INDEX CIX_documents_seq ON dbo.documents(doc_seq);
GO
IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name='IX_documents_identifier')
CREATE INDEX IX_documents_identifier ON dbo.documents(identifier_name)
    INCLUDE (document_type, review_status, usd_net_difference);
GO
IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name='IX_documents_period')
CREATE INDEX IX_documents_period ON dbo.documents(year_month, country)
    INCLUDE (matched_hotel_name, usd_total_overcharged, usd_net_difference);
GO
IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name='IX_documents_review')
CREATE INDEX IX_documents_review ON dbo.documents(review_status, ingested_at_utc)
    WHERE review_status IN ('needs_review','rejected');
GO
IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name='IX_documents_hotel')
CREATE INDEX IX_documents_hotel ON dbo.documents(matched_hotel_name);
GO

-- ---------------------------------------------------------------------------
-- folio_lines — one row per charge line
-- ---------------------------------------------------------------------------
IF OBJECT_ID('dbo.folio_lines') IS NULL
CREATE TABLE dbo.folio_lines (
    line_id                 BIGINT         IDENTITY(1,1) NOT NULL CONSTRAINT PK_folio_lines PRIMARY KEY,
    document_id             CHAR(32)       NOT NULL,
    line_no                 INT            NOT NULL,
    line_kind               VARCHAR(20)    NOT NULL,   -- nightly | ancillary
    category                VARCHAR(30)    NULL,       -- tax | airfare | late_checkout | ...

    stay_date               DATE           NULL,
    nights                  INT            NOT NULL DEFAULT 1,
    description             NVARCHAR(1000) NULL,       -- verbatim, source language
    english_description     NVARCHAR(1000) NULL,

    amount                  DECIMAL(19,4)  NOT NULL,   -- line total, document currency
    per_night_amount        DECIMAL(19,4)  NULL,       -- amount / nights
    usd_per_night_amount    DECIMAL(19,4)  NULL,

    -- Per-line audit
    negotiated_rate_usd     DECIMAL(19,4)  NULL,
    variance_usd            DECIMAL(19,4)  NULL,       -- + overcharge, - undercharge
    is_overcharged          BIT            NOT NULL DEFAULT 0,

    created_at_utc          DATETIME2(3)   NOT NULL DEFAULT SYSUTCDATETIME(),

    CONSTRAINT FK_folio_lines_document FOREIGN KEY (document_id)
        REFERENCES dbo.documents(document_id) ON DELETE CASCADE,
    CONSTRAINT UQ_folio_lines_doc_line UNIQUE (document_id, line_no),
    CONSTRAINT CK_folio_lines_kind CHECK (line_kind IN ('nightly','ancillary'))
);
GO
IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name='IX_folio_lines_document')
CREATE INDEX IX_folio_lines_document ON dbo.folio_lines(document_id) INCLUDE (line_kind, amount);
GO

-- ---------------------------------------------------------------------------
-- negotiated_rates — rate directory (SAP-fed in prod), versioned
-- ---------------------------------------------------------------------------
IF OBJECT_ID('dbo.negotiated_rates') IS NULL
CREATE TABLE dbo.negotiated_rates (
    rate_id             INT            IDENTITY(1,1) NOT NULL CONSTRAINT PK_negotiated_rates PRIMARY KEY,
    hotel_name          NVARCHAR(400)  NOT NULL,
    hotel_name_norm     NVARCHAR(400)  NOT NULL,       -- lowercased, punctuation-stripped
    country             NVARCHAR(100)  NULL,
    city                NVARCHAR(200)  NULL,
    nightly_rate_usd    DECIMAL(19,4)  NOT NULL,
    valid_from          DATE           NOT NULL,
    valid_to            DATE           NULL,           -- NULL = open-ended
    source              VARCHAR(30)    NOT NULL DEFAULT 'manual',   -- manual | sap
    updated_by          NVARCHAR(200)  NULL,
    updated_at_utc      DATETIME2(3)   NOT NULL DEFAULT SYSUTCDATETIME(),
    CONSTRAINT CK_negotiated_rates_dates CHECK (valid_to IS NULL OR valid_to >= valid_from)
);
GO
IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name='IX_negotiated_rates_lookup')
CREATE INDEX IX_negotiated_rates_lookup
    ON dbo.negotiated_rates(hotel_name_norm, valid_from) INCLUDE (nightly_rate_usd, country, valid_to);
GO

-- ---------------------------------------------------------------------------
-- fx_rates — dated FX. Reconciliation uses the rate effective on folio_date.
-- ---------------------------------------------------------------------------
IF OBJECT_ID('dbo.fx_rates') IS NULL
CREATE TABLE dbo.fx_rates (
    currency        CHAR(3)        NOT NULL,
    rate_date       DATE           NOT NULL,
    usd_per_unit    DECIMAL(19,8)  NOT NULL,
    source          VARCHAR(40)    NOT NULL DEFAULT 'manual',
    updated_at_utc  DATETIME2(3)   NOT NULL DEFAULT SYSUTCDATETIME(),
    CONSTRAINT PK_fx_rates PRIMARY KEY (currency, rate_date)
);
GO

-- ---------------------------------------------------------------------------
-- audit_log — append-only. No UPDATE/DELETE grants in any role.
-- ---------------------------------------------------------------------------
IF OBJECT_ID('dbo.audit_log') IS NULL
CREATE TABLE dbo.audit_log (
    audit_id        BIGINT         IDENTITY(1,1) NOT NULL CONSTRAINT PK_audit_log PRIMARY KEY,
    occurred_at_utc DATETIME2(3)   NOT NULL DEFAULT SYSUTCDATETIME(),
    actor           NVARCHAR(200)  NOT NULL,      -- UPN or 'system:worker'
    action          VARCHAR(50)    NOT NULL,      -- ingested | reprocessed | approved | corrected | rejected | rate_changed
    entity_type     VARCHAR(30)    NOT NULL,      -- document | negotiated_rate | fx_rate
    entity_id       NVARCHAR(100)  NOT NULL,
    before_json     NVARCHAR(MAX)  NULL,
    after_json      NVARCHAR(MAX)  NULL,
    correlation_id  VARCHAR(64)    NULL
);
GO
IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name='IX_audit_log_entity')
CREATE INDEX IX_audit_log_entity ON dbo.audit_log(entity_type, entity_id, occurred_at_utc DESC);
GO

-- ---------------------------------------------------------------------------
-- vw_folio_report — denormalized reporting shape (legacy-compatible columns)
-- ---------------------------------------------------------------------------
IF OBJECT_ID('dbo.vw_folio_report') IS NOT NULL DROP VIEW dbo.vw_folio_report;
GO
CREATE VIEW dbo.vw_folio_report AS
SELECT
    d.document_id,
    l.line_id,
    d.file_name                                   AS file_name,
    d.vendor_name                                 AS folio_hotel_name,
    d.vendor_address                              AS hotel_address,
    d.country,
    d.document_type,
    d.folio_date,
    d.invoice_number,
    d.booking_number,
    d.reservation_number                          AS nro_reserv,
    d.gds_record_locator,
    d.e_invoice_id,
    d.supplier_tax_id,
    d.customer_name,
    d.customer_tax_id,
    d.travel_route,
    d.guest_name                                  AS guest,
    d.checkin_date                                AS checkin,
    CONVERT(VARCHAR(8), d.checkin_time, 108)      AS checkin_timestamp,
    d.checkout_date                               AS checkout,
    CONVERT(VARCHAR(8), d.checkout_time, 108)     AS checkout_timestamp,
    d.currency,
    d.number_of_nights                            AS number_of_days,
    l.english_description,
    d.reconciliation_year                         AS [year],
    d.reconciliation_quarter                      AS year_quarter,
    d.year_month,
    l.per_night_amount                            AS original_per_day_price,
    d.matched_hotel_name,
    d.negotiated_rate_usd                         AS negotiated_rate,
    d.fx_usd_per_unit                             AS currency_multiplier_rate,
    l.usd_per_night_amount                        AS usd_per_day_price,
    CASE WHEN d.rate_issue_found = 1 THEN 'Yes' ELSE 'No' END AS issue_found,
    d.nights_overcharged                          AS number_of_nights_overcharged,
    d.nights_undercharged                         AS number_of_nights_undercharged,
    d.usd_total_overcharged                       AS usd_total_overcharged_amount,
    d.usd_total_undercharged                      AS usd_total_undercharged_amount,
    d.usd_net_difference                          AS usd_total_amount_differ,
    d.identifier_name,
    d.uploading_person_name,
    d.review_status,
    d.review_reason                               AS extraction_warnings
FROM dbo.documents d
LEFT JOIN dbo.folio_lines l ON l.document_id = d.document_id
WHERE d.review_status <> 'rejected';
GO

-- ---------------------------------------------------------------------------
-- vw_review_queue — what a human must look at, oldest first
-- ---------------------------------------------------------------------------
IF OBJECT_ID('dbo.vw_review_queue') IS NOT NULL DROP VIEW dbo.vw_review_queue;
GO
CREATE VIEW dbo.vw_review_queue AS
SELECT
    d.document_id, d.identifier_name, d.file_name, d.document_type,
    d.vendor_name, d.country, d.currency, d.total_amount,
    d.review_reason, d.low_confidence_fields, d.contains_multiple_docs,
    d.unreadable_reason, d.extraction_attempts, d.ocr_confidence_floor,
    d.ingested_at_utc
FROM dbo.documents d
WHERE d.review_status = 'needs_review';
GO

INSERT INTO dbo.schema_migrations (version, description)
SELECT '001', 'Initial TFA v2 schema: documents, folio_lines, rates, fx, audit, views'
WHERE NOT EXISTS (SELECT 1 FROM dbo.schema_migrations WHERE version = '001');
GO
