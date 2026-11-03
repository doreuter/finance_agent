import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("FINANCE_DB", str(tmp_path / "finance.db"))
    monkeypatch.setenv("FINANCE_NO_REFRESH", "1")
    monkeypatch.setenv("APP_TOKEN", "")
    monkeypatch.setenv("BASE_CURRENCY", "CHF")
    # No network in tests: price refreshes do nothing unless a test stubs them.
    from app import prices
    monkeypatch.setattr(prices, "refresh", lambda symbols, currencies, base: [])
    from app.main import app
    with TestClient(app) as c:
        yield c


def store_price(symbol, day, close, currency):
    from app import prices
    prices.store(symbol, currency, [(day, close)])
