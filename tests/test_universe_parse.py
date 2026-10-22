"""Universe parsing: fixtures reproduce the quirks seen in real iShares files on 2026-10-22."""
import pytest

from portfolio_pulse import universe as u

US_HEADER = ["Ticker", "Name", "Sector", "Asset Class", "Market Value", "Weight (%)", "Exchange", "Currency"]
CA_HEADER = ["Ticker", "Name", "Sector", "Weight (%)", "Exchange", "Currency"]


def workbook(rows, sheet="Holdings", boms=0):
    def cell(v):
        if isinstance(v, tuple):                                   # (ss:Index, value)
            return f'<ss:Cell ss:Index="{v[0]}"><ss:Data ss:Type="String">{v[1]}</ss:Data></ss:Cell>'
        return f'<ss:Cell><ss:Data ss:Type="String">{v}</ss:Data></ss:Cell>'
    body = "".join("<ss:Row>" + "".join(cell(v) for v in r) + "</ss:Row>" for r in rows)
    doc = ('<?xml version="1.0"?>\n<ss:Workbook xmlns:ss="urn:schemas-microsoft-com:office:spreadsheet">'
           f'<ss:Worksheet ss:Name="{sheet}"><ss:Table>{body}</ss:Table></ss:Worksheet></ss:Workbook>')
    return u.BOM * boms + doc.encode("utf-8")


def titles(name, as_of):
    return [["10/21/2026"], [name], ["Fund Holdings as of", as_of], []]


def test_us_file_with_bare_ampersands_class_shares_and_rejects():
    data = workbook(titles("iShares Test S&P Fund", "Oct 21, 2026") + [US_HEADER,
        ["NVDA", "NVIDIA", "Information Technology", "Equity", "1", "8.0", "NASDAQ", "USD"],
        ["MOG A", "MOOG INC CLASS A", "Industrials", "Equity", "1", "0.3", "NYSE", "USD"],
        ["--", "RENT THE RUNWAY INC", "Consumer Discretionary", "Equity", "2.51", "0",
         "NO MARKET (E.G. UNLISTED)", "USD"],
        ["ESZ6", "S&P500 EMINI DEC 26", "Cash and/or Derivatives", "Futures", "0", "0",
         "Index And Options Market", "USD"]])
    p = u.parse_ishares_xml(data)
    assert (p.fund_name, p.holdings_as_of) == ("iShares Test S&P Fund", "2026-10-21")
    assert [(r["symbol"], r["exchange"], r["currency"]) for r in p.rows] == \
           [("NVDA", "NASDAQ", "USD"), ("MOG.A", "NYSE", "USD")]
    assert [r["reason"] for r in p.rejects] == ["exchange not covered: NO MARKET (E.G. UNLISTED)",
                                                "cash or derivative"]


def test_canadian_file_double_bom_currency_from_exchange_and_merge():
    data = workbook([["21-Oct-2026"], ["iShares Test S&P/TSX Fund"], ["Fund Holdings as of", "Oct 21, 2026"],
                     [], CA_HEADER,
                     ["RY", "ROYAL BANK OF CANADA", "Financials", "6.0", "Toronto Stock Exchange", "CAD"],
                     ["NVDA", "NVIDIA", "Information Technology", "3.2", "NASDAQ", "CAD"],
                     ["2299955D", "CONSTELLATION SOFTWARE INC", "Information Technology", "0",
                      "Toronto Stock Exchange", "CAD"],
                     ["RY", "ROYAL BANK OF CANADA", "Financials", "1.5", "Toronto Stock Exchange", "CAD"]],
                    boms=2)
    p = u.parse_ishares_xml(data)
    rows = {r["symbol"]: r for r in p.rows}
    assert rows["NVDA"]["currency"] == "USD"                      # not the fund's CAD
    assert rows["RY"]["currency"] == "CAD" and rows["RY"]["weight_pct"] == pytest.approx(7.5)
    assert p.rejects == [{"line": 8, "ticker": "2299955D", "name": "CONSTELLATION SOFTWARE INC",
                          "reason": "not a standard exchange ticker"}]


def test_skipped_cells_via_ss_index_keep_columns_aligned():
    data = workbook(titles("Fund", "Oct 21, 2026") + [CA_HEADER,
                    ["SHOP", "SHOPIFY", (5, "Toronto Stock Exchange"), "CAD"]])
    p = u.parse_ishares_xml(data)
    assert [(r["symbol"], r["exchange"], r["sector"]) for r in p.rows] == [("SHOP", "TSX", None)]


def test_structural_problems_are_friendly_errors():
    with pytest.raises(u.UniverseError, match="Holdings"):
        u.parse_ishares_xml(workbook([["x"]], sheet="Performance"))
    with pytest.raises(u.UniverseError, match="Ticker"):
        u.parse_ishares_xml(workbook([["no header here"]]))
    with pytest.raises(u.UniverseError, match="missing column"):
        u.parse_ishares_xml(workbook([["Ticker", "Name"]]))


def test_a_csv_renamed_to_xls_is_a_friendly_error():
    with pytest.raises(u.UniverseError, match="Excel 2003 XML"):
        u.parse_ishares_xml(b"Ticker,Name\nRY,Royal Bank\n")


def test_manual_csv_defaults_and_rejects():
    text = ("\ufeffsymbol,name,exchange,currency,asset_type\n"
            "vfv,Vanguard S&P 500 Index ETF,TSX,,\n"
            "XUS.U,iShares Core S&P 500 (USD units),TSX,USD,etf\n"
            "VTI,Vanguard Total Stock Market ETF,NYSE_ARCA,,\n"
            "QQC,QQC Nasdaq-100 Index ETF,LSE,,\n"
            "VFV,Duplicate,TSX,,\n"
            "--,Bad ticker,TSX,,\n"
            ",,,,\n")
    p = u.parse_manual_csv(text)
    assert [(r["symbol"], r["exchange"], r["currency"], r["asset_type"]) for r in p.rows] == [
        ("VFV", "TSX", "CAD", "etf"), ("XUS.U", "TSX", "USD", "etf"), ("VTI", "NYSE_ARCA", "USD", "etf")]
    assert [(r["line"], r["reason"].split(" ")[0]) for r in p.rejects] == [
        (5, "exchange"), (6, "duplicate"), (7, "not")]
    with pytest.raises(u.UniverseError, match="missing column"):
        u.parse_manual_csv("ticker,name\nVFV,x\n")


@pytest.mark.parametrize("raw, expected", [
    ("mog a", "MOG.A"), ("BBD.B", "BBD.B"), ("XUS.U", "XUS.U"), ("--", None), ("2299955D", None)])
def test_normalize_ticker(raw, expected):
    assert u.normalize_ticker(raw) == expected


@pytest.mark.parametrize("text, expected", [
    ("NASDAQ", ("NASDAQ", "USD")), ("New York Stock Exchange Inc.", ("NYSE", "USD")),
    ("NYSE Arca", ("NYSE_ARCA", "USD")), ("Toronto Stock Exchange", ("TSX", "CAD")),
    ("Index And Options Market", None), ("NO MARKET (E.G. UNLISTED)", None)])
def test_map_exchange(text, expected):
    assert u.map_exchange(text) == expected