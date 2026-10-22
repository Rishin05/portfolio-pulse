"""Symbol universe: a searchable catalogue of listed symbols (not the watchlist).

Sources: iShares fund holdings files you download yourself, plus your own manual ETF list (CSV).
Verified 2026-10-22 on IVV, IWM (Russell 2000), XIU and XEQT files:
  * the real format is Excel 2003 XML saved as .xls, sometimes with 1-2 leading UTF-8 BOMs;
  * text contains bare '&' (URLs, "S&P"), which strict XML rejects -> escaped before parsing;
  * sheet 'Holdings': title rows (fund name in row 2, 'Fund Holdings as of'), then a header row
    starting with 'Ticker'; Canadian files have no 'Asset Class' column;
  * 'Currency' in Canadian files is the fund's reporting currency (CAD even for NVDA),
    so currency is derived from the exchange instead;
  * futures/cash rows, unlisted rows and non-standard tickers are rejected with a reason;
  * look-through files can list one stock several times -> merged, weights summed.

Loading (import_list): each list mirrors its latest file. The same file again changes nothing
(no batch, no rejects file); a newer file adds/updates/removes rows; an older file is refused;
a file whose title doesn't match a known list id (e.g. a Russell 2000 file loaded as IWB) is refused.
"""
from __future__ import annotations

import csv
import hashlib
import io
import logging
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from . import db

log = logging.getLogger(__name__)

SS = "{urn:schemas-microsoft-com:office:spreadsheet}"
BOM = b"\xef\xbb\xbf"
BARE_AMP = re.compile(rb"&(?!#\d+;|#x[0-9A-Fa-f]+;|[A-Za-z][A-Za-z0-9]*;)")
TICKER_RE = re.compile(r"^[A-Z][A-Z0-9]{0,5}(\.[A-Z]{1,2})?$")      # SHOP, MOG.A, BBD.B, XUS.U
LIST_RE = re.compile(r"^[A-Z0-9_]{1,12}$")
EXCHANGE_CURRENCY = {"NASDAQ": "USD", "NYSE": "USD", "NYSE_ARCA": "USD", "CBOE": "USD", "TSX": "CAD"}
EXCLUDED_SECTORS = {"cash and/or derivatives"}
MANUAL_REQUIRED = {"symbol", "name", "exchange"}

# A file loaded into one of these lists must say so in its title row (catches the wrong download).
KNOWN_FUNDS = {"IVV": "S&P 500", "IWB": "Russell 1000", "IWM": "Russell 2000", "XIU": "S&P/TSX 60",
               "XIC": "Composite", "XEQT": "Core Equity"}
_COMPARED = ("name", "raw_ticker", "currency", "asset_type", "sector", "weight_pct",
             "holdings_as_of", "fund_name", "is_demo")


class UniverseError(ValueError):
    """A file that can't be used at all (friendly message, never a stack trace)."""


@dataclass
class Parsed:
    fund_name: str | None = None
    holdings_as_of: str | None = None
    rows: list[dict] = field(default_factory=list)
    rejects: list[dict] = field(default_factory=list)


@dataclass
class ImportResult:
    list_id: str
    fund_name: str | None
    holdings_as_of: str | None
    read: int = 0
    added: int = 0
    updated: int = 0
    removed: int = 0
    unchanged: int = 0
    rejected: int = 0
    dry_run: bool = False


# ---------------------------------------------------------------- parsing (no database)

def map_exchange(text: str | None) -> tuple[str, str] | None:
    """Issuer exchange text -> (code, trading currency); None = not covered by our news sources."""
    t = (text or "").strip().lower()
    if t.startswith("nasdaq"):
        return "NASDAQ", "USD"
    if t.startswith("nyse arca"):
        return "NYSE_ARCA", "USD"
    if t.startswith("nyse") or t.startswith("new york stock exchange"):
        return "NYSE", "USD"
    if t.startswith("cboe"):
        return "CBOE", "USD"
    if t.startswith("toronto stock exchange"):
        return "TSX", "CAD"
    return None


def normalize_ticker(raw: str | None) -> str | None:
    """'mog a' -> 'MOG.A'; returns None for '--', '2299955D' and other non-exchange codes."""
    t = re.sub(r"\s+", ".", (raw or "").strip().upper())
    return t if TICKER_RE.match(t) else None


def _num(text: str) -> float | None:
    try:
        return float(text.replace(",", ""))
    except (ValueError, AttributeError):
        return None


def _as_of(text: str) -> str | None:
    for fmt in ("%b %d, %Y", "%d-%b-%Y", "%m/%d/%Y"):
        try:
            return datetime.strptime(text.strip(), fmt).date().isoformat()
        except ValueError:
            pass
    return None


def _cells(row) -> list[str]:
    cells: list[str] = []
    for c in row.findall(SS + "Cell"):
        idx = c.get(SS + "Index")                       # 1-based; skipped cells are omitted
        if idx and idx.isdigit():
            cells += [""] * max(0, int(idx) - 1 - len(cells))
        d = c.find(SS + "Data")
        cells.append("".join(d.itertext()).strip() if d is not None else "")
    return cells


def _load_workbook(data: bytes) -> ET.Element:
    while data.startswith(BOM):
        data = data[3:]
    data = BARE_AMP.sub(b"&amp;", data)
    try:
        return ET.fromstring(data)
    except ET.ParseError as exc:
        raise UniverseError(f"Not a readable Excel 2003 XML holdings file ({exc}).") from None


def parse_ishares_xml(data: bytes) -> Parsed:
    root = _load_workbook(data)
    ws = next((s for s in root.findall(SS + "Worksheet")
               if (s.get(SS + "Name") or "").strip().lower() == "holdings"), None)
    if ws is None:
        raise UniverseError("No 'Holdings' worksheet found in this file.")
    rows = [_cells(r) for r in ws.iter(SS + "Row")]
    out = Parsed()
    header_at = None
    for i, cells in enumerate(rows):
        first = cells[0] if cells else ""
        if first == "Ticker":
            header_at = i
            break
        if first.lower().startswith("fund holdings as of") and len(cells) > 1:
            out.holdings_as_of = _as_of(cells[1])
        if i == 1 and first:
            out.fund_name = first
    if header_at is None:
        raise UniverseError("No header row starting with 'Ticker' was found.")
    header = [h.strip().lower() for h in rows[header_at]]
    missing = {"ticker", "name", "exchange"} - set(header)
    if missing:
        raise UniverseError(f"Holdings sheet is missing column(s): {sorted(missing)}.")
    col = {h: i for i, h in enumerate(header) if h}

    def get(cells: list[str], key: str) -> str:
        i = col.get(key)
        return cells[i].strip() if i is not None and i < len(cells) else ""

    merged: dict[tuple[str, str], dict] = {}
    for n, cells in enumerate(rows[header_at + 1:], start=header_at + 2):
        if not any(c.strip() for c in cells):
            continue
        raw, name = get(cells, "ticker"), get(cells, "name")

        def reject(reason: str) -> None:
            out.rejects.append({"line": n, "ticker": raw, "name": name, "reason": reason})

        if get(cells, "sector").lower() in EXCLUDED_SECTORS:
            reject("cash or derivative")
            continue
        asset_class = get(cells, "asset class")
        if asset_class and asset_class.lower() != "equity":
            reject(f"asset class {asset_class}")
            continue
        exch = map_exchange(get(cells, "exchange"))
        if exch is None:
            reject(f"exchange not covered: {get(cells, 'exchange') or '-'}")
            continue
        sym = normalize_ticker(raw)
        if sym is None:
            reject("not a standard exchange ticker")
            continue
        weight = _num(get(cells, "weight (%)"))
        key = (sym, exch[0])
        if key in merged:                                  # look-through duplicate
            if weight is not None:
                merged[key]["weight_pct"] = (merged[key]["weight_pct"] or 0.0) + weight
            continue
        merged[key] = {"symbol": sym, "exchange": exch[0], "currency": exch[1], "name": name,
                       "raw_ticker": raw, "asset_type": "equity",
                       "sector": get(cells, "sector") or None, "weight_pct": weight}
    out.rows = list(merged.values())
    return out


def parse_manual_csv(text: str) -> Parsed:
    """Your own list: symbol,name,exchange[,currency][,asset_type]. Exchange: NASDAQ|NYSE|NYSE_ARCA|CBOE|TSX."""
    reader = csv.DictReader(io.StringIO(text.lstrip("\ufeff")))
    fields = {(f or "").strip().lower() for f in (reader.fieldnames or [])}
    missing = MANUAL_REQUIRED - fields
    if missing:
        raise UniverseError(f"Manual list is missing column(s): {sorted(missing)}.")
    out = Parsed(fund_name="manual list")
    seen: set[tuple[str, str]] = set()
    for n, raw_row in enumerate(reader, start=2):
        row = {k.strip().lower(): (v or "").strip() for k, v in raw_row.items()
               if k is not None and not isinstance(v, list)}
        if not any(row.values()):
            continue
        raw, name = row.get("symbol", ""), row.get("name", "")

        def reject(reason: str) -> None:
            out.rejects.append({"line": n, "ticker": raw, "name": name, "reason": reason})

        sym = normalize_ticker(raw)
        exch = row.get("exchange", "").upper()
        cur = (row.get("currency") or EXCHANGE_CURRENCY.get(exch, "")).upper()
        atype = (row.get("asset_type") or "etf").lower()
        if sym is None:
            reject("not a standard exchange ticker")
        elif exch not in EXCHANGE_CURRENCY:
            reject(f"exchange must be one of {sorted(EXCHANGE_CURRENCY)}")
        elif cur not in ("USD", "CAD"):
            reject("currency must be USD or CAD")
        elif atype not in ("equity", "etf"):
            reject("asset_type must be equity or etf")
        elif not name:
            reject("name is required")
        elif (sym, exch) in seen:
            reject("duplicate symbol and exchange")
        else:
            seen.add((sym, exch))
            out.rows.append({"symbol": sym, "exchange": exch, "currency": cur, "name": name,
                             "raw_ticker": raw, "asset_type": atype, "sector": None, "weight_pct": None})
    return out


# ---------------------------------------------------------------- loading (database)

def _same(a, b) -> bool:
    if isinstance(a, float) or isinstance(b, float):
        return a is not None and b is not None and round(a, 6) == round(b, 6)
    return a == b


def _write_rejects(rejects: list[dict], rejects_dir: Path, list_id: str) -> Path:
    rejects_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    path = rejects_dir / f"universe_{list_id}_{stamp}.csv"
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=["line", "ticker", "name", "reason"])
        w.writeheader()
        w.writerows(rejects)
    return path


def import_list(conn, path: str | Path, list_id: str, dry_run: bool = False,
                is_demo: bool = False, rejects_dir: Path | None = None) -> ImportResult:
    """Load one holdings file (.xls, Excel 2003 XML) or a manual CSV into symbol_universe."""
    path = Path(path)
    lid = (list_id or "").strip().upper()
    if not LIST_RE.match(lid):
        raise UniverseError(f"--list must be 1-12 letters/digits/_ (e.g. IVV, MANUAL), got {list_id!r}.")
    data = path.read_bytes()
    manual = path.suffix.lower() == ".csv"
    parsed = parse_manual_csv(data.decode("utf-8-sig")) if manual else parse_ishares_xml(data)
    expected = KNOWN_FUNDS.get(lid)
    if not manual and expected and expected.lower() not in (parsed.fund_name or "").lower():
        raise UniverseError(f"{path.name} is '{parsed.fund_name}', not the {lid} fund "
                            f"(expected '{expected}' in its title). Nothing changed.")
    if not manual and not parsed.holdings_as_of:
        raise UniverseError(f"{path.name} has no 'Fund Holdings as of' date. Nothing changed.")

    existing = {(r["symbol"], r["exchange"]): dict(r) for r in
                conn.execute("SELECT * FROM symbol_universe WHERE list_id = ?", (lid,))}
    stored_as_of = max((r["holdings_as_of"] for r in existing.values() if r["holdings_as_of"]), default=None)
    if parsed.holdings_as_of and stored_as_of and parsed.holdings_as_of < stored_as_of:
        raise UniverseError(f"{path.name} is dated {parsed.holdings_as_of}, older than {lid}'s stored "
                            f"{stored_as_of}. Nothing changed.")

    new = {}
    for r in parsed.rows:
        new[(r["symbol"], r["exchange"])] = {**r, "holdings_as_of": parsed.holdings_as_of,
                                             "fund_name": parsed.fund_name, "is_demo": int(is_demo)}
    added = sorted(new.keys() - existing.keys())
    removed = sorted(existing.keys() - new.keys())
    updated = sorted(k for k in new.keys() & existing.keys()
                     if not all(_same(new[k][c], existing[k][c]) for c in _COMPARED))
    res = ImportResult(lid, parsed.fund_name, parsed.holdings_as_of, read=len(parsed.rows) + len(parsed.rejects),
                       added=len(added), updated=len(updated), removed=len(removed),
                       unchanged=len(new) - len(added) - len(updated), rejected=len(parsed.rejects),
                       dry_run=dry_run)

    writing = not dry_run and bool(added or updated or removed or not existing)
    if writing:
        if parsed.rejects and rejects_dir is not None:
            _write_rejects(parsed.rejects, rejects_dir, lid)
        source_id = "csv_import" if manual else "ishares_holdings"
        batch = db.start_batch(conn, source_id, f"universe {lid} from {path.name}",
                               content_sha256=hashlib.sha256(data).hexdigest())
        now = db.utc_now()
        for k in added:
            db.insert_ignore(conn, "symbol_universe", {
                "list_id": lid, **{c: new[k][c] for c in ("symbol", "exchange", *_COMPARED)},
                "source_file": path.name, "source_id": source_id, "batch_id": batch, "loaded_at_utc": now})
        for k in updated:
            sets = ", ".join(f"{c} = ?" for c in _COMPARED)
            conn.execute(f"UPDATE symbol_universe SET {sets}, source_file = ?, batch_id = ?, loaded_at_utc = ? "
                         "WHERE list_id = ? AND symbol = ? AND exchange = ?",
                         (*[new[k][c] for c in _COMPARED], path.name, batch, now, lid, *k))
        for k in removed:
            conn.execute("DELETE FROM symbol_universe WHERE list_id = ? AND symbol = ? AND exchange = ?",
                         (lid, *k))
        db.finish_batch(conn, batch, read=res.read, inserted=res.added, skipped=res.unchanged,
                        rejected=res.rejected)
        conn.commit()
    log.info("Universe %s%s: '%s' as of %s - added %s, updated %s, removed %s, unchanged %s, rejected %s%s.",
             lid, " (dry run, nothing written)" if dry_run else "", res.fund_name, res.holdings_as_of or "-",
             res.added, res.updated, res.removed, res.unchanged, res.rejected,
             " (see data/rejects/)" if writing and res.rejected and rejects_dir is not None else "")
    return res