"""Command line entry point:  python -m portfolio_pulse <command> [options]

Commands
  init-db                       create tables (safe to re-run)
  demo [--seed N]               load synthetic demo data (safe to re-run)
  import-csv FILE --map JSON    import your own export (add --dry-run to validate only)
  fetch-news [--source ...]     pull watchlist headlines (auto | finnhub | rss | none)
  export                        write CSVs for Power BI to data/powerbi/
    run-daily                     fetch-news + import everything in data/inbox/ + export
  snaptrade-connect [--broker]  print a one-time, read-only SnapTrade Connection Portal link
  snaptrade-pull [--dry-run]    save masked raw SnapTrade JSON to data/raw/snaptrade/
  snaptrade-load [--dry-run]    load the newest raw pull into the database (re-run: 0 new rows)
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from pathlib import Path

from dotenv import load_dotenv

from . import (csv_import, db, export, news, snaptrade_load, snaptrade_readonly,
               snaptrade_sync, synthetic)

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
    c = sub.add_parser("snaptrade-connect")
    c.add_argument("--broker", default=None,
                   help="optional SnapTrade broker slug; omit to choose in the portal")
    sp = sub.add_parser("snaptrade-pull")
    sp.add_argument("--dry-run", action="store_true", help="fetch and count, write nothing")
    sp.add_argument("--show-shape", action="store_true",
                    help="print response keys/types only (no values) for debugging")
    sl = sub.add_parser("snaptrade-load")
    sl.add_argument("--folder", default=None, help="raw folder to load (default: newest)")
    sl.add_argument("--dry-run", action="store_true", help="validate and count, then roll back")
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
        elif args.cmd == "snaptrade-connect":
            snaptrade_connect(args.broker)
        elif args.cmd == "snaptrade-pull":
            snaptrade_pull(args.dry_run, args.show_shape)
        elif args.cmd == "snaptrade-load":
            snaptrade_load_cmd(conn, args.folder, args.dry_run)
    except (csv_import.ImportConfigError, FileNotFoundError, news.NewsSourceUnavailable,
            snaptrade_readonly.SnapTradeError, snaptrade_load.RawDataError) as exc:
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



def snaptrade_connect(broker: str | None) -> None:
    """Print a one-time, read-only Connection Portal link.

    The link is printed (stdout only) and never logged: it is a short-lived
    login URL and must not end up in logs/portfolio_pulse.log.
    """
    client = snaptrade_readonly.ReadOnlySnapTrade()
    url = client.connection_portal_url(broker=broker)
    print("\nOpen this link in your browser within 5 minutes to connect a brokerage "
          "(read-only access):\n")
    print(url)
    print("\nComplete the broker login inside SnapTrade, then close the tab. "
          "No data is downloaded until a later step.\n")
    log.info("Generated a read-only SnapTrade Connection Portal link (link not logged).")



def snaptrade_pull(dry_run: bool, show_shape: bool) -> None:
    """Fetch read-only SnapTrade data and save masked raw JSON."""
    import json

    client = snaptrade_readonly.ReadOnlySnapTrade()
    res = snaptrade_sync.pull_raw(client, ROOT / "data" / "raw" / "snaptrade", dry_run=dry_run)
    summary = (f"{res.accounts} account(s), {res.positions} position(s), "
               f"{res.activities} activity row(s)")
    if dry_run:
        log.info("SnapTrade pull (dry run): %s; nothing written.", summary)
    else:
        log.info("SnapTrade pull: %s; saved %s file(s) to %s", summary, len(res.files),
                 res.folder.relative_to(ROOT))
    for err in res.errors:
        log.warning(err)
    if show_shape:
        print(json.dumps(res.shapes, indent=2))



def snaptrade_load_cmd(conn, folder: str | None, dry_run: bool) -> None:
    """Load a raw SnapTrade pull into the database."""
    raw_root = ROOT / "data" / "raw" / "snaptrade"
    target = Path(folder) if folder else snaptrade_load.latest_raw_folder(raw_root)
    res = snaptrade_load.load_folder(conn, target, dry_run=dry_run)
    log.info("SnapTrade load%s from %s (as of %s): read %s, inserted %s, skipped %s, rejected %s.",
             " (dry run, rolled back)" if dry_run else "", target.name, res.as_of_date,
             res.read, res.inserted, res.skipped, res.rejected)
    for w in res.warnings:
        log.warning(w)