from datetime import date, timedelta

import pytest

from app import extract, prices
from tests.conftest import store_price

TODAY = date.today().isoformat()


def make_account(client, **kw):
    body = {"name": "Neon", "institution": "Neon", "kind": "cash", "currency": "CHF", "valuation": "balance", **kw}
    r = client.post("/api/accounts", json=body)
    assert r.status_code == 200, r.text
    return r.json()


def test_balance_accounts_convert_to_chf(client):
    neon = make_account(client)
    dkb = make_account(client, name="DKB", institution="DKB", currency="EUR")
    client.post(f"/api/accounts/{neon['id']}/balances", json={"day": TODAY, "amount": 1000})
    client.post(f"/api/accounts/{dkb['id']}/balances", json={"day": TODAY, "amount": 500})
    store_price("EURCHF=X", TODAY, 0.9, "CHF")

    s = client.get("/api/summary").json()
    assert s["total"] == pytest.approx(1450)
    by = {a["name"]: a for a in s["accounts"]}
    assert by["DKB"]["value"] == pytest.approx(500)
    assert by["DKB"]["value_base"] == pytest.approx(450)
    assert {k["kind"]: k["value"] for k in s["by_kind"]}["cash"] == pytest.approx(1450)


def test_missing_fx_rate_is_reported_not_guessed(client):
    dkb = make_account(client, name="DKB", institution="DKB", currency="EUR")
    client.post(f"/api/accounts/{dkb['id']}/balances", json={"day": TODAY, "amount": 500})
    a = client.get("/api/summary").json()["accounts"][0]
    assert a["value_base"] is None
    assert "no EUR/CHF rate yet" in a["problems"]


def test_latest_balance_wins_and_old_balance_is_stale(client):
    viac = make_account(client, name="VIAC", institution="VIAC", kind="pillar3a")
    old = (date.today() - timedelta(days=60)).isoformat()
    client.post(f"/api/accounts/{viac['id']}/balances", json={"day": old, "amount": 20000})
    a = client.get("/api/summary").json()["accounts"][0]
    assert a["value"] == 20000 and a["stale_days"] == 60
    client.post(f"/api/accounts/{viac['id']}/balances", json={"day": TODAY, "amount": 21000})
    a = client.get("/api/summary").json()["accounts"][0]
    assert a["value"] == 21000 and a["stale_days"] is None


def test_holdings_value_uses_prices_and_fx(client):
    depot = make_account(client, name="Neon Invest", kind="depot", valuation="holdings")
    r = client.put(f"/api/accounts/{depot['id']}/holdings", json=[
        {"name": "Vanguard Total World", "symbol": "vt", "quantity": 10, "currency": "USD"},
        {"name": "Cash", "quantity": 250, "manual_price": 1, "currency": "CHF"},
    ])
    assert r.status_code == 200
    store_price("VT", TODAY, 120.0, "USD")
    store_price("USDCHF=X", TODAY, 0.8, "CHF")
    a = client.get("/api/summary").json()["accounts"][0]
    assert a["value_base"] == pytest.approx(10 * 120 * 0.8 + 250)
    holdings = client.get(f"/api/accounts/{depot['id']}/holdings").json()
    assert {h["symbol"] for h in holdings} == {"VT", None}


def test_history_records_daily_values(client):
    neon = make_account(client)
    client.post(f"/api/accounts/{neon['id']}/balances", json={"day": TODAY, "amount": 1234})
    h = client.get("/api/history").json()
    assert h == [{"day": TODAY, "total": 1234, "cash": 1234, "depot": 0, "pillar3a": 0, "pillar2": 0}]


def test_parse_chart_handles_pence_and_gaps():
    payload = {"chart": {"result": [{
        "meta": {"currency": "GBp", "gmtoffset": 0},
        "timestamp": [1793577600, 1793664000],
        "indicators": {"quote": [{"close": [None, 7500.0]}]},
    }]}}
    currency, points = prices.parse_chart(payload)
    assert currency == "GBP"
    assert points == [("2026-11-03", 75.0)]


def test_parse_chart_error():
    with pytest.raises(prices.PriceError):
        prices.parse_chart({"chart": {"result": None, "error": {"description": "No data found"}}})


def test_upload_extract_and_apply(client, monkeypatch):
    viac = make_account(client, name="VIAC 3a", institution="VIAC", kind="pillar3a")
    seen = {}

    def fake_extract(path, media_type, accounts):
        seen["media_type"] = media_type
        seen["accounts"] = [a["name"] for a in accounts]
        return extract.Extraction(account_id=viac["id"], institution="VIAC", as_of=TODAY,
                                  balance=23456.7, currency="CHF", notes="VIAC portfolio overview")

    monkeypatch.setattr(extract, "extract", fake_extract)
    r = client.post("/api/upload", files={"file": ("IMG_0001.PNG", b"\x89PNG fake", "image/png")})
    assert r.status_code == 200
    upload_id = r.json()["id"]
    assert seen == {"media_type": "image/png", "accounts": ["VIAC 3a"]}

    pending = client.get("/api/uploads").json()
    assert pending[0]["status"] == "pending"
    assert pending[0]["extracted"]["balance"] == 23456.7

    r = client.post(f"/api/uploads/{upload_id}/apply", json={"account_id": viac["id"], "as_of": TODAY, "balance": 23456.7})
    assert r.status_code == 200
    assert client.get("/api/uploads").json() == []
    assert client.get("/api/summary").json()["total"] == pytest.approx(23456.7)
    assert client.get(f"/api/uploads/{upload_id}/file").content == b"\x89PNG fake"


def test_apply_positions_looks_up_tickers_by_isin(client, monkeypatch):
    depot = make_account(client, name="Neon Invest", kind="depot", valuation="holdings")
    monkeypatch.setattr(prices, "search", lambda q: [{"symbol": "VWRL.SW"}] if q == "IE00B3RBWM25" else [])
    client.post("/api/upload", files={"file": ("x.csv", b"a;b", "text/csv")})
    upload_id = client.get("/api/uploads").json()[0]["id"]
    r = client.post(f"/api/uploads/{upload_id}/apply", json={
        "account_id": depot["id"], "as_of": TODAY, "replace_holdings": True,
        "positions": [
            {"name": "Vanguard FTSE All-World", "isin": "IE00B3RBWM25", "quantity": 12},
            {"name": "Unknown fund", "isin": "XX0000000000", "quantity": 3, "value": 300, "currency": "CHF"},
        ]})
    assert r.status_code == 200
    assert any("Unknown fund" in n for n in r.json()["notes"])
    holdings = client.get(f"/api/accounts/{depot['id']}/holdings").json()
    by = {h["name"]: h for h in holdings}
    assert by["Vanguard FTSE All-World"]["symbol"] == "VWRL.SW"
    assert by["Unknown fund"]["symbol"] is None and by["Unknown fund"]["manual_price"] == 300


def test_failed_extraction_is_kept_for_retry(client, monkeypatch):
    def boom(path, media_type, accounts):
        raise extract.ExtractError("Claude API key missing or invalid: set ANTHROPIC_API_KEY in .env")
    monkeypatch.setattr(extract, "extract", boom)
    client.post("/api/upload", files={"file": ("s.pdf", b"%PDF-1.4", "application/pdf")})
    u = client.get("/api/uploads").json()[0]
    assert u["status"] == "failed" and "ANTHROPIC_API_KEY" in u["error"]


def test_content_blocks_by_type(tmp_path):
    csv = tmp_path / "dkb.csv"
    csv.write_bytes('"Kontostand vom 01.11.2026:";"1.234,56 €"\n'.encode("cp1252"))
    blocks = extract.content_blocks(csv, "text/csv")
    assert "1.234,56 €" in blocks[0]["text"]
    png = tmp_path / "a.png"
    png.write_bytes(b"\x89PNG")
    assert extract.content_blocks(png, "image/png")[0]["type"] == "image"
    with pytest.raises(extract.ExtractError):
        extract.content_blocks(png, "image/heic")


def test_token_protects_api(client, monkeypatch):
    monkeypatch.setenv("APP_TOKEN", "s3cret")
    assert client.get("/api/summary").status_code == 401
    assert client.get("/api/summary", headers={"Authorization": "Bearer s3cret"}).status_code == 200
    assert client.post("/api/login", json={"token": "nope"}).status_code == 401
    assert client.post("/api/login", json={"token": "s3cret"}).status_code == 200
    assert client.get("/api/summary").status_code == 200  # cookie set by login
