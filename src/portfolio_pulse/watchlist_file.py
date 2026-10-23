"""Editable watchlist file (config/watchlist.xlsx or .csv): you curate it, `watchlist import` applies it.

Columns (only `symbol` is required; a blank cell means "leave as is" or "fill from the universe"):
  symbol, active (1/0 or yes/no; blank = 1), name, exchange, currency,
  finnhub (symbol to ask Finnhub for, or '-' = never use Finnhub), rss (Google News phrase or
  search syntax), note (why; stored with the news mapping)
Rules:
  * Every row becomes origin='manual', so your file wins over the holdings sync.
  * Symbols NOT in the file are left alone. Switch one off with active=0; don't delete the line.
  * Blank name/exchange/currency are filled from the symbol universe when the ticker has exactly
    one listing there (or one on the exchange you give).
  * Rows are planned and validated first, then only real differences are applied, so the same
    file again changes nothing. Bad rows go to data/rejects/ and are not applied.
"""
from __future__ import annotations

import csv
import io
import logging
import warnings
import zipfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from . import symbol_map, universe, watchlist
from .symbol_map import UNSET
from .watchlist import SYMBOL_RE, WatchlistError

log = logging.getLogger(__name__)

SOFT_LIMIT = 50                     # more active symbols than this makes daily news runs slow
COLUMNS = {"symbol", "active", "name", "exchange", "currency", "finnhub", "rss", "note"}
TRUE, FALSE = {"", "1", "yes", "y", "true"}, {"0", "no", "n", "false"}
SKIP_FINNHUB = {"-", "none"}
WL_COLUMNS = ["symbol", "active", "name", "exchange", "currency", "finnhub", "rss", "note"]
SHEET_WL, SHEET_U, SHEET_HELP = "Watchlist", "Universe", "Help"


def split_pick(cell: str) -> tuple[str, str | None]:
    """'SHOP | TSX | SHOPIFY...' -> ('SHOP', 'TSX'); a plain 'RY' -> ('RY', None)."""
    parts = [p.strip() for p in (cell or "").split("|")]
    return parts[0], (parts[1].upper() or None) if len(parts) > 1 else None


def pick_label(symbol: str, exchange: str, name: str) -> str:
    return f"{symbol} | {exchange} | {name}"


def default_path(config_dir: Path) -> tuple[Path | None, str | None]:
    """config/watchlist.xlsx if present, else config/watchlist.csv; plus a warning if both exist."""
    xlsx, csv_ = config_dir / "watchlist.xlsx", config_dir / "watchlist.csv"
    if xlsx.exists():
        note = ("Both config/watchlist.xlsx and config/watchlist.csv exist; using the .xlsx. "
                "Rename or delete the .csv to avoid confusion.") if csv_.exists() else None
        return xlsx, note
    return (csv_, None) if csv_.exists() else (None, None)


def _openpyxl():
    try:
        import openpyxl
        return openpyxl
    except ImportError:
        raise WatchlistError("Excel support needs openpyxl: pip install -r requirements.txt") from None


def _cell_text(v) -> str:
    if v is None:
        return ""
    if isinstance(v, bool):
        return "1" if v else "0"
    if isinstance(v, float) and v.is_integer():
        return str(int(v))
    return str(v).strip()


def _parse_xlsx(path: Path) -> tuple[list[tuple[int, dict]], set[str]]:
    opx = _openpyxl()
    from openpyxl.utils.exceptions import InvalidFileException
    try:
        with warnings.catch_warnings():   # Excel's newer dropdown format; reading rows is unaffected
            warnings.filterwarnings("ignore", message="Data Validation extension is not supported",
                                    category=UserWarning)
            wb = opx.load_workbook(path, read_only=True, data_only=True)
    except (zipfile.BadZipFile, InvalidFileException, KeyError, OSError) as exc:
        raise WatchlistError(f"{path.name} isn't a readable .xlsx file ({type(exc).__name__}).") from None
    try:
        if SHEET_WL not in wb.sheetnames:
            raise WatchlistError(f"{path.name} has no '{SHEET_WL}' sheet. Recreate it with `watchlist picker`.")
        it = wb[SHEET_WL].iter_rows(values_only=True)
        header = [_cell_text(h).lower() for h in next(it, ())]
        if "symbol" not in header:
            raise WatchlistError(f"The '{SHEET_WL}' sheet needs a 'symbol' column in row 1.")
        rows = []
        for n, values in enumerate(it, start=2):
            row = {h: _cell_text(v) for h, v in zip(header, values) if h}
            if any(row.values()):
                rows.append((n, row))
        return rows, {h for h in header if h} - COLUMNS
    finally:
        wb.close()


@dataclass
class FileResult:
    read: int = 0
    added: list[str] = field(default_factory=list)
    updated: list[str] = field(default_factory=list)
    deactivated: list[str] = field(default_factory=list)
    unchanged: list[str] = field(default_factory=list)
    rejects: list[dict] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    dry_run: bool = False


def _parse(text: str) -> tuple[list[tuple[int, dict]], set[str]]:
    reader = csv.DictReader(io.StringIO(text.lstrip("\ufeff")))
    fields = {(f or "").strip().lower() for f in (reader.fieldnames or [])} - {""}
    if "symbol" not in fields:
        raise WatchlistError("The watchlist file needs a 'symbol' column (see examples/watchlist.example.csv).")
    rows = []
    for n, raw in enumerate(reader, start=2):
        row = {k.strip().lower(): (v or "").strip() for k, v in raw.items()
               if k is not None and not isinstance(v, list)}
        if any(row.values()):
            rows.append((n, row))
    return rows, fields - COLUMNS


def _plan(conn, row: dict) -> tuple[dict, str | None]:
    """Validate one row and work out the desired state. Raises WatchlistError for a bad row."""
    sym_text, picked_exch = split_pick(row.get("symbol", ""))
    sym = watchlist.normalize_symbol(sym_text)
    flag = row.get("active", "").lower()
    if flag not in TRUE | FALSE:
        raise WatchlistError(f"active must be 1/0 or yes/no, got {row.get('active')!r}.")
    name = row.get("name") or None
    exch = (row.get("exchange") or picked_exch or "").upper() or None
    cur = (row.get("currency") or "").upper() or None
    if cur and len(cur) != 3:
        raise WatchlistError(f"currency must be a 3-letter code like CAD, got {cur!r}.")
    warn = None
    if not (name and exch and cur):
        try:
            info = universe.lookup(conn, sym, exch)
            name, exch, cur = name or info["name"], exch or info["exchange"], cur or info["currency"]
        except universe.UniverseError as exc:
            warn = f"{sym}: details not filled from the universe ({exc})"
    fh = row.get("finnhub", "")
    if fh == "":
        finnhub = UNSET
    elif fh.lower() in SKIP_FINNHUB:
        finnhub = None
    else:
        finnhub = fh.upper()
        if not SYMBOL_RE.match(finnhub):
            raise WatchlistError(f"finnhub must be a ticker like SHOP or '-', got {fh!r}.")
    return {"symbol": sym, "active": int(flag in TRUE), "name": name, "exchange": exch, "currency": cur,
            "finnhub": finnhub, "rss": row.get("rss") or UNSET, "note": row.get("note") or UNSET}, warn


def _map_changes(conn, p: dict) -> bool:
    if p["finnhub"] is UNSET and p["rss"] is UNSET and p["note"] is UNSET:
        return False
    m = conn.execute("SELECT finnhub_symbol, rss_query, note FROM symbol_map WHERE symbol = ?",
                     (p["symbol"],)).fetchone()
    if m is None:
        return True
    return ((p["finnhub"] is not UNSET and p["finnhub"] != m["finnhub_symbol"])
            or (p["rss"] is not UNSET and p["rss"] != m["rss_query"])
            or (p["note"] is not UNSET and p["note"] != m["note"]))


def _classify(conn, p: dict) -> str:
    w = conn.execute("SELECT * FROM watchlist WHERE symbol = ?", (p["symbol"],)).fetchone()
    if w is None:
        return "added" if p["active"] else "deactivated"
    row_change = (w["origin"] != "manual" or w["active"] != p["active"]
                  or any(p[k] is not None and p[k] != w[c]
                         for k, c in (("name", "company_name"), ("exchange", "exchange"), ("currency", "currency"))))
    if not (row_change or _map_changes(conn, p)):
        return "unchanged"
    return "deactivated" if w["active"] and not p["active"] else "updated"


def _apply(conn, p: dict) -> None:
    map_change = _map_changes(conn, p)                     # check before the watchlist row changes
    watchlist.add_manual(conn, p["symbol"], p["name"], None, p["exchange"], p["currency"])
    if not p["active"]:
        watchlist.remove(conn, p["symbol"])
    if map_change:
        symbol_map.set_mapping(conn, p["symbol"], finnhub=p["finnhub"], rss_query=p["rss"], note=p["note"])


def _write_rejects(rejects: list[dict], rejects_dir: Path) -> Path:
    rejects_dir.mkdir(parents=True, exist_ok=True)
    path = rejects_dir / f"watchlist_{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}.csv"
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=["line", "symbol", "reason"])
        w.writeheader()
        w.writerows(rejects)
    return path


def import_file(conn, path: str | Path, dry_run: bool = False, rejects_dir: Path | None = None) -> FileResult:
    path = Path(path)
    rows, unknown = (_parse_xlsx(path) if path.suffix.lower() == ".xlsx"
                     else _parse(path.read_text(encoding="utf-8-sig")))
    res = FileResult(read=len(rows), dry_run=dry_run)
    if unknown:
        res.warnings.append(f"Ignored unknown column(s): {sorted(unknown)}")
    plans, seen = [], set()
    for n, row in rows:
        try:
            p, warn = _plan(conn, row)
        except WatchlistError as exc:
            res.rejects.append({"line": n, "symbol": row.get("symbol", ""), "reason": str(exc)})
            continue
        if p["symbol"] in seen:
            res.rejects.append({"line": n, "symbol": p["symbol"], "reason": "duplicate symbol in file"})
            continue
        seen.add(p["symbol"])
        if warn:
            res.warnings.append(warn)
        plans.append(p)
    for p in plans:
        kind = _classify(conn, p)
        getattr(res, kind).append(p["symbol"])
        if kind != "unchanged" and not dry_run:
            _apply(conn, p)
    if res.rejects and rejects_dir is not None and not dry_run:
        _write_rejects(res.rejects, rejects_dir)
    active = conn.execute("SELECT COUNT(*) FROM watchlist WHERE active = 1").fetchone()[0]
    if active > SOFT_LIMIT:
        res.warnings.append(f"{active} active watchlist symbols (> {SOFT_LIMIT}): daily news runs will be slow "
                            "and use more of the free API limits.")
    for w in res.warnings:
        log.warning(w)
    log.info("Watchlist file%s: %s row(s); added %s, updated %s, deactivated %s, unchanged %s, rejected %s%s.",
             " (dry run, nothing written)" if dry_run else "", res.read, res.added or "none",
             res.updated or "none", res.deactivated or "none", len(res.unchanged), len(res.rejects),
             " (see data/rejects/)" if res.rejects and rejects_dir is not None and not dry_run else "")
    return res


_SEED_SQL = """
SELECT w.symbol, w.active, w.company_name, w.exchange, w.currency,
       m.symbol AS mapped, m.finnhub_symbol, m.rss_query, m.note,
       (SELECT MAX(u.name) FROM symbol_universe u
        WHERE u.symbol = w.symbol AND u.exchange = w.exchange) AS universe_name
FROM watchlist w LEFT JOIN symbol_map m ON m.symbol = w.symbol
WHERE w.origin = 'manual' ORDER BY w.symbol
"""
_UNIVERSE_SQL = """
SELECT symbol, exchange, MAX(currency) AS currency, MAX(asset_type) AS asset_type,
       GROUP_CONCAT(list_id) AS lists, MAX(name) AS name
FROM symbol_universe GROUP BY symbol, exchange ORDER BY symbol, exchange
"""
_HELP = [
    "Edit the Watchlist sheet, save, then run:  python -m portfolio_pulse watchlist import",
    "symbol: pick from the dropdown (recent Microsoft 365 Excel lets you type to search it) or type a ticker.",
    "active: 1 = on, 0 = off. To remove a symbol set 0; don't delete the row.",
    "Blank name / exchange / currency are filled from the symbol universe.",
    "finnhub: the ticker to ask Finnhub for, or - to never use Finnhub. rss: a Google News phrase.",
    "Run `watchlist picker` again to refresh the Universe sheet; your Watchlist rows are kept.",
    "Educational analytics only. Not financial advice.",
]


def _typed(col: str, value: str):
    if value == "":
        return None
    return int(value) if col == "active" and value.isdigit() else value


def write_picker(conn, path: str | Path) -> dict:
    """Write the Excel picker. Keeps an existing file's Watchlist rows; otherwise seeds from manual rows."""
    opx = _openpyxl()
    from openpyxl.styles import Font
    from openpyxl.worksheet.datavalidation import DataValidation
    path = Path(path)
    kept = _parse_xlsx(path)[0] if path.exists() else None

    wb = opx.Workbook()
    ws = wb.active
    ws.title = SHEET_WL
    ws.append(WL_COLUMNS)
    if kept is not None:
        for _, row in kept:
            ws.append([_typed(c, row.get(c, "")) for c in WL_COLUMNS])
        seeded = len(kept)
    else:
        seeded = 0
        for r in conn.execute(_SEED_SQL):
            label = pick_label(r["symbol"], r["exchange"], r["universe_name"]) if r["universe_name"] else r["symbol"]
            fh = (r["finnhub_symbol"] or "-") if r["mapped"] else ""
            ws.append([_typed(c, v) for c, v in zip(WL_COLUMNS, [
                label, str(r["active"]), r["company_name"] or "", r["exchange"] or "", r["currency"] or "",
                fh, r["rss_query"] or "", r["note"] or ""])])
            seeded += 1

    uni = wb.create_sheet(SHEET_U)
    uni.append(["pick", "symbol", "exchange", "currency", "type", "lists", "name"])
    n = 0
    for r in conn.execute(_UNIVERSE_SQL):
        lists = ",".join(sorted(set((r["lists"] or "").split(","))))
        uni.append([pick_label(r["symbol"], r["exchange"], r["name"]), r["symbol"], r["exchange"],
                    r["currency"], r["asset_type"], lists, r["name"]])
        n += 1
    if n:
        dv = DataValidation(type="list", formula1=f"{SHEET_U}!$A$2:$A${n + 1}", allow_blank=True,
                            showErrorMessage=False, showInputMessage=True, promptTitle="Pick or type",
                            prompt="Pick a symbol from the list, or type a ticker.")
        ws.add_data_validation(dv)
        dv.add("A2:A1000")

    help_ws = wb.create_sheet(SHEET_HELP)
    for line in _HELP:
        help_ws.append([line])
    for sheet, widths in ((ws, [44, 8, 34, 10, 10, 10, 44, 50]), (uni, [60, 10, 10, 10, 8, 20, 50]),
                          (help_ws, [110])):
        for cell in sheet[1]:
            cell.font = Font(bold=True)
        sheet.freeze_panes = "A2"
        for i, w in enumerate(widths):
            sheet.column_dimensions[chr(65 + i)].width = w
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        wb.save(path)
    except PermissionError:
        raise WatchlistError(f"Couldn't write {path.name}. If it's open in Excel, close it and try again.") from None
    log.info("Excel picker written to %s: %s watchlist row(s) (%s), %s universe listing(s).", path.name, seeded,
             "kept from the existing file" if kept is not None else "seeded from your manual watchlist", n)
    return {"rows": seeded, "universe": n, "kept": kept is not None}