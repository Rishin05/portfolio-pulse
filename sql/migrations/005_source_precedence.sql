-- 005_source_precedence: reconstructed values, one best value per account-day, quality labels.
-- Precedence (lower wins): broker-reported snapshot (snaptrade/synthetic = 1, csv_import = 2)
--   > reconstructed from holdings x prices (3). Reconstructions with priced_share < 1 are never used.
-- Reconciliation: reported vs reconstructed differing by more than 0.5% = 'mismatch' (reported still wins).
-- Quality label (worst wins): DEMO > MISSING > STALE > estimated > reported.
-- v_daily_performance (v1) is untouched and still reads daily_snapshot directly.
-- Do not edit once applied; add 006_... instead.

CREATE TABLE reconstructed_snapshot (
    account_id     TEXT NOT NULL REFERENCES account(account_id),
    as_of_date     TEXT NOT NULL,                 -- YYYY-MM-DD
    end_value      REAL NOT NULL CHECK (end_value >= 0),
    currency       TEXT NOT NULL CHECK (length(currency) = 3),
    method         TEXT NOT NULL DEFAULT 'holdings_x_prices' CHECK (method IN ('holdings_x_prices')),
    priced_share   REAL NOT NULL CHECK (priced_share >= 0 AND priced_share <= 1),
    quality_flag   TEXT NOT NULL CHECK (quality_flag IN ('estimated','demo')),
    source_id      TEXT NOT NULL REFERENCES source(source_id),   -- price source used
    batch_id       TEXT NOT NULL REFERENCES import_batch(batch_id),
    loaded_at_utc  TEXT NOT NULL,
    notes          TEXT,
    PRIMARY KEY (account_id, as_of_date)
);

CREATE VIEW v_snapshot_best AS
WITH candidates AS (
    SELECT account_id, as_of_date, end_value, currency, quality_flag, source_id,
           'reported' AS method,
           CASE source_id WHEN 'csv_import' THEN 2 ELSE 1 END AS precedence
    FROM daily_snapshot
    UNION ALL
    SELECT account_id, as_of_date, end_value, currency, quality_flag, source_id,
           'reconstructed', 3
    FROM reconstructed_snapshot
    WHERE priced_share >= 1
),
ranked AS (
    SELECT c.*,
           ROW_NUMBER() OVER w_order AS rn,
           COUNT(*) OVER w_all AS candidates,
           MIN(CASE WHEN method = 'reported' THEN end_value END) OVER w_all AS reported_value,
           MIN(CASE WHEN method = 'reconstructed' THEN end_value END) OVER w_all AS reconstructed_value
    FROM candidates c
    WINDOW w_all AS (PARTITION BY account_id, as_of_date),
           w_order AS (PARTITION BY account_id, as_of_date ORDER BY precedence, source_id)
)
SELECT account_id, as_of_date, end_value, currency, quality_flag, source_id, method,
       precedence, candidates, reported_value, reconstructed_value,
       CASE WHEN reported_value IS NULL OR reconstructed_value IS NULL THEN NULL
            WHEN reported_value = 0 THEN CASE WHEN reconstructed_value = 0 THEN 'ok' ELSE 'mismatch' END
            WHEN ABS(reconstructed_value - reported_value) / reported_value > 0.005 THEN 'mismatch'
            ELSE 'ok' END AS reconciliation
FROM ranked
WHERE rn = 1;

-- Rebuild the CAD snapshot view on top of the best value (same columns as 003, plus lineage and label).
DROP VIEW v_snapshot_cad;
CREATE VIEW v_snapshot_cad AS
WITH j AS (
    SELECT s.*, r.rate, r.rate_date,
           CAST(julianday(s.as_of_date) - julianday(r.rate_date) AS INTEGER) AS gap
    FROM v_snapshot_best s
    LEFT JOIN v_fx_rate_cad r
      ON s.currency <> 'CAD'
     AND r.currency = s.currency
     AND r.source_id = CASE WHEN s.source_id = 'synthetic' THEN 'synthetic' ELSE 'boc_valet' END
     AND r.rate_date <= s.as_of_date
     AND (r.next_rate_date IS NULL OR s.as_of_date < r.next_rate_date)
)
SELECT account_id, as_of_date, end_value, currency,
       CASE WHEN currency = 'CAD' THEN 1.0 ELSE rate END              AS fx_rate,
       CASE WHEN currency = 'CAD' THEN as_of_date ELSE rate_date END  AS fx_rate_date,
       CASE WHEN currency = 'CAD' THEN 0 ELSE gap END                 AS fx_gap_days,
       CASE WHEN currency = 'CAD' THEN 'identity'
            WHEN rate IS NULL THEN 'missing'
            WHEN gap = 0 THEN 'reported'
            WHEN gap <= 5 THEN 'carried_forward'
            ELSE 'stale' END                                          AS fx_status,
       CASE WHEN currency = 'CAD' THEN end_value ELSE end_value * rate END AS end_value_cad,
       quality_flag, source_id, method, precedence, candidates, reconciliation,
       CASE WHEN quality_flag = 'demo' THEN 'DEMO'
            WHEN currency <> 'CAD' AND rate IS NULL THEN 'MISSING'
            WHEN currency <> 'CAD' AND gap > 5 THEN 'STALE'
            WHEN method = 'reconstructed' OR quality_flag = 'estimated'
                 OR (currency <> 'CAD' AND gap BETWEEN 1 AND 5) THEN 'estimated'
            ELSE 'reported' END                                       AS quality_label
FROM j;

-- Stored engine results with a quality label (account: this day and the previous one; household: worst account).
CREATE VIEW v_perf_account_daily AS
SELECT p.*,
       CASE WHEN p.household_id IN ('DEMO','PAPER') OR s1.quality_label = 'DEMO' THEN 'DEMO'
            WHEN p.fx_status = 'missing' THEN 'MISSING'
            WHEN p.fx_status = 'stale' THEN 'STALE'
            WHEN p.fx_status = 'carried_forward'
                 OR s1.quality_label = 'estimated' OR s0.quality_label = 'estimated' THEN 'estimated'
            ELSE 'reported' END AS quality_label
FROM perf_account_daily p
LEFT JOIN v_snapshot_cad s1 ON s1.account_id = p.account_id AND s1.as_of_date = p.as_of_date
LEFT JOIN v_snapshot_cad s0 ON s0.account_id = p.account_id AND s0.as_of_date = p.prev_date;

CREATE VIEW v_perf_household_daily AS
WITH worst AS (
    SELECT household_id, as_of_date,
           MAX(CASE quality_label WHEN 'DEMO' THEN 5 WHEN 'MISSING' THEN 4 WHEN 'STALE' THEN 3
                                  WHEN 'estimated' THEN 2 ELSE 1 END) AS rank
    FROM v_perf_account_daily
    GROUP BY household_id, as_of_date
)
SELECT h.*,
       CASE w.rank WHEN 5 THEN 'DEMO' WHEN 4 THEN 'MISSING' WHEN 3 THEN 'STALE'
                   WHEN 2 THEN 'estimated' ELSE 'reported' END AS quality_label
FROM perf_household_daily h
JOIN worst w ON w.household_id = h.household_id AND w.as_of_date = h.as_of_date;