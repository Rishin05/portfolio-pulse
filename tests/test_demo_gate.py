import csv

import pytest

from portfolio_pulse import demo_gate


def _write(folder, name, header, rows):
    with (folder / f"{name}.csv").open("w", encoding="utf-8", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(header)
        w.writerows(rows)


@pytest.fixture
def good(tmp_path):
    _write(tmp_path, "dim_account", ["account_id", "display_name", "is_demo", "connection_id"],
           [["DEMO-TFSA", "Demo TFSA", "1", ""]])
    _write(tmp_path, "fact_cash_flow", ["cash_flow_id", "account_id", "amount"], [["c1", "DEMO-TFSA", "100"]])
    _write(tmp_path, "fact_news", ["article_id", "url", "is_demo"], [["a1", "https://www.example.com/x", "1"]])
    _write(tmp_path, "dim_source", ["source_id", "is_demo"], [["finnhub", "0"], ["synthetic", "1"]])
    return tmp_path


def test_clean_demo_export_passes(good):
    assert demo_gate.check_export(good) == []
    assert demo_gate.main([str(good)]) == 0


def test_row_not_flagged_demo_fails(good):
    _write(good, "fact_news", ["article_id", "url", "is_demo"], [["a1", "https://example.com/x", "0"]])
    assert "fact_news: 1 row(s) not flagged DEMO" in demo_gate.check_export(good)


def test_snaptrade_paper_account_fails(good):
    _write(good, "dim_account", ["account_id", "display_name", "is_demo", "connection_id"],
           [["DEMO-TFSA", "Demo TFSA", "1", ""], ["ST-alpaca-paper-x", "Paper", "1", "ST-abc"]])
    assert any("ST-alpaca-paper-x" in p for p in demo_gate.check_export(good))


def test_fact_row_for_unknown_account_fails(good):
    _write(good, "fact_cash_flow", ["cash_flow_id", "account_id", "amount"], [["c1", "REAL-1", "100"]])
    assert any("not in the DEMO accounts" in p and "REAL-1" in p for p in demo_gate.check_export(good))


def test_real_news_link_fails(good):
    _write(good, "fact_news", ["article_id", "url", "is_demo"], [["a1", "https://finnhub.io/x", "1"]])
    assert any("finnhub.io" in p for p in demo_gate.check_export(good))


def test_forbidden_column_fails(good):
    _write(good, "dim_connection", ["connection_id", "provider_ref"], [["c1", "uuid"]])
    assert any("provider_ref" in p for p in demo_gate.check_export(good))


def test_empty_folder_fails(tmp_path):
    assert demo_gate.check_export(tmp_path) == [f"No CSV files in {tmp_path}"]
    assert demo_gate.main([str(tmp_path)]) == 1


def test_demo_account_with_demo_connection_passes(good):
    _write(good, "dim_account", ["account_id", "display_name", "is_demo", "connection_id"],
           [["DEMO-TFSA", "Demo TFSA", "1", "DEMO-CONN"]])
    assert demo_gate.check_export(good) == []