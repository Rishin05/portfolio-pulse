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

from . import fx_converter, news, symbol_map

log = logging.getLogger(__name__)

EXPORT_CONTRACT_VERSION = "1.6"
STALE_NEWS_DAYS = 7
STALE_SNAPSHOT_DAYS = 4   # more than a long weekend


def _q(conn, sql: str) -> pd.DataFrame:
    return pd.read_sql_query(sql, conn)

NEWS_QUALITY_SQL = """
WITH sym_articles AS (                 -- links (migration 010) plus the first symbol, in case not refreshed
    SELECT article_id, symbol FROM news_article_symbol
    UNION
    SELECT article_id, symbol FROM news_article
)
SELECT w.symbol,
       SUM(CASE WHEN src.is_demo = 0 AND n.duplicate_of IS NULL THEN 1 ELSE 0 END) AS real_n,
       SUM(CASE WHEN src.is_demo = 1 THEN 1 ELSE 0 END) AS demo_n,
       SUM(CASE WHEN n.duplicate_of IS NOT NULL THEN 1 ELSE 0 END) AS dup_n,
       SUM(CASE WHEN n.article_id IS NOT NULL AND n.published_at_utc IS NULL THEN 1 ELSE 0 END) AS no_ts,
       MAX(CASE WHEN src.is_demo = 0
                THEN COALESCE(n.published_local_date, substr(n.published_at_utc, 1, 10)) END) AS last_real,
       MAX(CASE WHEN src.is_demo = 1
                THEN COALESCE(n.published_local_date, substr(n.published_at_utc, 1, 10)) END) AS last_demo
FROM watchlist w
LEFT JOIN sym_articles sa ON sa.symbol = w.symbol
LEFT JOIN news_article n ON n.article_id = sa.article_id
LEFT JOIN source src ON src.source_id = n.source_id
WHERE w.active = 1
GROUP BY w.symbol
ORDER BY w.symbol
"""

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
    routes = {r.symbol: r for r in symbol_map.resolve_active(conn)}
    rss_on = news._env_flag("NEWS_RSS_FALLBACK")
    for r in conn.execute(NEWS_QUALITY_SQL):
        if r["real_n"]:                                     # real news first
            last = r["last_real"]
            age = (today - date.fromisoformat(last)).days if last else None
            status = "OK" if age is not None and age <= STALE_NEWS_DAYS else "STALE"
        elif r["demo_n"]:
            last = r["last_demo"]
            age = (today - date.fromisoformat(last)).days if last else None
            status = "DEMO"
        else:
            last, age, status = None, None, "MISSING"
        route = routes.get(r["symbol"])
        fh = (route.finnhub_symbol or f"skipped ({route.finnhub_basis})") if route else "-"
        rows.append({"area": "news", "item": r["symbol"], "status": status, "last_date": last, "age_days": age,
                     "detail": f"{r['real_n']} real, {r['demo_n']} demo, {r['dup_n']} cross-source duplicates, "
                               f"{r['no_ts']} without timestamp; Finnhub: {fh}; "
                               f"RSS fallback: {'on' if rss_on else 'off'}"})
    df = pd.DataFrame(rows)
    if not df.empty:
        df["age_days"] = df["age_days"].astype("Int64")   # whole days, blank when unknown (not 6.0)
    return df


def build_dim_date(start: str, end: str) -> pd.DataFrame:
    d = pd.date_range(start, end, freq="D")
    return pd.DataFrame({"date": d.date, "year": d.year, "month_num": d.month, "month": d.strftime("%b"),
                         "year_month": d.strftime("%Y-%m"), "week_of_year": d.isocalendar().week.values,
                         "weekday_num": d.weekday + 1, "weekday": d.strftime("%a"),
                         "is_weekday": d.weekday < 5})



DATE_COLUMNS = {   # every date a visual can slice by; dim_date must cover all of them
    "fact_perf_account_daily": ["as_of_date"],
    "fact_perf_household_daily": ["as_of_date"],
    "fact_daily_performance": ["as_of_date"],
    "fact_cash_flow": ["flow_date"],
    "fact_activity": ["trade_date"],
    "fact_holding_snapshot": ["as_of_date"],
    "fact_news": ["market_date", "published_local_date"],
    "fact_fx_cost": ["as_of_date"],
}

def _date_span(tables: dict[str, pd.DataFrame]) -> tuple[str, str] | None:
    """Earliest and latest YYYY-MM-DD across DATE_COLUMNS (timestamps are cut to the date)."""
    values: list[str] = []
    for name, cols in DATE_COLUMNS.items():
        df = tables.get(name)
        if df is None or df.empty:
            continue
        for col in cols:
            if col not in df.columns:
                continue
            s = df[col].dropna().astype(str).str[:10]
            s = s[s.str.match(r"^\d{4}-\d{2}-\d{2}$")]
            if not s.empty:
                values += [s.min(), s.max()]
    return (min(values), max(values)) if values else None

def export_all(conn, out_dir: str | Path) -> dict[str, int]:
    news.refresh_derived(conn)   # idempotent: Toronto dates, story keys, article-symbol links
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
                              "n.source_id, s.is_demo, n.published_local_date, n.market_date, n.story_key, "
                              "n.duplicate_of FROM news_article n JOIN source s USING (source_id) "
                              "ORDER BY n.published_at_utc DESC"),
        "bridge_news_symbol": _q(conn, "SELECT article_id, symbol, source_id, first_seen_utc "
                                       "FROM news_article_symbol ORDER BY symbol, article_id"),
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
        # --- contract 1.6: engine v2 in CAD, activity, holdings, provenance dims ---
        "fact_perf_account_daily": _q(conn, "SELECT p.*, a.is_demo FROM v_perf_account_daily p "
                                            "JOIN account a USING (account_id) "
                                            "ORDER BY p.account_id, p.as_of_date").round(10),
        "fact_perf_household_daily": _q(conn, "SELECT h.*, (SELECT MIN(a.is_demo) FROM perf_account_daily pa "
                                              "JOIN account a USING (account_id) "
                                              "WHERE pa.household_id = h.household_id) AS is_demo "
                                              "FROM v_perf_household_daily h "
                                              "ORDER BY h.household_id, h.as_of_date").round(10),
        "fact_activity": _q(conn, "SELECT * FROM v_activity_cad "
                                  "ORDER BY trade_date, account_id, activity_id").round(10),
        "fact_holding_snapshot": _q(conn, "SELECT * FROM v_holding_cad "
                                          "ORDER BY as_of_date, account_id, symbol").round(10),
        "dim_connection": _q(conn, "SELECT connection_id, institution_id, source_id, display_name, access_mode, "
                                   "status, created_at_utc, last_synced_at_utc, is_demo FROM connection "
                                   "ORDER BY connection_id"),   # never provider_ref
        "dim_institution": _q(conn, "SELECT institution_id, display_name, country, website_url, is_demo "
                                    "FROM institution ORDER BY institution_id"),
    }
    dsu = tables["dim_symbol_universe"]
    if not dsu.empty:
        dsu["lists"] = dsu["lists"].map(lambda s: ",".join(sorted(set(s.split(",")))) if s else s)
    fx_rows, fx_skipped = fx_converter.cost_grid(conn)
    tables["fact_fx_cost"] = pd.DataFrame(fx_rows, columns=fx_converter.COST_COLUMNS)
    if fx_skipped:
        log.info("fact_fx_cost: %s combination(s) skipped (no usable rate, fee or quote side).", fx_skipped)
    span = _date_span(tables)
    if span:
        tables["dim_date"] = build_dim_date(*span)
    tables["export_info"] = pd.DataFrame([{"exported_at_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                                           "note": "Educational analytics only. Not financial advice.",
                                           "contract_version": EXPORT_CONTRACT_VERSION}])
    counts = {}
    for name, df in tables.items():
        df.to_csv(out / f"{name}.csv", index=False)
        counts[name] = len(df)
    log.info("Exported %s tables to %s", len(counts), out)
    return counts
