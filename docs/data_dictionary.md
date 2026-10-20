# Portfolio Pulse — Data Dictionary

Schema version **2** (`sql/migrations/001_baseline.sql`, `002_multi_broker.sql`).
Educational analytics only. Not investment advice. The app never places trades.

## Conventions

| Topic | Rule |
|---|---|
| Money | Every money column has a currency column beside it (ISO 4217, e.g. `CAD`, `USD`). Amounts are stored in that native currency and never pre-converted. |
| Sign | `+` = into the account, `−` = out of the account. |
| Dates | `YYYY-MM-DD`, the account's local market day. |
| Timestamps | UTC ISO-8601, e.g. `2026-10-15T14:30:00Z`. |
| IDs | `stable_id()` = first 32 hex chars of SHA-256 of the natural key, so re-loading never duplicates rows. |
| Loading | `insert_ignore()` skips duplicate keys only. Rows that break a CHECK or NOT NULL rule raise an error instead of being dropped. |
| Provenance | Every fact row carries `source_id`, `batch_id` and `loaded_at_utc`. |
| Quality | Stored `quality_flag`: `reported`, `estimated` or `demo`. `STALE` and `MISSING` are derived at export time, not stored. |
| Privacy | Account IDs are your own labels, never real account numbers. No usernames, passwords, tokens or API keys are stored in the database. |
| Schema changes | Add a new numbered file in `sql/migrations/`. Never edit an applied migration. |

## Sources (`source`)

| source_id | Kind | Demo | Use |
|---|---|---|---|
| `synthetic` | synthetic | yes | Generated demo data. The only data allowed in public demos. |
| `csv_import` | file | no | Your own downloaded export, loaded via a JSON column map. |
| `finnhub` | api | no | Company news headlines and links. |
| `google_news_rss` | rss | no | Headlines and links, personal non-commercial use, opt-in. |
| `snaptrade` | api | no | Read-only brokerage data (Phase 2). |
| `boc_valet` | api | no | Bank of Canada daily reference FX rates (Phase 3). A benchmark, not a broker's rate. |

## Tables

### `schema_version`
| Column | Type | Notes |
|---|---|---|
| version | INTEGER PK | Migration number. |
| name | TEXT | File stem, e.g. `002_multi_broker`. |
| applied_at_utc | TEXT | When it was applied. |

### `import_batch`
One row per ingestion run (demo, file import, API pull).

| Column | Type | Notes |
|---|---|---|
| batch_id | TEXT PK | Random UUID per run. |
| source_id | TEXT FK → source | |
| description | TEXT | File or job name. |
| content_sha256 | TEXT | Hash of the input file, if any. |
| started_at_utc | TEXT | |
| rows_read / rows_inserted / rows_skipped / rows_rejected | INTEGER | Skipped = duplicates already present. Rejected = failed validation. |
| dry_run | INTEGER 0/1 | |

### `institution`
| Column | Type | Notes |
|---|---|---|
| institution_id | TEXT PK | Your own slug, e.g. `wealthsimple`. |
| display_name | TEXT | |
| country | TEXT | ISO 3166 two-letter code. |
| website_url | TEXT | |
| is_demo | INTEGER 0/1 | |

### `connection`
One data link to an institution. Never holds credentials.

| Column | Type | Notes |
|---|---|---|
| connection_id | TEXT PK | |
| institution_id | TEXT FK → institution | |
| source_id | TEXT FK → source | `snaptrade`, `csv_import` or `synthetic`. |
| display_name | TEXT | |
| access_mode | TEXT | Always `read_only` (enforced by CHECK). |
| status | TEXT | `active`, `disabled` or `error`. |
| provider_ref | TEXT | Provider's opaque connection ID only. Never a secret. |
| created_at_utc / last_synced_at_utc | TEXT | |
| is_demo | INTEGER 0/1 | |

### `account`
| Column | Type | Notes |
|---|---|---|
| account_id | TEXT PK | Your own label, e.g. `DEMO-TFSA`. |
| display_name | TEXT | |
| account_type | TEXT | TFSA, RRSP, FHSA, non-registered, ... |
| base_currency | TEXT | Reporting currency of the account. |
| is_demo | INTEGER 0/1 | |
| institution_id | TEXT FK → institution | Nullable (added in v2). |
| connection_id | TEXT FK → connection | Nullable (added in v2). |

### `daily_snapshot`
End-of-day value per account. PK (account_id, as_of_date).

| Column | Type | Notes |
|---|---|---|
| account_id | TEXT FK → account | |
| as_of_date | TEXT | |
| end_value / currency | REAL / TEXT | `end_value >= 0`. |
| quality_flag, source_id, batch_id, loaded_at_utc | | Provenance. |

### `cash_flow`
MVP flow table used by `v_daily_performance`. Phase 3 will derive flows from `activity` instead.

| Column | Type | Notes |
|---|---|---|
| cash_flow_id | TEXT PK | stable_id of the natural key. |
| account_id | TEXT FK → account | |
| flow_date | TEXT | |
| flow_type | TEXT | DEPOSIT, WITHDRAWAL, TRANSFER_IN, TRANSFER_OUT, DIVIDEND, INTEREST, FEE, OTHER. |
| amount / currency | REAL / TEXT | Signed. |
| is_external | INTEGER 0/1 | 1 = contribution or withdrawal (not performance). |
| description, source_id, batch_id, loaded_at_utc | | |

### `activity`
Every account event in one ledger, in its native currency.

| Column | Type | Notes |
|---|---|---|
| activity_id | TEXT PK | stable_id(source, provider ID or natural key). |
| account_id | TEXT FK → account | |
| activity_type | TEXT | Normalized type (see the next section). |
| raw_type | TEXT | The broker's own label, kept verbatim. |
| trade_date / settlement_date | TEXT | |
| symbol, description | TEXT | |
| quantity | REAL | Units: `+` received, `−` delivered. |
| price / price_currency | REAL / TEXT | |
| amount / currency | REAL / TEXT | Net cash effect, fees included. |
| fee_amount / fee_currency | REAL / TEXT | Breakdown of the fee already inside `amount`. |
| counter_amount / counter_currency | REAL / TEXT | FX_CONVERSION only: the amount and currency arriving. |
| reported_fx_rate | REAL | Broker's rate if given: units leaving per 1 unit arriving = \|amount\| / \|counter_amount\|. |
| provider_activity_id | TEXT | |
| quality_flag, source_id, batch_id, loaded_at_utc | | Provenance. |

Rules enforced by CHECK: amount/currency pairs are both set or both NULL. FX_CONVERSION needs a counter leg in a different currency.

### `holding_snapshot`
What an account held on a date. Cash is a row too (`CASH.CAD`, `CASH.USD`). PK (account_id, as_of_date, symbol).

| Column | Type | Notes |
|---|---|---|
| asset_class | TEXT | equity, etf, fund, bond, cash, crypto, other. |
| quantity | REAL | |
| price / price_currency | REAL / TEXT | |
| market_value / market_value_currency | REAL / TEXT | |
| book_value / book_value_currency | REAL / TEXT | Average cost, commission included. |
| quality_flag, source_id, batch_id, loaded_at_utc | | Provenance. |

### `fx_rate`
Daily reference rates. PK (rate_date, base_currency, quote_currency, source_id).

| Column | Type | Notes |
|---|---|---|
| rate | REAL | Units of quote per 1 base. `USD→CAD 1.37` means 1 USD = 1.37 CAD (same direction as BoC `FXUSDCAD`). |
| series_id | TEXT | Provider series, e.g. `FXUSDCAD`. Demo rows use `DEMO-USDCAD`. |
| quality_flag, source_id, batch_id, loaded_at_utc | | Provenance. |

### `fee_schedule`
Published fees, with where and when they were checked.

| Column | Type | Notes |
|---|---|---|
| fee_schedule_id | TEXT PK | |
| institution_id | TEXT FK → institution | |
| fee_type | TEXT | fx_conversion, commission, account, other. |
| applies_to | TEXT | e.g. `CAD<->USD`, `US equities`. |
| rate_bps | REAL | 150 = 1.50%. |
| flat_amount / flat_currency | REAL / TEXT | At least one of rate_bps or flat_amount is required. |
| effective_from / effective_to | TEXT | `effective_to` NULL = still current. |
| source_url | TEXT | Required. Demo rows use example.com. |
| date_checked | TEXT | Required. The date you last confirmed the page. |
| notes, is_demo | | |

### `fx_posted_rate` (migration 006)
Dated FX quotes posted by an institution (e.g. a bank's USD/CAD rate). Quotes, not fees.

| Column | Type | Notes |
|---|---|---|
| fx_posted_rate_id | TEXT PK | stable_id('quote', institution, base, quote, rate_date, channel) |
| institution_id | TEXT FK → institution | |
| base_currency / quote_currency | TEXT | Upper-case ISO, different; e.g. USD / CAD. |
| rate_date | TEXT | YYYY-MM-DD the quote applies to. Never reused on another day. |
| channel | TEXT | NOT NULL, default `standard` (e.g. cash vs transfer). |
| customer_buy_rate | REAL | CAD you pay per 1 USD. |
| customer_sell_rate | REAL | CAD you receive per 1 USD. At least one rate; buy ≥ sell. |
| source_url / date_checked | TEXT | Required; date_checked ≥ rate_date. Demo rows use example.com. |
| notes, is_demo | | |

UNIQUE (institution_id, base_currency, quote_currency, rate_date, channel).

### `watchlist` and `news_article`
Headlines only: headline, publisher, UTC timestamp and a link to the original article. Never the article body. The UI says "Published around this date" and never claims a headline caused a move.

## Activity types and the performance rule

| activity_type | Typical sign | External flow (F)? | Treatment |
|---|---|---|---|
| DEPOSIT | + | yes | Contribution |
| WITHDRAWAL | − | yes | Withdrawal |
| TRANSFER_IN | + | yes | Contribution |
| TRANSFER_OUT | − | yes | Withdrawal |
| DIVIDEND | + | no | Part of return |
| INTEREST | + | no | Part of return |
| FEE | − | no | Part of return |
| TAX | − | no | Part of return (withholding tax; assumption) |
| BUY / SELL | − / + | no | Ignored (moves value within the account) |
| FX_CONVERSION | − then + | no | Ignored for return. Spread versus the benchmark is reported by the FX Fee Converter. |
| SPLIT | 0 | no | Quantity change only |
| OTHER | ± | review | Flagged for review |

- Investment change = V(t) − V(t−1) − F(t)
- Daily return = [V(t) − V(t−1) − F(t)] / [V(t−1) + F(t)]
- Flows are assumed at the start of the day, and weekend flows roll into the next snapshot.
- TWR = Π(1 + r) − 1

## FX spread (used in Phase 4)

For a conversion with benchmark mid rate *m* (CAD per USD):

- Buying USD: effective = |CAD paid| / USD received, and spread_bps = (effective / m − 1) × 10,000.
- Selling USD: effective = CAD received / |USD paid|, and spread_bps = (1 − effective / m) × 10,000.
- Cost in CAD = spread_bps / 10,000 × CAD value of the conversion.

Demo data uses a 50 bps spread at Northern Demo Invest, and `tests/test_multi_broker.py` checks that it is recovered.