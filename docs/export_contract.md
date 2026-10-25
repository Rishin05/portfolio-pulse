# Portfolio Pulse — Export Contract (Power BI)

**Contract version 1.6** · Enforced by `tests/test_export_contract.py` (columns) and `tests/test_export_contract_16.py` (1.6 guarantees)

`python -m portfolio_pulse export` (also the last step of `run-daily`) writes one CSV per
table to `data/powerbi/`. Power BI Desktop reads that folder because it has no built-in
SQLite connector. Only a .pbix built from DEMO data is ever shared.

**Demo export:** `export --out data/powerbi_demo` against a separate demo database
(`PULSE_DB_PATH=<project>\data\demo\pulse_demo.db`, filled only by `demo`). A demo export must pass
`python -m portfolio_pulse.demo_gate data/powerbi_demo` (exit 0) before any .pbix built from it is shared.

## File format
- UTF-8, comma-separated, one header row, no index column.
- Dates `YYYY-MM-DD`; timestamps UTC ISO-8601 ending in `Z` (or `+00:00` in `export_info`).
  `fact_activity.trade_date` can carry a time; use its date part.
- Decimal point `.`. Files up to 1.5: money rounded to 2 dp, returns to 8 dp (a fraction: 0.0459 = 4.59%).
  Files added in 1.6: numbers rounded to 10 dp so summed totals reconcile to the cent.
- Flags: `is_demo`, `is_external`, `active` are `0`/`1`. `is_weekday` is `True`/`False`.
- Empty cell = NULL.

## Change rules
- **Additive** (new column at the end, or a new file): allowed. Bump the minor version, update this doc and the contract test.
- **Breaking** (rename, remove, reorder, change meaning or units): bump the major version, and update the Power BI report in the same commit.
- Never exported: credentials, `.env` values, `connection.provider_ref`, real account numbers, activity descriptions.

## Live files (21)

| File | Grain | Columns (in order) |
|---|---|---|
| `fact_daily_performance.csv` | account × date (engine v1, native currency; kept for compatibility) | account_id, as_of_date, prev_date, start_value, end_value, currency, net_external_flow, internal_flow, flow_count, total_change, investment_change, investment_return, days_since_prev, quality_flag, source_id, source_is_demo |
| `fact_perf_account_daily.csv` | account × date (engine v2) | account_id, household_id, as_of_date, prev_date, currency, start_value, end_value, net_flow, investment_change, return_native, start_value_cad, end_value_cad, net_flow_cad, investment_change_cad, return_cad, market_change_cad, fx_change_cad, currency_effect, fx_status, computed_at_utc, quality_label, is_demo |
| `fact_perf_household_daily.csv` | household × date (engine v2, CAD) | household_id, as_of_date, accounts, accounts_with_return, end_value_cad, external_flow_cad, internal_transfer_cad, investment_change_cad, market_change_cad, fx_change_cad, return_cad, fx_status, computed_at_utc, quality_label, is_demo |
| `fact_cash_flow.csv` | one cash flow | cash_flow_id, account_id, flow_date, flow_type, amount, currency, is_external, description, source_id |
| `fact_activity.csv` | one activity (13 normalized types) | activity_id, account_id, activity_type, raw_type, trade_date, symbol, quantity, amount, currency, fx_rate, fx_rate_date, fx_gap_days, fx_status, amount_cad, counter_amount, counter_currency, counter_amount_cad, counter_fx_status, reported_fx_rate, fx_conversion_cost_cad, quality_flag, source_id |
| `fact_holding_snapshot.csv` | account × date × symbol | account_id, as_of_date, symbol, asset_class, quantity, market_value, currency, fx_rate, fx_rate_date, fx_gap_days, fx_status, market_value_cad, quality_flag, source_id |
| `fact_news.csv` | article | article_id, symbol, headline, url, publisher, published_at_utc, published_date_utc, fetched_at_utc, source_id, is_demo, published_local_date, market_date, story_key, duplicate_of |
| `bridge_news_symbol.csv` | article × symbol | article_id, symbol, source_id, first_seen_utc |
| `fact_fx_cost.csv` | institution × date × direction × amount | as_of_date, institution_id, model, channel, from_currency, to_currency, amount, boc_rate, boc_rate_date, boc_status, platform_rate, received_at_benchmark, received_via_platform, notional_cad, cost_cad, cost_bps, quality_label, source_ref, source_url, date_checked, is_demo |
| `dim_account.csv` | account | account_id, display_name, account_type, base_currency, is_demo, institution_id, connection_id |
| `dim_connection.csv` | connection (read-only) | connection_id, institution_id, source_id, display_name, access_mode, status, created_at_utc, last_synced_at_utc, is_demo |
| `dim_institution.csv` | institution | institution_id, display_name, country, website_url, is_demo |
| `dim_watchlist.csv` | symbol | symbol, company_name, exchange, currency, active, origin, is_demo, last_seen_in_holdings |
| `dim_source.csv` | source | source_id, display_name, kind, is_demo, usage_note |
| `dim_date.csv` | calendar day, earliest to latest date used by any fact (`export.DATE_COLUMNS`) | date, year, month_num, month, year_month, week_of_year, weekday_num, weekday, is_weekday |
| `dim_fee_schedule.csv` | fee row | fee_schedule_id, institution_id, fee_type, applies_to, rate_bps, flat_amount, flat_currency, effective_from, effective_to, source_url, date_checked, notes, is_demo |
| `dim_fx_posted_rate.csv` | institution × date × channel | fx_posted_rate_id, institution_id, base_currency, quote_currency, rate_date, channel, customer_buy_rate, customer_sell_rate, source_url, date_checked, notes, is_demo |
| `dim_symbol_universe.csv` | listing (symbol × exchange) | listing_key, symbol, exchange, currency, asset_type, name, sector, lists, holdings_as_of, is_demo |
| `bridge_universe_list.csv` | list × listing | list_id, listing_key, weight_pct, holdings_as_of, fund_name, is_demo |
| `data_quality.csv` | area × item | area, item, status, last_date, age_days, detail |
| `export_info.csv` | one row | exported_at_utc, note, contract_version |

`fact_fx_cost`: the Bank of Canada rate is an indicative reference rate (CAD per 1 USD), not a tradable
rate. `cost_cad` = CAD value at that rate of what you'd get minus the CAD value of what you get;
`cost_bps` = cost_cad / notional_cad × 10,000 (a 1.5% fee = 150 bps). Negative = better than the
reference. Percent fees appear only from `effective_from` (or `date_checked`) onward; posted quotes
only on their own `rate_date`. `quality_label` is reported / estimated / STALE / DEMO. Rows that
can't be computed are omitted, never zero-filled. Never sum `cost_bps`; average it weighted by `notional_cad`.

`fact_perf_*`: `_cad` columns are converted at the Bank of Canada rate for that date (`fx_status` says how
fresh the rate was). `market_change_cad` + `fx_change_cad` = `investment_change_cad`. Household rows net
internal transfers between the household's own accounts (`internal_transfer_cad`), so household
`return_cad` is the figure to compound for a household TWR.

`fact_perf_household_daily.is_demo` is the worst case of its accounts (0 if any account is real).

`data_quality.status` is one of `OK`, `DEMO`, `STALE`, `MISSING`. Portfolio data is STALE after
4 days without a snapshot, and news after 7 days.

## Power BI relationships (one-to-many, single direction)
- `dim_account[account_id]` → `fact_perf_account_daily`, `fact_daily_performance`, `fact_cash_flow`, `fact_activity`, `fact_holding_snapshot`
- `dim_date[date]` → `fact_perf_account_daily[as_of_date]`, `fact_perf_household_daily[as_of_date]`, `fact_daily_performance[as_of_date]`, `fact_cash_flow[flow_date]`, `fact_activity[trade_date]` (date part), `fact_holding_snapshot[as_of_date]`, `fact_news[market_date]`, `fact_fx_cost[as_of_date]`
- `dim_watchlist[symbol]` → `bridge_news_symbol[symbol]` → `fact_news[article_id]` (an article can belong to several symbols; `fact_news[symbol]` is only the first one)
- `dim_symbol_universe[listing_key]` → `bridge_universe_list[listing_key]` (one listing, many lists)
- `dim_watchlist[symbol]` → `dim_symbol_universe[symbol]` (a symbol can trade on two exchanges, e.g. SHOP on TSX and NASDAQ)
- `dim_institution[institution_id]` → `dim_account`, `dim_connection`, `dim_fee_schedule`, `fact_fx_cost`
- `dim_source[source_id]` → all facts

## Rules for measures
- "Published around this date" for trading day D: articles whose `market_date` is D or the weekday before D, with `duplicate_of` blank. `market_date` uses a 16:00 Toronto close and the weekday calendar (holidays not removed). The wording is a time window only: never say or imply that a headline caused a price move.
- News status in `data_quality` uses real articles first: OK (real news within 7 days), STALE (older), DEMO (only synthetic), MISSING (none); `detail` gives the counts and the routing.
- Each row's native money is in its own `currency` column. **Never sum native amounts across currencies;** sum the `_cad` columns instead.
- Period return: compound daily returns (TWR = Π(1 + r) − 1). Never average percentages;
  divide summed dollars where a dollar-weighted figure is needed
  (e.g. daily return for a selection of accounts = Σ investment_change_cad / Σ (start_value_cad + net_flow_cad)).
- Label `source_is_demo = 1` / `is_demo = 1` rows as DEMO on every visual.

## Planned files (not exported yet)

| File | Notes |
|---|---|
| `fact_fx_rate.csv` | BoC Valet rates (demo rates in demo exports). |

## Changelog
- **1.6** (2026-10-24): added `fact_perf_account_daily`, `fact_perf_household_daily`, `fact_activity`, `fact_holding_snapshot`, `dim_connection` (never `provider_ref`) and `dim_institution`; `export_info` gained `contract_version`; `dim_date` now spans every fact date (rows only, columns unchanged); 1.6 files rounded to 10 dp; `export --out` and the demo privacy gate (additive, schema v10, Phase 6). `fact_daily_performance` (v1) is kept; the report uses the v2 CAD tables.
- **1.5** (2026-10-22): `fact_news` gained `published_local_date`, `market_date`, `story_key`, `duplicate_of` (appended); added `bridge_news_symbol`; news `data_quality` now ranks real articles before demo and explains its reason (additive, schema v10, Phase 5).
- **1.4** (2026-10-22): added `dim_symbol_universe` and `bridge_universe_list` (additive, schema v9, Phase 5). `weight_pct` is a weight inside its own fund; never compare it across lists.
- **1.3** (2026-10-22): `dim_watchlist` gained `origin` (demo / holding / manual), `is_demo` and `last_seen_in_holdings`, appended at the end (additive, schema v7, Phase 5).
- **1.2** (2026-10-20): added `dim_fee_schedule`, `dim_fx_posted_rate`, `fact_fx_cost` (additive, schema v6, Phase 4).
- **1.1** (2026-10-15): `dim_account` gained `institution_id` and `connection_id` (additive, schema v2).
- **1.0**: starter MVP, 9 files.