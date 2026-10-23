"""Export tidy CSVs for Power BI (the 'data folder bridge').

Power BI Desktop has no built-in SQLite connector, so the MVP writes one CSV per
table to data/powerbi/ and Power BI reads that folder. Switch to PostgreSQL later
if you want a direct database connection.
"""
from __future__ import annotations

import logging
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pandas as pd

from . import fx_converter, news

log = logging.getLogger(__name__)

STALE_NEWS_DAYS = 7
STALE_SNAPSHOT_DAYS = 4   # more than a long weekend


def _q(conn, sql: str) -> pd.DataFrame:
    return pd.read_sql_query(sql, conn)


def build_quality(conn, today: date | None = None) -> pd.DataFrame:
    today = today or date.today()
    rows = []
    for r in conn.execute("""
        SELECT a.account_id, a.is_demo, MAX(s.as_of_date) AS last_date, COUNT(s.as_of_date) AS n,
               SUM(CASE WHEN p.days_since_prev > 3 THEN 1 ELSE 0 END) AS gaps
        FROM account a LEFT JOIN daily_snapshot s ON s.account_id = a.account_id
        LEFT JOIN v_daily_performance p ON p.account_id = s.account_id AND p.as_of_date = s.as_of_date
        GROUP BY a.account_id"""):
        last = date.fromisoformat(r["last_date"]) if r["last_date"] else None
        age = (today - last).days if last else None
        status = "DEMO" if r["is_demo"] else ("MISSING" if last is None else
                                              "STALE" if age > STALE_SNAPSHOT_DAYS else "OK")
        rows.append({"area": "portfolio", "item": r["account_id"], "status": status,
                     "last_date": r["last_date"], "age_days": age,
                     "detail": f"{r['n']} snapshots, {r['gaps'] or 0} multi-day gaps"})
    for r in conn.execute("""
        SELECT w.symbol, MAX(n.published_at_utc) AS last_pub, COUNT(n.article_id) AS n,
               SUM(CASE WHEN n.published_at_utc IS NULL THEN 1 ELSE 0 END) AS no_ts,
               MAX(src.is_demo) AS demo
        FROM watchlist w LEFT JOIN news_article n ON n.symbol = w.symbol
        LEFT JOIN source src ON src.source_id = n.source_id
        WHERE w.active = 1 GROUP BY w.symbol"""):
        last = datetime.fromisoformat(r["last_pub"].replace("Z", "+00:00")).date() if r["last_pub"] else None
        age = (today - last).days if last else None
        status = ("MISSING" if r["n"] == 0 else "DEMO" if r["demo"] else
                  "STALE" if age is None or age > STALE_NEWS_DAYS else "OK")
        rows.append({"area": "news", "item": r["symbol"], "status": status, "last_date": str(last) if last else None,
                     "age_days": age, "detail": f"{r['n']} articles, {r['no_ts'] or 0} without timestamp"})
    return pd.DataFrame(rows)


def build_dim_date(start: str, end: str) -> pd.DataFrame:
    d = pd.date_range(start, end, freq="D")
    return pd.DataFrame({"date": d.date, "year": d.year, "month_num": d.month, "month": d.strftime("%b"),
                         "year_month": d.strftime("%Y-%m"), "week_of_year": d.isocalendar().week.values,
                         "weekday_num": d.weekday + 1, "weekday": d.strftime("%a"),
                         "is_weekday": d.weekday < 5})


def export_all(conn, out_dir: str | Path) -> dict[str, int]:
    news.refresh_derived(conn)   # idempotent: Toronto dates, story keys, article-symbol links
    out = Path(out_dir)
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    perf = _q(conn, "SELECT p.*, s.is_demo AS source_is_demo FROM v_daily_performance p "
                    "JOIN source s ON s.source_id = p.source_id ORDER BY account_id, as_of_date")
    perf = perf.round({"start_value": 2, "end_value": 2, "net_external_flow": 2, "internal_flow": 2,
                       "total_change": 2, "investment_change": 2, "investment_return": 8})
    perf["days_since_prev"] = perf["days_since_prev"].astype("Int64")
    tables = {
        "fact_daily_performance": perf,
        "fact_cash_flow": _q(conn, "SELECT cash_flow_id, account_id, flow_date, flow_type, amount, currency, "
                                   "is_external, description, source_id FROM cash_flow ORDER BY flow_date"),
        "fact_news": _q(conn, "SELECT n.article_id, n.symbol, n.headline, n.url, n.publisher, n.published_at_utc, "
                              "substr(n.published_at_utc, 1, 10) AS published_date_utc, n.fetched_at_utc, "
                              "n.source_id, s.is_demo FROM news_article n JOIN source s USING (source_id) "
                              "ORDER BY n.published_at_utc DESC"),
        "dim_account": _q(conn, "SELECT * FROM account"),
        "dim_watchlist": _q(conn, "SELECT symbol, company_name, exchange, currency, active, origin, is_demo, "
                                  "last_seen_in_holdings FROM watchlist ORDER BY symbol"),
        "dim_source": _q(conn, "SELECT * FROM source"),
        "data_quality": build_quality(conn),
        "dim_fee_schedule": _q(conn, "SELECT fee_schedule_id, institution_id, fee_type, applies_to, rate_bps, "
                                     "flat_amount, flat_currency, effective_from, effective_to, source_url, "
                                     "date_checked, notes, is_demo FROM fee_schedule "
                                     "ORDER BY institution_id, fee_type, fee_schedule_id"),
        "dim_fx_posted_rate": _q(conn, "SELECT fx_posted_rate_id, institution_id, base_currency, quote_currency, "
                                       "rate_date, channel, customer_buy_rate, customer_sell_rate, source_url, "
                                       "date_checked, notes, is_demo FROM fx_posted_rate "
                                       "ORDER BY institution_id, rate_date, channel"),
        "dim_symbol_universe": _q(conn, "SELECT symbol || '|' || exchange AS listing_key, symbol, exchange, "
                                        "MAX(currency) AS currency, MAX(asset_type) AS asset_type, MAX(name) AS name, "
                                        "MAX(sector) AS sector, GROUP_CONCAT(list_id) AS lists, "
                                        "MAX(holdings_as_of) AS holdings_as_of, MAX(is_demo) AS is_demo "
                                        "FROM symbol_universe GROUP BY symbol, exchange ORDER BY symbol, exchange"),
        "bridge_universe_list": _q(conn, "SELECT list_id, symbol || '|' || exchange AS listing_key, weight_pct, "
                                         "holdings_as_of, fund_name, is_demo FROM symbol_universe "
                                         "ORDER BY list_id, symbol, exchange"),
    }
    dsu = tables["dim_symbol_universe"]
    if not dsu.empty:
        dsu["lists"] = dsu["lists"].map(lambda s: ",".join(sorted(set(s.split(",")))) if s else s)
    fx_rows, fx_skipped = fx_converter.cost_grid(conn)
    tables["fact_fx_cost"] = pd.DataFrame(fx_rows, columns=fx_converter.COST_COLUMNS)
    if fx_skipped:
        log.info("fact_fx_cost: %s combination(s) skipped (no usable rate, fee or quote side).", fx_skipped)
    if not perf.empty:
        tables["dim_date"] = build_dim_date(perf["as_of_date"].min(), perf["as_of_date"].max())
    tables["export_info"] = pd.DataFrame([{"exported_at_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                                           "note": "Educational analytics only. Not financial advice."}])
    counts = {}
    for name, df in tables.items():
        df.to_csv(out / f"{name}.csv", index=False)
        counts[name] = len(df)
    log.info("Exported %s tables to %s", len(counts), out)
    return counts
