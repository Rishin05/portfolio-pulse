"""Read-only Finnhub coverage probe (Phase 5, step 3). Prints status codes, names and counts only.

Usage:  python scripts/finnhub_probe.py VFV VFV.TO QQC QQC.TO SHOP SHOP.TO
The key is read from .env and sent only in the X-Finnhub-Token header; it is never printed.
About 15 calls, paced at ~1/second, well under the free per-minute limit.
"""
from __future__ import annotations

import os
import sys
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import requests
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
BASE = "https://finnhub.io/api/v1"
PAUSE = 1.1


def get(session, path: str, params: dict):
    time.sleep(PAUSE)
    resp = session.get(f"{BASE}/{path}", params=params, timeout=15)
    try:
        body = resp.json()
    except ValueError:
        body = None
    return resp.status_code, body


def err_text(body) -> str:
    return body.get("error", "") if isinstance(body, dict) else ""


def main(symbols: list[str]) -> None:
    load_dotenv(ROOT / ".env")
    key = os.getenv("FINNHUB_API_KEY", "").strip()
    if not key:
        sys.exit("FINNHUB_API_KEY is not set in .env")
    s = requests.Session()
    s.headers.update({"X-Finnhub-Token": key,
                      "User-Agent": "portfolio-pulse/0.1 (personal educational project)"})
    to = date.today()
    frm = to - timedelta(days=30)
    print(f"Finnhub probe {datetime.now(timezone.utc):%Y-%m-%d %H:%M} UTC, news window {frm} to {to}\n")

    for base in sorted({sym.split(".")[0].upper() for sym in symbols}):
        code, body = get(s, "search", {"q": base})
        results = body.get("result", []) if code == 200 and isinstance(body, dict) else []
        hits = [f"{h.get('symbol')} ({h.get('type') or '?'}: {h.get('description') or '?'})"
                for h in results if isinstance(h, dict)
                and str(h.get("symbol", "")).split(".")[0].upper() == base]
        print(f"search {base!r}: HTTP {code} {err_text(body)}")
        for h in hits or ["no exact-ticker matches"]:
            print(f"    {h}")
    print()

    for sym in symbols:
        code_p, prof = get(s, "stock/profile2", {"symbol": sym})
        if code_p == 200 and isinstance(prof, dict) and prof:
            prof_txt = f"{prof.get('name')} | exchange={prof.get('exchange')} | currency={prof.get('currency')}"
        elif code_p == 200:
            prof_txt = "empty (profile2 doesn't know this symbol)"
        else:
            prof_txt = err_text(prof)
        code_n, news = get(s, "company-news", {"symbol": sym, "from": frm.isoformat(), "to": to.isoformat()})
        if code_n == 200 and isinstance(news, list):
            ts = sorted(n["datetime"] for n in news
                        if isinstance(n.get("datetime"), (int, float)) and n["datetime"] > 0)
            span = (f"{datetime.fromtimestamp(ts[0], timezone.utc):%Y-%m-%d} to "
                    f"{datetime.fromtimestamp(ts[-1], timezone.utc):%Y-%m-%d}") if ts else "-"
            pubs = sorted({n.get("source") or "?" for n in news})[:5]
            news_txt = f"{len(news)} articles, {span}, publishers: {', '.join(pubs) or '-'}"
        else:
            news_txt = err_text(news) or "(no JSON body)"
        print(f"{sym:<9} profile2     HTTP {code_p}: {prof_txt}")
        print(f"{'':<9} company-news HTTP {code_n}: {news_txt}")


if __name__ == "__main__":
    main([a.upper() for a in sys.argv[1:]] or ["VFV", "VFV.TO", "QQC", "QQC.TO", "SHOP", "SHOP.TO"])