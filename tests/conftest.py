import pytest
from fastapi.testclient import TestClient

PASSWORD = "correct horse battery"


@pytest.fixture
def anon(tmp_path, monkeypatch):
    """A client for an app with no logins yet."""
    monkeypatch.setenv("FINANCE_DB", str(tmp_path / "finance.db"))
    monkeypatch.setenv("FINANCE_NO_REFRESH", "1")
    monkeypatch.setenv("SETUP_TOKEN", "")
    monkeypatch.setenv("BASE_CURRENCY", "CHF")
    # No network in tests: price refreshes do nothing and Claude is never called unless a test stubs them.
    from app import auth, extract, prices
    monkeypatch.setattr(prices, "refresh", lambda symbols, currencies, base: [])
    monkeypatch.setattr(extract, "extract", lambda path, media_type, accounts, uploaded_by=None: extract.Extraction())
    auth.reset_login_limits()
    from app.main import app
    with TestClient(app) as c:
        yield c


@pytest.fixture
def client(anon):
    """Signed in as the first family member, Dominik."""
    r = anon.post("/api/setup", data={"name": "Dominik", "username": "dominik", "password": PASSWORD})
    assert r.status_code == 200, r.text
    anon.user = r.json()["user"]
    return anon


def store_price(symbol, day, close, currency):
    from app import prices
    prices.store(symbol, currency, [(day, close)])
