-- 004_performance_v2: stored results of performance engine v2 (derived data).
-- Rebuilt in full by `python -m portfolio_pulse performance` (and run-daily) inside one transaction.
-- Source tables are never modified by the engine. Do not edit once applied; add 005_... instead.

CREATE TABLE perf_account_daily (
    account_id             TEXT NOT NULL REFERENCES account(account_id),
    household_id           TEXT NOT NULL CHECK (household_id IN ('DEMO','PAPER','REAL')),
    as_of_date             TEXT NOT NULL,          -- YYYY-MM-DD
    prev_date              TEXT,                   -- NULL on an account's first snapshot
    currency               TEXT NOT NULL CHECK (length(currency) = 3),
    start_value            REAL,                   -- native currency
    end_value              REAL NOT NULL,
    net_flow               REAL,                   -- external flows, native (at period-start rate)
    investment_change      REAL,
    return_native          REAL,                   -- e.g. the USD return of a USD account
    start_value_cad        REAL,                   -- V(t-1) x previous snapshot's rate
    end_value_cad          REAL,                   -- V(t) x this snapshot's rate
    net_flow_cad           REAL,
    investment_change_cad  REAL,
    return_cad             REAL,
    market_change_cad      REAL,                   -- investment_change_cad = market + fx
    fx_change_cad          REAL,
    currency_effect        REAL,                   -- (1 + return_cad) / (1 + return_native) - 1
    fx_status              TEXT NOT NULL CHECK (fx_status IN
                             ('identity','reported','carried_forward','stale','missing')),
    computed_at_utc        TEXT NOT NULL,
    PRIMARY KEY (account_id, as_of_date)
);

CREATE TABLE perf_household_daily (
    household_id           TEXT NOT NULL CHECK (household_id IN ('DEMO','PAPER','REAL')),
    as_of_date             TEXT NOT NULL,
    accounts               INTEGER NOT NULL,
    accounts_with_return   INTEGER NOT NULL,
    end_value_cad          REAL,
    external_flow_cad      REAL,                   -- own-account transfers excluded
    internal_transfer_cad  REAL,                   -- gross amount moved between your own accounts
    investment_change_cad  REAL,
    market_change_cad      REAL,
    fx_change_cad          REAL,
    return_cad             REAL,                   -- summed dollars / summed capital
    fx_status              TEXT NOT NULL CHECK (fx_status IN
                             ('identity','reported','carried_forward','stale','missing')),
    computed_at_utc        TEXT NOT NULL,
    PRIMARY KEY (household_id, as_of_date)
);