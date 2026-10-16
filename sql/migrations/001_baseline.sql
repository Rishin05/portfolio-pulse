-- Portfolio Pulse: SQLite schema (educational analytics, not financial advice)
-- All monetary amounts are stored in the account's currency (CAD for the MVP).
-- Timestamps are ISO-8601 strings in UTC (e.g. 2026-10-15T14:30:00Z).
-- Dates are ISO-8601 calendar dates (YYYY-MM-DD) in the account's local market day.

PRAGMA foreign_keys = ON;

-- Where every row came from. is_demo = 1 means synthetic data that must never be
-- presented as real financial information.
CREATE TABLE IF NOT EXISTS source (
    source_id     TEXT PRIMARY KEY,          -- e.g. 'synthetic', 'csv_import', 'finnhub', 'google_news_rss'
    display_name  TEXT NOT NULL,
    kind          TEXT NOT NULL CHECK (kind IN ('synthetic','file','api','rss')),
    is_demo       INTEGER NOT NULL CHECK (is_demo IN (0,1)),
    usage_note    TEXT                        -- licensing / usage caveat shown in the report
);

-- One row per ingestion run (file import, API pull, demo generation).
CREATE TABLE IF NOT EXISTS import_batch (
    batch_id       TEXT PRIMARY KEY,
    source_id      TEXT NOT NULL REFERENCES source(source_id),
    description    TEXT,                       -- file name or job name
    content_sha256 TEXT,                       -- hash of the input file, if any
    started_at_utc TEXT NOT NULL,
    rows_read      INTEGER NOT NULL DEFAULT 0,
    rows_inserted  INTEGER NOT NULL DEFAULT 0,
    rows_skipped   INTEGER NOT NULL DEFAULT 0, -- duplicates already in the database
    rows_rejected  INTEGER NOT NULL DEFAULT 0, -- failed validation
    dry_run        INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS account (
    account_id     TEXT PRIMARY KEY,           -- your own stable label, never a real account number
    display_name   TEXT NOT NULL,
    account_type   TEXT,                       -- TFSA, RRSP, FHSA, non-registered, ...
    base_currency  TEXT NOT NULL DEFAULT 'CAD' CHECK (length(base_currency) = 3),
    is_demo        INTEGER NOT NULL CHECK (is_demo IN (0,1))
);

-- End-of-day value per account per date. One row per (account, date): re-running a
-- load replaces nothing silently; duplicates are skipped and counted.
CREATE TABLE IF NOT EXISTS daily_snapshot (
    account_id     TEXT NOT NULL REFERENCES account(account_id),
    as_of_date     TEXT NOT NULL,              -- YYYY-MM-DD
    end_value      REAL NOT NULL CHECK (end_value >= 0),
    currency       TEXT NOT NULL CHECK (length(currency) = 3),
    quality_flag   TEXT NOT NULL CHECK (quality_flag IN ('reported','estimated','demo')),
    source_id      TEXT NOT NULL REFERENCES source(source_id),
    batch_id       TEXT NOT NULL REFERENCES import_batch(batch_id),
    loaded_at_utc  TEXT NOT NULL,
    PRIMARY KEY (account_id, as_of_date)
);

-- Money moving into (+) or out of (-) an account. is_external = 1 means the flow is a
-- contribution/withdrawal (NOT investment performance). Dividends and fees are
-- is_external = 0 because they are part of investment return.
CREATE TABLE IF NOT EXISTS cash_flow (
    cash_flow_id   TEXT PRIMARY KEY,           -- deterministic hash of the natural key
    account_id     TEXT NOT NULL REFERENCES account(account_id),
    flow_date      TEXT NOT NULL,              -- YYYY-MM-DD
    flow_type      TEXT NOT NULL CHECK (flow_type IN
                     ('DEPOSIT','WITHDRAWAL','TRANSFER_IN','TRANSFER_OUT',
                      'DIVIDEND','INTEREST','FEE','OTHER')),
    amount         REAL NOT NULL,              -- signed: + into the account, - out of it
    currency       TEXT NOT NULL CHECK (length(currency) = 3),
    is_external    INTEGER NOT NULL CHECK (is_external IN (0,1)),
    description    TEXT,
    source_id      TEXT NOT NULL REFERENCES source(source_id),
    batch_id       TEXT NOT NULL REFERENCES import_batch(batch_id),
    loaded_at_utc  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_cash_flow_account_date ON cash_flow(account_id, flow_date);

CREATE TABLE IF NOT EXISTS watchlist (
    symbol         TEXT PRIMARY KEY,           -- e.g. 'SHOP', 'RY.TO'
    company_name   TEXT NOT NULL,
    exchange       TEXT,
    currency       TEXT CHECK (currency IS NULL OR length(currency) = 3),
    news_query     TEXT,                       -- search phrase for RSS sources
    active         INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0,1)),
    added_at_utc   TEXT NOT NULL
);

-- Headlines only: we store the link to the original article, never the article body.
CREATE TABLE IF NOT EXISTS news_article (
    article_id        TEXT PRIMARY KEY,        -- sha256 of the normalized URL
    symbol            TEXT NOT NULL REFERENCES watchlist(symbol),
    headline          TEXT NOT NULL,
    url               TEXT NOT NULL,
    publisher         TEXT,
    published_at_utc  TEXT,                    -- NULL if the source gave no timestamp
    fetched_at_utc    TEXT NOT NULL,
    source_id         TEXT NOT NULL REFERENCES source(source_id),
    batch_id          TEXT NOT NULL REFERENCES import_batch(batch_id)
);
CREATE INDEX IF NOT EXISTS ix_news_symbol_time ON news_article(symbol, published_at_utc);

-- -----------------------------------------------------------------------------
-- Daily performance per account.
-- start_value          = previous snapshot's end_value (NULL on the first day)
-- net_external_flow    = sum of external flows dated after the previous snapshot,
--                        up to and including this date
-- investment_change    = end - start - net_external_flow   (dollars the market made/lost)
-- investment_return    = investment_change / (start + net_external_flow)
--   Assumption: external flows land at the START of the day (so new money is exposed
--   to that day's move). This is a simplification - see the project notes.
-- -----------------------------------------------------------------------------
DROP VIEW IF EXISTS v_daily_performance;
CREATE VIEW v_daily_performance AS
WITH ordered AS (
    SELECT s.*,
           LAG(s.as_of_date) OVER w AS prev_date,
           LAG(s.end_value)  OVER w AS start_value
    FROM daily_snapshot s
    WINDOW w AS (PARTITION BY s.account_id ORDER BY s.as_of_date)
),
flows AS (
    SELECT o.account_id, o.as_of_date,
           COALESCE(SUM(CASE WHEN c.is_external = 1 THEN c.amount END), 0) AS net_external_flow,
           COALESCE(SUM(CASE WHEN c.is_external = 0 THEN c.amount END), 0) AS internal_flow,
           COUNT(c.cash_flow_id) AS flow_count
    FROM ordered o
    LEFT JOIN cash_flow c
      ON c.account_id = o.account_id
     AND c.flow_date <= o.as_of_date
     AND (o.prev_date IS NULL OR c.flow_date > o.prev_date)
    GROUP BY o.account_id, o.as_of_date
)
SELECT o.account_id,
       o.as_of_date,
       o.prev_date,
       o.start_value,
       o.end_value,
       o.currency,
       f.net_external_flow,
       f.internal_flow,
       f.flow_count,
       o.end_value - o.start_value                              AS total_change,
       o.end_value - o.start_value - f.net_external_flow        AS investment_change,
       CASE WHEN o.start_value IS NULL THEN NULL
            WHEN (o.start_value + f.net_external_flow) <= 0 THEN NULL
            ELSE (o.end_value - o.start_value - f.net_external_flow)
                 / (o.start_value + f.net_external_flow)
       END                                                      AS investment_return,
       CAST(julianday(o.as_of_date) - julianday(o.prev_date) AS INTEGER) AS days_since_prev,
       o.quality_flag,
       o.source_id
FROM ordered o
JOIN flows f ON f.account_id = o.account_id AND f.as_of_date = o.as_of_date;
