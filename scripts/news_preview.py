"""Print what a news source would return for ONE watchlist symbol. Nothing is stored.

Usage:  python scripts/news_preview.py VTI            (RSS, using the mapped query)
        python scripts/news_preview.py SHOP finnhub
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))   # works without pip install -e

from dotenv import load_dotenv  # noqa: E402

from portfolio_pulse import db, news, symbol_map  # noqa: E402


def main(symbol: str, source: str = "rss") -> None:
    load_dotenv(db.PROJECT_ROOT / ".env")
    conn = db.connect(os.getenv("PULSE_DB_PATH") or db.PROJECT_ROOT / "data" / "pulse.db")
    r = symbol_map.resolve(conn, symbol)
    if source == "finnhub":
        if not r.finnhub_symbol:
            sys.exit(f"{r.symbol}: Finnhub is not used for this symbol ({r.finnhub_basis}).")
        print(f"Finnhub symbol: {r.finnhub_symbol}")
        items = news.fetch_finnhub(r.finnhub_symbol, os.getenv("FINNHUB_API_KEY", "").strip())
    else:
        print(f"RSS query: {news.build_rss_query(r.rss_query)}")
        items = news.fetch_google_news_rss(r.rss_query)
    for a in items:
        print(f"{a['published_at_utc'] or 'no date':<21} {(a['publisher'] or '?')[:22]:<23} {a['headline'][:90]}")
    print(f"{len(items)} item(s); nothing stored.")
    conn.close()


if __name__ == "__main__":
    if len(sys.argv) < 2:
        sys.exit(__doc__)
    main(sys.argv[1], sys.argv[2] if len(sys.argv) > 2 else "rss")