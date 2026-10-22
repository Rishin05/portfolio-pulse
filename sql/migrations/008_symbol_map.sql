-- 008_symbol_map: how each watchlist symbol is looked up by each news source.
--   finnhub_symbol = symbol to send to Finnhub company-news, or NULL = don't use Finnhub for it.
--                    A row with NULL is an explicit, checked decision (e.g. TSX-only ETF: the free
--                    plan returned HTTP 403 for .TO symbols on 2026-10-22).
--   No row at all  = default rule in symbol_map.py (USD -> same symbol; anything else -> skip).
--   rss_query      = Google News RSS phrase; NULL falls back to watchlist.news_query, then company_name.
-- Do not edit once applied; add 009_... instead.

CREATE TABLE symbol_map (
    symbol          TEXT PRIMARY KEY REFERENCES watchlist(symbol),
    finnhub_symbol  TEXT CHECK (finnhub_symbol IS NULL
                                OR (length(trim(finnhub_symbol)) > 0 AND finnhub_symbol = upper(finnhub_symbol))),
    rss_query       TEXT CHECK (rss_query IS NULL OR length(trim(rss_query)) > 0),
    note            TEXT,
    checked_on      TEXT NOT NULL CHECK (checked_on GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]'),
    updated_at_utc  TEXT NOT NULL
);