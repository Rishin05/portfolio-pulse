import pytest

from portfolio_pulse import db, news


@pytest.fixture
def conn(tmp_path):
    c = db.connect(tmp_path / "test.db")
    db.init_db(c)
    yield c
    c.close()


@pytest.fixture(autouse=True)
def _no_real_sleep(monkeypatch):
    """News pacing would make tests slow; tests that check pacing replace this themselves."""
    monkeypatch.setattr(news, "_sleep", lambda seconds: None)