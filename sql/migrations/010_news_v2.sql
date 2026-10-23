-- 010_news_v2: Toronto dates, a story key for cross-source duplicates, and article <-> symbol links.
--   published_local_date = Toronto calendar date of published_at_utc (DST-aware; filled in Python).
--   market_date          = first weekday session the article could relate to (16:00 Toronto close;
--                          weekends -> Monday; weekday calendar, holidays not removed).
--   story_key            = hash of the normalized headline (publisher suffix removed).
--   duplicate_of         = the first article with the same story_key for the same symbol within
--                          +/-1 day. Duplicates are KEPT and flagged, never deleted.
-- news_article_symbol: one article can belong to several symbols (news_article.symbol stays as the
-- symbol it was first stored for). Existing rows are linked here; dates/keys are backfilled by
-- news.refresh_derived(), which runs before each fetch and export because SQLite has no time zones.
-- Do not edit once applied; add 011_... instead.

ALTER TABLE news_article ADD COLUMN published_local_date TEXT
    CHECK (published_local_date IS NULL
           OR published_local_date GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]');
ALTER TABLE news_article ADD COLUMN market_date TEXT
    CHECK (market_date IS NULL OR market_date GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]');
ALTER TABLE news_article ADD COLUMN story_key TEXT;
ALTER TABLE news_article ADD COLUMN duplicate_of TEXT REFERENCES news_article(article_id);

CREATE TABLE news_article_symbol (
    article_id      TEXT NOT NULL REFERENCES news_article(article_id),
    symbol          TEXT NOT NULL REFERENCES watchlist(symbol),
    source_id       TEXT NOT NULL REFERENCES source(source_id),
    first_seen_utc  TEXT NOT NULL,
    PRIMARY KEY (article_id, symbol)
);

INSERT INTO news_article_symbol (article_id, symbol, source_id, first_seen_utc)
SELECT article_id, symbol, source_id, fetched_at_utc FROM news_article;

CREATE INDEX ix_news_article_story ON news_article (story_key);
CREATE INDEX ix_news_article_symbol_symbol ON news_article_symbol (symbol);