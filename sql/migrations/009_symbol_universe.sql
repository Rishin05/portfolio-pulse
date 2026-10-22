-- 009_symbol_universe: a searchable catalogue of listed symbols. It is NOT the watchlist.
-- One row per (list, symbol, exchange); list_id e.g. IVV, IWM, XIU, XEQT, MANUAL.
-- Each list mirrors its latest file: the same file again changes nothing, a newer file adds,
-- updates and removes rows, an older file is refused. Watchlist rows are never touched.
-- Currency is the trading currency derived from the exchange, not the fund's reporting currency.
-- source_file holds a file name only, never a full path.
-- Do not edit once applied; add 010_... instead.

CREATE TABLE symbol_universe (
    list_id         TEXT NOT NULL CHECK (length(list_id) BETWEEN 1 AND 12 AND list_id = upper(list_id)),
    symbol          TEXT NOT NULL CHECK (length(symbol) BETWEEN 1 AND 9 AND symbol = upper(symbol)),
    exchange        TEXT NOT NULL CHECK (exchange IN ('NASDAQ', 'NYSE', 'NYSE_ARCA', 'CBOE', 'TSX')),
    name            TEXT NOT NULL CHECK (length(trim(name)) > 0),
    raw_ticker      TEXT NOT NULL,
    currency        TEXT NOT NULL CHECK (currency IN ('USD', 'CAD')),
    asset_type      TEXT NOT NULL CHECK (asset_type IN ('equity', 'etf')),
    sector          TEXT,
    weight_pct      REAL,
    holdings_as_of  TEXT CHECK (holdings_as_of IS NULL
                                OR holdings_as_of GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]'),
    fund_name       TEXT,
    is_demo         INTEGER NOT NULL DEFAULT 0 CHECK (is_demo IN (0, 1)),
    source_file     TEXT NOT NULL,
    source_id       TEXT NOT NULL REFERENCES source(source_id),
    batch_id        TEXT NOT NULL REFERENCES import_batch(batch_id),
    loaded_at_utc   TEXT NOT NULL,
    PRIMARY KEY (list_id, symbol, exchange)
);

CREATE INDEX ix_symbol_universe_symbol ON symbol_universe (symbol);