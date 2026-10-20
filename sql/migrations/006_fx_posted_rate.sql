-- 006_fx_posted_rate: dated FX quotes posted by an institution (e.g. a bank's USD/CAD rate).
-- Rates are quote currency per 1 base (CAD per 1 USD): same direction as BoC FXUSDCAD and fx_rate.rate.
--   customer_buy_rate  = CAD you PAY to receive 1 USD (you buy USD).
--   customer_sell_rate = CAD you RECEIVE for 1 USD  (you sell USD).
-- A posted rate applies only to its own rate_date; the calculator never carries it to other dates.
-- Percent/flat fees stay in fee_schedule; this table holds quotes, not fees.
-- Additive only: fee_schedule, dim_fee_schedule.csv and demo rows are unchanged.
-- Do not edit once applied; add 007_... instead.

CREATE TABLE fx_posted_rate (
    fx_posted_rate_id   TEXT PRIMARY KEY,
    institution_id      TEXT NOT NULL REFERENCES institution(institution_id),
    base_currency       TEXT NOT NULL CHECK (length(base_currency) = 3 AND base_currency = upper(base_currency)),
    quote_currency      TEXT NOT NULL CHECK (length(quote_currency) = 3 AND quote_currency = upper(quote_currency)),
    rate_date           TEXT NOT NULL CHECK (rate_date GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]'),
    channel             TEXT NOT NULL DEFAULT 'standard' CHECK (length(trim(channel)) > 0),
    customer_buy_rate   REAL CHECK (customer_buy_rate IS NULL OR customer_buy_rate > 0),
    customer_sell_rate  REAL CHECK (customer_sell_rate IS NULL OR customer_sell_rate > 0),
    source_url          TEXT NOT NULL CHECK (length(trim(source_url)) > 0),
    date_checked        TEXT NOT NULL CHECK (date_checked GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]'),
    notes               TEXT,
    is_demo             INTEGER NOT NULL CHECK (is_demo IN (0,1)),
    CHECK (base_currency <> quote_currency),
    CHECK (customer_buy_rate IS NOT NULL OR customer_sell_rate IS NOT NULL),
    CHECK (customer_buy_rate IS NULL OR customer_sell_rate IS NULL
           OR customer_buy_rate >= customer_sell_rate),
    CHECK (date_checked >= rate_date),          -- can't have confirmed a rate before it was posted
    UNIQUE (institution_id, base_currency, quote_currency, rate_date, channel)
);