"""Command line entry point:  python -m portfolio_pulse <command> [options]

Commands
  init-db                       create tables (safe to re-run)
  demo [--seed N]               load synthetic demo data (safe to re-run)
  import-csv FILE --map JSON    import your own export (add --dry-run to validate only)
  fetch-news [--source ...]     pull watchlist headlines (auto | finnhub | rss | none)
  export                        write CSVs for Power BI to data/powerbi/
  run-daily                     fetch-news + import everything in data/inbox/ + export
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
from pathlib import Path

from dotenv import load_dotenv

from . import csv_import, db, export, news, synthetic

ROOT = db.PROJECT_ROOT
log = logging.getLogger("portfolio_pulse")


def setup_logging(verbose: bool) -> None:
    (ROOT / "logs").mkdir(exist_ok=True)
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        handlers=[logging.StreamHandler(sys.stdout),
                  logging.FileHandler(ROOT / "logs" / "portfolio_pulse.log", encoding="utf-8")],
    )
    logging.getLogger("urllib3").setLevel(logging.WARNING)   # never echo request URLs/headers


def main(argv: list[str] | None = None) -> int:
    load_dotenv(ROOT / ".env")   # secrets live in .env, which .gitignore excludes
    p = argparse.ArgumentParser(prog="portfolio_pulse", description="Educational portfolio analytics (not advice).")
    p.add_argument("--db", default=os.getenv("PULSE_DB_PATH", str(ROOT / "data" / "pulse.db")))
    p.add_argument("-v", "--verbose", action="store_true")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("init-db")
    d = sub.add_parser("demo"); d.add_argument("--seed", type=int, default=42)
    i = sub.add_parser("import-csv"); i.add_argument("file"); i.add_argument("--map", required=True)
    i.add_argument("--dry-run", action="store_true")
    n = sub.add_parser("fetch-news"); n.add_argument("--source", default="auto",
                                                     choices=["auto", "finnhub", "rss", "none"])
    n.add_argument("--dry-run", action="store_true")
    sub.add_parser("export")
    r = sub.add_parser("run-daily"); r.add_argument("--map", default=str(ROOT / "config" / "column_map.json"))
    r.add_argument("--source", default="auto", choices=["auto", "finnhub", "rss", "none"])
    args = p.parse_args(argv)
    setup_logging(args.verbose)

    conn = db.connect(args.db)
    db.init_db(conn)
    try:
        if args.cmd == "demo":
            synthetic.load_demo(conn, args.seed)
        elif args.cmd == "import-csv":
            res = csv_import.import_csv(conn, args.file, csv_import.load_mapping(args.map),
                                        dry_run=args.dry_run, rejects_dir=ROOT / "data" / "rejects")
            for w in res.warnings:
                log.warning(w)
            for rej in res.rejected[:10]:
                log.warning("Rejected line %s: %s", rej["line"], rej["reason"])
            if res.rejected:
                log.warning("%s rejected row(s) saved to data/rejects/ for review.", len(res.rejected))
        elif args.cmd == "fetch-news":
            news.fetch_watchlist_news(conn, args.source, dry_run=args.dry_run)
        elif args.cmd == "export":
            export.export_all(conn, ROOT / "data" / "powerbi")
        elif args.cmd == "run-daily":
            run_daily(conn, Path(args.map), args.source)
    except (csv_import.ImportConfigError, FileNotFoundError, news.NewsSourceUnavailable) as exc:
        log.error("%s", exc)
        return 2
    finally:
        conn.close()
    return 0


def run_daily(conn, map_path: Path, source: str) -> None:
    try:
        news.fetch_watchlist_news(conn, source)
    except news.NewsSourceUnavailable as exc:
        log.warning("News step skipped: %s", exc)   # news failing must not block the rest
    inbox = ROOT / "data" / "inbox"
    files = sorted(inbox.glob("*.csv"))
    if files and not map_path.exists():
        log.warning("Found %s file(s) in data/inbox but no %s. Copy config/column_map.example.json "
                    "to column_map.json and edit it.", len(files), map_path.name)
    elif files:
        mapping = csv_import.load_mapping(map_path)
        for f in files:
            csv_import.import_csv(conn, f, mapping, rejects_dir=ROOT / "data" / "rejects")
    export.export_all(conn, ROOT / "data" / "powerbi")
