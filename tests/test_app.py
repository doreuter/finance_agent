from datetime import date, timedelta

import pytest

from app import extract, prices
from tests.conftest import PASSWORD, store_price

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

    def fake_extract(path, media_type, accounts, uploaded_by=None):
        seen["media_type"] = media_type
        seen["accounts"] = [(a["name"], a["owner"]) for a in accounts]
        seen["uploaded_by"] = uploaded_by
        return extract.Extraction(institution="VIAC", as_of=TODAY, notes="VIAC portfolio overview", accounts=[
            extract.FoundAccount(account_id=viac["id"], name="Portfolio 1", kind="pillar3a", balance=23456.7, currency="CHF")])

    monkeypatch.setattr(extract, "extract", fake_extract)
    r = client.post("/api/upload", files={"file": ("IMG_0001.PNG", b"\x89PNG fake", "image/png")})
    assert r.status_code == 200
    upload_id = r.json()["id"]
    assert seen == {"media_type": "image/png", "accounts": [("VIAC 3a", "joint")], "uploaded_by": "Dominik"}

    pending = client.get("/api/uploads").json()
    assert pending[0]["status"] == "pending"
    assert pending[0]["extracted"]["accounts"][0]["balance"] == 23456.7

    r = client.post(f"/api/uploads/{upload_id}/apply", json={
        "as_of": TODAY, "items": [{"account_id": viac["id"], "balance": 23456.7}]})
    assert r.status_code == 200
    assert client.get("/api/uploads").json() == []
    assert client.get("/api/summary").json()["total"] == pytest.approx(23456.7)
    assert client.get(f"/api/uploads/{upload_id}/file").content == b"\x89PNG fake"


def test_bank_overview_books_several_accounts_and_creates_new_ones(client):
    main = make_account(client, name="Main account", institution="Neon")
    client.post("/api/upload", files={"file": ("neon.png", b"\x89PNG", "image/png")})
    upload_id = client.get("/api/uploads").json()[0]["id"]
    r = client.post(f"/api/uploads/{upload_id}/apply", json={"as_of": TODAY, "items": [
        {"account_id": main["id"], "balance": 1200},
        {"new_account": {"name": "Space Ferien", "institution": "Neon", "kind": "cash", "currency": "CHF"}, "balance": 800},
    ]})
    assert r.status_code == 200, r.text
    s = client.get("/api/summary").json()
    assert s["total"] == pytest.approx(2000)
    assert [(a["institution"], a["name"]) for a in s["accounts"]] == [("Neon", "Main account"), ("Neon", "Space Ferien")]


def test_apply_item_needs_exactly_one_target(client):
    client.post("/api/upload", files={"file": ("x.png", b"\x89PNG", "image/png")})
    upload_id = client.get("/api/uploads").json()[0]["id"]
    r = client.post(f"/api/uploads/{upload_id}/apply", json={"as_of": TODAY, "items": [{"balance": 5}]})
    assert r.status_code == 422


def test_old_single_account_extraction_still_shows(client):
    from app import db
    with db.connect() as conn:
        conn.execute("INSERT INTO uploads (filename, media_type, path, status, extracted_json) VALUES (?, ?, ?, 'pending', ?)",
                     ("old.png", "image/png", "/nonexistent", '{"account_id": null, "institution": "DKB", "balance": 5.0, "currency": "EUR"}'))
    found = client.get("/api/uploads").json()[0]["extracted"]["accounts"]
    assert found[0]["balance"] == 5.0 and found[0]["name"] == "DKB"


def test_apply_positions_looks_up_tickers_by_isin(client, monkeypatch):
    depot = make_account(client, name="Neon Invest", kind="depot", valuation="holdings")
    monkeypatch.setattr(prices, "search", lambda q: [{"symbol": "VWRL.SW"}] if q == "IE00B3RBWM25" else [])
    client.post("/api/upload", files={"file": ("x.csv", b"a;b", "text/csv")})
    upload_id = client.get("/api/uploads").json()[0]["id"]
    r = client.post(f"/api/uploads/{upload_id}/apply", json={"as_of": TODAY, "items": [{
        "account_id": depot["id"], "replace_holdings": True,
        "positions": [
            {"name": "Vanguard FTSE All-World", "isin": "IE00B3RBWM25", "quantity": 12},
            {"name": "Unknown fund", "isin": "XX0000000000", "quantity": 3, "value": 300, "currency": "CHF"},
        ]}]})
    assert r.status_code == 200
    assert any("Unknown fund" in n for n in r.json()["notes"])
    holdings = client.get(f"/api/accounts/{depot['id']}/holdings").json()
    by = {h["name"]: h for h in holdings}
    assert by["Vanguard FTSE All-World"]["symbol"] == "VWRL.SW"
    assert by["Unknown fund"]["symbol"] is None and by["Unknown fund"]["manual_price"] == 300


def test_failed_extraction_is_kept_for_retry(client, monkeypatch):
    def boom(path, media_type, accounts, uploaded_by=None):
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


def test_api_needs_login(anon):
    assert anon.get("/api/status").json()["needs_setup"] is True
    assert anon.get("/api/summary").status_code == 401
    anon.post("/api/setup", data={"name": "Dominik", "username": "dominik", "password": PASSWORD})
    assert anon.get("/api/summary").status_code == 200  # session cookie from setup
    anon.post("/api/logout")
    assert anon.get("/api/summary").status_code == 401
    assert anon.post("/api/login", json={"username": "dominik", "password": "wrong password"}).status_code == 401
    assert anon.post("/api/login", json={"username": "Dominik", "password": PASSWORD}).status_code == 200
    assert anon.get("/api/summary").status_code == 200


def test_setup_only_once_and_needs_token_when_set(anon, monkeypatch):
    monkeypatch.setenv("SETUP_TOKEN", "server-secret")
    form = {"name": "Dominik", "username": "dominik", "password": PASSWORD}
    assert anon.post("/api/setup", data={**form, "setup_token": "guess"}).status_code == 403
    assert anon.post("/api/setup", data={**form, "password": "short", "setup_token": "server-secret"}).status_code == 422
    assert anon.post("/api/setup", data={**form, "setup_token": "server-secret"}).status_code == 200
    assert anon.post("/api/setup", data={**form, "username": "other", "setup_token": "server-secret"}).status_code == 409


def test_login_is_rate_limited(client):
    client.post("/api/logout")
    for _ in range(5):
        assert client.post("/api/login", json={"username": "dominik", "password": "nope nope nope"}).status_code == 401
    assert client.post("/api/login", json={"username": "dominik", "password": PASSWORD}).status_code == 429


def test_upload_token_only_works_for_uploads(client):
    token = client.post("/api/me/upload-token").json()["token"]
    client.post("/api/logout")
    headers = {"Authorization": f"Bearer {token}"}
    assert client.get("/api/summary", headers=headers).status_code == 401
    r = client.post("/api/upload", headers=headers, files={"file": ("s.png", b"\x89PNG", "image/png")})
    assert r.status_code == 200
    assert client.post("/api/upload", headers={"Authorization": "Bearer fa_wrong"},
                       files={"file": ("s.png", b"\x89PNG", "image/png")}).status_code == 401


def test_family_member_and_owner_filter(client):
    wife = client.post("/api/users", json={"name": "Anna", "username": "anna", "password": PASSWORD}).json()
    mine = make_account(client, owner_id=client.user["id"])
    hers = make_account(client, owner_id=wife["id"])
    joint = make_account(client, name="Haushalt", owner_id=None)
    for acc, amount in ((mine, 100), (hers, 200), (joint, 50)):
        client.post(f"/api/accounts/{acc['id']}/balances", json={"day": TODAY, "amount": amount})
    total = lambda owner: client.get(f"/api/summary?owner={owner}").json()["total"]
    assert total("all") == 350 and total(client.user["id"]) == 100 and total(wife["id"]) == 200 and total("joint") == 50
    assert client.get(f"/api/history?owner={wife['id']}").json()[0]["total"] == 200
    # make her account joint, then back
    assert client.patch(f"/api/accounts/{hers['id']}", json={"owner_id": None}).json()["owner_id"] is None
    assert total("joint") == 250
    assert client.patch(f"/api/accounts/{hers['id']}", json={"owner_id": 999}).status_code == 422
    # she can sign in and sees everything
    client.post("/api/logout")
    client.post("/api/login", json={"username": "anna", "password": PASSWORD})
    assert total("all") == 350


def test_password_change_signs_out_other_sessions(client):
    from fastapi.testclient import TestClient
    from app.main import app
    other = TestClient(app)
    other.post("/api/login", json={"username": "dominik", "password": PASSWORD})
    assert other.get("/api/summary").status_code == 200
    r = client.post("/api/me/password", json={"current": PASSWORD, "new": "a brand new password"})
    assert r.status_code == 200
    assert other.get("/api/summary").status_code == 401
    assert client.get("/api/summary").status_code == 200


def test_setup_imports_local_database(anon, tmp_path):
    import sqlite3
    local = tmp_path / "local.db"
    conn = sqlite3.connect(local)  # what the local app wrote before there were logins
    conn.executescript("""
        CREATE TABLE accounts (id INTEGER PRIMARY KEY, name TEXT NOT NULL, institution TEXT NOT NULL, kind TEXT NOT NULL,
            currency TEXT NOT NULL DEFAULT 'CHF', valuation TEXT NOT NULL DEFAULT 'balance', archived INTEGER NOT NULL DEFAULT 0,
            created_at TEXT);
        CREATE TABLE balances (id INTEGER PRIMARY KEY, account_id INTEGER, day TEXT, amount REAL, source TEXT, note TEXT, created_at TEXT);
        CREATE TABLE holdings (id INTEGER PRIMARY KEY, account_id INTEGER, name TEXT, symbol TEXT, isin TEXT, quantity REAL,
            manual_price REAL, currency TEXT, updated_at TEXT);
        INSERT INTO accounts (name, institution, kind) VALUES ('Main account', 'Neon', 'cash');
    """)
    conn.execute("INSERT INTO balances (account_id, day, amount, source) VALUES (1, ?, 4321, 'manual')", (TODAY,))
    conn.commit()
    conn.close()
    with open(local, "rb") as f:
        r = anon.post("/api/setup", data={"name": "Dominik", "username": "dominik", "password": PASSWORD},
                      files={"import_db": ("finance.db", f, "application/octet-stream")})
    assert r.status_code == 200, r.text
    s = anon.get("/api/summary").json()
    assert s["total"] == 4321 and s["accounts"][0]["owner_id"] == r.json()["user"]["id"]
    assert anon.get("/api/backup").content[:16] == b"SQLite format 3\x00"


def test_setup_rejects_other_files(anon):
    r = anon.post("/api/setup", data={"name": "D", "username": "dominik", "password": PASSWORD},
                  files={"import_db": ("finance.db", b"not a database at all", "application/octet-stream")})
    assert r.status_code == 422
    assert anon.get("/api/status").json()["needs_setup"] is True


def test_security_headers(client):
    r = client.get("/")
    assert r.headers["x-frame-options"] == "DENY" and "default-src 'self'" in r.headers["content-security-policy"]
