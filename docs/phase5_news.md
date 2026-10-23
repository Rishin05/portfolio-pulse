# Phase 5: News integration v2

Educational analytics only. Headlines and links are shown as "Published around this date";
the project never says or implies that a headline caused a price move.

## Pipeline (run-daily order)
SnapTrade -> BoC FX -> watchlist file (config/watchlist.xlsx, else .csv) -> watchlist sync from holdings
-> news (per symbol) -> inbox CSV -> performance -> export. Every step is non-blocking.

## Watchlist (migration 007)
- `origin`: demo (synthetic seed) / holding (added by `watchlist sync` from each account's latest
  holdings) / manual (your file or `watchlist add|remove`). Sync only switches `holding` rows on or off;
  manual and demo rows are never changed by sync. Rows are never deleted (news keeps its history).
- `is_demo` = 1 unless a non-demo account holds the symbol (worst label wins; default 1).
- Editable file: `config/watchlist.xlsx` (Excel picker, `watchlist picker`) or `config/watchlist.csv`,
  both git-ignored; template `examples/watchlist.example.csv`. Columns: symbol, active, name, exchange,
  currency, finnhub ('-' = never), rss, note. Plan-then-apply: the same file again changes nothing;
  bad rows -> data/rejects/. Symbols not in the file are left alone. Soft limit: 50 active symbols.

## Symbol universe (migration 009) - a catalogue, not the watchlist
- `universe-import FILE --list ID` loads iShares holdings files you download yourself (Excel 2003 XML
  saved as .xls) or a manual CSV. Lists: IVV (S&P 500), IWM (Russell 2000), XIU (S&P/TSX 60),
  XEQT (look-through of its funds), MANUAL. Files live in data/reference/ (git-ignored, personal use).
- Quirks handled: 1-2 leading BOMs, bare '&', title rows before the 'Ticker' header, no 'Asset Class'
  column in Canadian files, Canadian 'Currency' = the fund's reporting currency (currency is derived
  from the exchange), derivatives/unlisted rows, class shares ('MOG A' -> 'MOG.A'), look-through duplicates.
- Each list mirrors its latest file; older files and files whose title doesn't match a known list id are
  refused. `watchlist search TEXT` ranks exact ticker, ticker prefix, name word, name contains; then list
  tier (IVV/XIU/IWB, XEQT, MANUAL, IWM); then in-fund weight (never compared across funds).

## Routing (migration 008 symbol_map)
- Per symbol: Finnhub if a key is set and symbol_map gives a Finnhub symbol -> Google News RSS if you opted
  in (`--source rss`, or NEWS_RSS_FALLBACK=1 for auto/run-daily) -> nothing (MISSING). A source that fails
  or returns 0 hands over to the next.
- Default when unmapped: USD -> same symbol; anything else -> skip. A bare ticker is never assumed to be a
  US listing (T = Telus on the TSX, AT&T on the NYSE).
- Errors: 401 -> Finnhub off for the run; 403 -> "not on your plan" (TSX .TO on the free plan); 429 (or
  503 from Google News) -> that source off for the run, Retry-After reported, no automatic retry.
  Pacing: 1.1 s between Finnhub calls, 1 s between RSS calls.
- RSS: a plain phrase is quoted; Google search syntax (OR, quotes, -word, site:) is passed through.
  NEWS_RSS_WINDOW_DAYS (1-30, default 7), NEWS_RSS_MAX_ITEMS (1-100, default 25, newest first).

## Storage, time and duplicates (migration 010)
- Stored: headline, link, publisher, UTC timestamp. Never article bodies.
- `published_local_date` = Toronto date (zoneinfo + tzdata; DST-safe).
- `market_date` = first weekday session: weekday before 16:00 Toronto -> that day; at/after 16:00 or a
  weekend -> next weekday. Weekday calendar: holidays are not removed.
- "Published around this date" for trading day D = market_date is D or the weekday before, duplicate_of blank.
- `news_article_symbol` links one article to several symbols. `story_key` = hash of the headline with the
  " - Publisher" suffix removed (Google News links are redirects, so URLs never match Finnhub's).
  Same story, same symbol, within +/-1 day, different link -> `duplicate_of` the first. Kept, never deleted.
- `refresh_derived()` (before each fetch and export) fills dates/keys and links for older and demo rows.

## Quality (data_quality, area = news)
Real articles first: OK (latest real article within 7 days), STALE (older or undated), DEMO (only
synthetic), MISSING (none). `detail` = real / demo / duplicate / undated counts + routing.

## Export contract
1.3 dim_watchlist origin/is_demo/last_seen_in_holdings; 1.4 dim_symbol_universe + bridge_universe_list;
1.5 fact_news local/market dates, story_key, duplicate_of + bridge_news_symbol.

## Verified facts (2026-10-22)
- Finnhub free plan, live probe: TSX `.TO` symbols return HTTP 403 (profile2 and company-news); search lists
  .TO and .NE; bare VFV/QQC return 200 with no data; SHOP (Nasdaq) works. Observed ~250 articles max per
  request (NVDA 250, META 247 for 7 days).
- Shopify's US listing moved from the NYSE to Nasdaq on 2025-03-31; ticker SHOP on both TSX and Nasdaq.
- Google News RSS: links are news.google.com redirects; headlines end " - <publisher>"; no account or feed limit.
- Alpha Vantage: the API key is only accepted as the `apikey` URL parameter (conflicts with our
  keys-in-headers rule); parked.