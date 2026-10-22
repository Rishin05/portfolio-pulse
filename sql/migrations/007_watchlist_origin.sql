-- 007_watchlist_origin: where each watchlist symbol came from, and whether it is demo data.
--   origin = 'demo'    seeded by the synthetic generator; sync never changes it
--            'holding' added by `watchlist sync` from the latest holdings; set inactive (never
--                      deleted) when no longer held, so its news history and foreign keys survive
--            'manual'  added, edited or removed by you; sync never changes `active`
-- Defaults are 'demo' / 1 on purpose: an unlabelled row is treated as demo, never claimed real.
-- The three existing rows (RY, SHOP, XEQT) are demo seeds, so the defaults label them correctly
-- and synthetic.py needs no change (existing demo figures stay identical).
-- Do not edit once applied; add 008_... instead.

ALTER TABLE watchlist ADD COLUMN origin TEXT NOT NULL DEFAULT 'demo'
    CHECK (origin IN ('demo', 'holding', 'manual'));

ALTER TABLE watchlist ADD COLUMN is_demo INTEGER NOT NULL DEFAULT 1
    CHECK (is_demo IN (0, 1));

ALTER TABLE watchlist ADD COLUMN last_seen_in_holdings TEXT
    CHECK (last_seen_in_holdings IS NULL
           OR last_seen_in_holdings GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]');