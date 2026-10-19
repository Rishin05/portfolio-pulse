-- 003_fx_cad: CAD reporting views. Stored amounts stay in native currency; these views add CAD beside them.
-- Rate rule (confirmed 2026-10-19): latest rate ON OR BEFORE the row's date, never a later one.
--   gap 0 days = reported; 1-5 = carried_forward (weekends/holidays); >5 = stale (converted, flagged);
--   no earlier rate = missing (CAD value NULL). CAD amounts = identity (rate 1).
-- Rate source: rows whose source_id is 'synthetic' use synthetic DEMO rates; all other rows use
--   Bank of Canada ('boc_valet'). Demo figures never mix with real rates.
-- Do not edit once applied. Add 004_... instead.

-- Each rate with the date the next one starts, so a row can join to the one rate in force on its date.
CREATE VIEW v_fx_rate_cad AS
SELECT base_currency AS currency,
       source_id,
       rate_date,
       rate,
       LEAD(rate_date) OVER (PARTITION BY base_currency, source_id ORDER BY rate_date) AS next_rate_date
FROM fx_rate
WHERE quote_currency = 'CAD';

-- Daily account values in CAD.
CREATE VIEW v_snapshot_cad AS
WITH j AS (
    SELECT s.account_id, s.as_of_date, s.end_value, s.currency, s.quality_flag, s.source_id,
           r.rate, r.rate_date,
           CAST(julianday(s.as_of_date) - julianday(r.rate_date) AS INTEGER) AS gap
    FROM daily_snapshot s
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
       quality_flag, source_id
FROM j;

-- Holdings (including CASH.USD rows) in CAD.
CREATE VIEW v_holding_cad AS
WITH j AS (
    SELECT h.account_id, h.as_of_date, h.symbol, h.asset_class, h.quantity,
           h.market_value, h.market_value_currency AS currency, h.quality_flag, h.source_id,
           r.rate, r.rate_date,
           CAST(julianday(h.as_of_date) - julianday(r.rate_date) AS INTEGER) AS gap
    FROM holding_snapshot h
    LEFT JOIN v_fx_rate_cad r
      ON h.market_value_currency <> 'CAD'
     AND r.currency = h.market_value_currency
     AND r.source_id = CASE WHEN h.source_id = 'synthetic' THEN 'synthetic' ELSE 'boc_valet' END
     AND r.rate_date <= h.as_of_date
     AND (r.next_rate_date IS NULL OR h.as_of_date < r.next_rate_date)
)
SELECT account_id, as_of_date, symbol, asset_class, quantity, market_value, currency,
       CASE WHEN currency = 'CAD' THEN 1.0 ELSE rate END              AS fx_rate,
       CASE WHEN currency = 'CAD' THEN as_of_date ELSE rate_date END  AS fx_rate_date,
       CASE WHEN currency = 'CAD' THEN 0 ELSE gap END                 AS fx_gap_days,
       CASE WHEN currency = 'CAD' THEN 'identity'
            WHEN rate IS NULL THEN 'missing'
            WHEN gap = 0 THEN 'reported'
            WHEN gap <= 5 THEN 'carried_forward'
            ELSE 'stale' END                                          AS fx_status,
       CASE WHEN currency = 'CAD' THEN market_value ELSE market_value * rate END AS market_value_cad,
       quality_flag, source_id
FROM j;

-- Activity ledger in CAD. FX_CONVERSION rows convert both sides; the difference at the
-- benchmark rate is the conversion cost (negative = cost to you).
CREATE VIEW v_activity_cad AS
WITH j AS (
    SELECT a.*,
           r.rate  AS a_rate,  r.rate_date  AS a_rate_date,
           CAST(julianday(a.trade_date) - julianday(r.rate_date) AS INTEGER)  AS a_gap,
           rc.rate AS c_rate, rc.rate_date AS c_rate_date,
           CAST(julianday(a.trade_date) - julianday(rc.rate_date) AS INTEGER) AS c_gap
    FROM activity a
    LEFT JOIN v_fx_rate_cad r
      ON a.currency <> 'CAD'
     AND r.currency = a.currency
     AND r.source_id = CASE WHEN a.source_id = 'synthetic' THEN 'synthetic' ELSE 'boc_valet' END
     AND r.rate_date <= a.trade_date
     AND (r.next_rate_date IS NULL OR a.trade_date < r.next_rate_date)
    LEFT JOIN v_fx_rate_cad rc
      ON a.counter_currency IS NOT NULL
     AND a.counter_currency <> 'CAD'
     AND rc.currency = a.counter_currency
     AND rc.source_id = CASE WHEN a.source_id = 'synthetic' THEN 'synthetic' ELSE 'boc_valet' END
     AND rc.rate_date <= a.trade_date
     AND (rc.next_rate_date IS NULL OR a.trade_date < rc.next_rate_date)
),
c AS (
    SELECT j.*,
           CASE WHEN currency = 'CAD' THEN amount ELSE amount * a_rate END AS amount_cad,
           CASE WHEN counter_currency IS NULL THEN NULL
                WHEN counter_currency = 'CAD' THEN counter_amount
                ELSE counter_amount * c_rate END                           AS counter_amount_cad,
           CASE WHEN currency = 'CAD' THEN 'identity'
                WHEN a_rate IS NULL THEN 'missing'
                WHEN a_gap = 0 THEN 'reported'
                WHEN a_gap <= 5 THEN 'carried_forward'
                ELSE 'stale' END                                           AS fx_status,
           CASE WHEN counter_currency IS NULL THEN NULL
                WHEN counter_currency = 'CAD' THEN 'identity'
                WHEN c_rate IS NULL THEN 'missing'
                WHEN c_gap = 0 THEN 'reported'
                WHEN c_gap <= 5 THEN 'carried_forward'
                ELSE 'stale' END                                           AS counter_fx_status
    FROM j
)
SELECT activity_id, account_id, activity_type, raw_type, trade_date, symbol, quantity,
       amount, currency,
       CASE WHEN currency = 'CAD' THEN 1.0 ELSE a_rate END               AS fx_rate,
       CASE WHEN currency = 'CAD' THEN trade_date ELSE a_rate_date END   AS fx_rate_date,
       CASE WHEN currency = 'CAD' THEN 0 ELSE a_gap END                  AS fx_gap_days,
       fx_status,
       amount_cad,
       counter_amount, counter_currency, counter_amount_cad, counter_fx_status,
       reported_fx_rate,
       CASE WHEN activity_type = 'FX_CONVERSION' THEN amount_cad + counter_amount_cad END
                                                                         AS fx_conversion_cost_cad,
       quality_flag, source_id
FROM c;