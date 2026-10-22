# Portfolio Pulse — Export Contract (Power BI)

**Contract version 1.1** · Enforced by `tests/test_export_contract.py`

`python -m portfolio_pulse export` (also the last step of `run-daily`) writes one CSV per
table to `data/powerbi/`. Power BI Desktop reads that folder because it has no built-in
SQLite connector. Only a .pbix built from DEMO data is ever shared.

## File format
- UTF-8, comma-separated, one header row, no index column.
- Dates `YYYY-MM-DD`; timestamps UTC ISO-8601 ending in `Z` (or `+00:00` in `export_info`).
- Decimal point `.`. Money rounded to 2 dp, returns to 8 dp (a fraction: 0.0459 = 4.59%).
- Flags: `is_demo`, `is_external`, `active` are `0`/`1`. `is_weekday` is `True`/`False`.
- Empty cell = NULL.

## Change rules
- **Additive** (new column at the end, or a new file): allowed. Bump the minor version, update this doc and the contract test.
- **Breaking** (rename, remove, reorder, change meaning or units): bump the major version, and update the Power BI report in the same commit.
- Never exported: credentials, `.env` values, `connection.provider_ref`, real account numbers.

## Live files (12)

| File | Grain | Columns (in order) |
|---|---|---|
| `fact_daily_performance.csv` | account × date | account_id, as_of_date, prev_date, start_value, end_value, currency, net_external_flow, internal_flow, flow_count, total_change, investment_change, investment_return, days_since_prev, quality_flag, source_id, source_is_demo |
| `fact_cash_flow.csv` | one cash flow | cash_flow_id, account_id, flow_date, flow_type, amount, currency, is_external, description, source_id |
| `fact_news.csv` | one headline | article_id, symbol, headline, url, publisher, published_at_utc, published_date_utc, fetched_at_utc, source_id, is_demo |
| `dim_account.csv` | account | account_id, display_name, account_type, base_currency, is_demo, institution_id, connection_id |
| `dim_watchlist.csv` | symbol | symbol, company_name, exchange, currency, active, origin, is_demo, last_seen_in_holdings |
| `dim_source.csv` | source | source_id, display_name, kind, is_demo, usage_note |
| `dim_date.csv` | calendar day (min to max performance date) | date, year, month_num, month, year_month, week_of_year, weekday_num, weekday, is_weekday |
| `data_quality.csv` | area × item | area, item, status, last_date, age_days, detail |
| `export_info.csv` | one row | exported_at_utc, note |
| `dim_fee_schedule.csv` | fee row | fee_schedule_id, institution_id, fee_type, applies_to, rate_bps, flat_amount, flat_currency, effective_from, effective_to, source_url, date_checked, notes, is_demo |
| `dim_fx_posted_rate.csv` | institution × date × channel | fx_posted_rate_id, institution_id, base_currency, quote_currency, rate_date, channel, customer_buy_rate, customer_sell_rate, source_url, date_checked, notes, is_demo |
| `fact_fx_cost.csv` | institution × date × direction × amount | as_of_date, institution_id, model, channel, from_currency, to_currency, amount, boc_rate, boc_rate_date, boc_status, platform_rate, received_at_benchmark, received_via_platform, notional_cad, cost_cad, cost_bps, quality_label, source_ref, source_url, date_checked, is_demo |

`fact_fx_cost`: the Bank of Canada rate is an indicative reference rate (CAD per 1 USD), not a tradable
rate. `cost_cad` = CAD value at that rate of what you'd get minus the CAD value of what you get;
`cost_bps` = cost_cad / notional_cad × 10,000 (a 1.5% fee = 150 bps). Negative = better than the
reference. Percent fees appear only from `effective_from` (or `date_checked`) onward; posted quotes
only on their own `rate_date`. `quality_label` is reported / estimated / STALE / DEMO. Rows that
can't be computed are omitted, never zero-filled. Never sum `cost_bps`; average it weighted by `notional_cad`.

`data_quality.status` is one of `OK`, `DEMO`, `STALE`, `MISSING`. Portfolio data is STALE after
4 days without a snapshot, and news after 7 days.

## Power BI relationships (one-to-many, single direction)
- `dim_account[account_id]` → `fact_daily_performance`, `fact_cash_flow`
- `dim_date[date]` → `fact_daily_performance[as_of_date]`, `fact_cash_flow[flow_date]`
- `dim_watchlist[symbol]` → `fact_news[symbol]`
- `dim_source[source_id]` → all facts

## Rules for measures
- Each row's money is in its own `currency` column. **Never sum across currencies.**
  Today every exported performance row is CAD. Phase 3 adds CAD-converted columns.
- Period return: compound daily returns (TWR = Π(1 + r) − 1). Never average percentages;
  divide summed dollars where a dollar-weighted figure is needed.
- Label `source_is_demo = 1` / `is_demo = 1` rows as DEMO on every visual.

## Planned files (not exported yet)
Tables exist since schema v2. Columns will follow `docs/data_dictionary.md`, minus `batch_id` and `loaded_at_utc`.

| File | Phase | Notes |
|---|---|---|
| `fact_activity.csv` | 3 | Native currency plus CAD-converted amount. |
| `fact_holding_snapshot.csv` | 3 | Native currency plus CAD market value. |
| `fact_fx_rate.csv` | 3 | BoC Valet rates (demo rates in demo exports). |
| `dim_institution.csv` | 3 | |
| `dim_connection.csv` | 3 | Excludes `provider_ref`. |
| `dim_fee_schedule.csv` | 4 | Includes `source_url` and `date_checked`. |
| `fact_fx_cost.csv` | 4 | Spread in bps and CAD per conversion. |

## Changelog
- **1.3** (2026-10-22): `dim_watchlist` gained `origin` (demo / holding / manual), `is_demo` and `last_seen_in_holdings`, appended at the end (additive, schema v7, Phase 5).
- **1.2** (2026-10-20): added `dim_fee_schedule`, `dim_fx_posted_rate`, `fact_fx_cost` (additive, schema v6, Phase 4).
- **1.2** (2026-10-20): added `dim_fee_schedule`, `dim_fx_posted_rate`, `fact_fx_cost` (additive, schema v6, Phase 4).
- **1.1** (2026-10-15): `dim_account` gained `institution_id` and `connection_id` (additive, schema v2).
- **1.0**: starter MVP, 9 files.