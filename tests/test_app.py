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

    def fake_extract(path, media_type, accounts, uploaded_by=None, **kw):
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
    # quantity known but no ticker: the quantity is kept, the price derived from the stated value
    assert by["Unknown fund"]["symbol"] is None
    assert by["Unknown fund"]["quantity"] == 3 and by["Unknown fund"]["manual_price"] == pytest.approx(100)


def test_failed_extraction_is_kept_for_retry(client, monkeypatch):
    def boom(path, media_type, accounts, uploaded_by=None, **kw):
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


def test_portfolio_lists_positions_with_share_and_day_change(client):
    depot = make_account(client, name="Neon Invest", kind="depot", valuation="holdings", owner_id=client.user["id"])
    client.put(f"/api/accounts/{depot['id']}/holdings", json=[
        {"name": "Vanguard Total World", "symbol": "VT", "quantity": 10, "currency": "USD"},
        {"name": "Cash", "quantity": 200, "manual_price": 1, "currency": "CHF"},
    ])
    yesterday = (date.today() - timedelta(days=1)).isoformat()
    store_price("VT", yesterday, 100.0, "USD")
    store_price("VT", TODAY, 110.0, "USD")
    store_price("USDCHF=X", TODAY, 0.8, "CHF")
    p = client.get("/api/portfolio").json()
    assert p["total"] == pytest.approx(880 + 200)
    vt, cash = p["positions"]  # largest first
    assert vt["name"] == "Vanguard Total World" and vt["price"] == 110 and vt["day_change"] == pytest.approx(0.1)
    assert vt["share"] == pytest.approx(880 / 1080) and vt["day_change_base"] == pytest.approx(80)
    assert cash["day_change"] is None and p["day_change_base"] == pytest.approx(80)
    assert client.get("/api/portfolio?owner=joint").json()["positions"] == []


def test_position_with_only_a_value_has_no_quantity(client):
    depot = make_account(client, name="Neon Invest", kind="depot", valuation="holdings")
    client.post("/api/upload", files={"file": ("neon.png", b"\x89PNG", "image/png")})
    upload_id = client.get("/api/uploads").json()[0]["id"]
    client.post(f"/api/uploads/{upload_id}/apply", json={"as_of": TODAY, "items": [{
        "account_id": depot["id"], "replace_holdings": True,
        "positions": [{"name": "AI & Big Data", "value": 19298.45, "currency": "CHF"}]}]})
    h = client.get(f"/api/accounts/{depot['id']}/holdings").json()[0]
    assert h["quantity"] is None and h["value_base"] == pytest.approx(19298.45)
    p = client.get("/api/portfolio").json()["positions"][0]
    assert p["quantity"] is None and p["price"] is None and p["value_only"] is True
    # the editor saves it back the same way
    r = client.put(f"/api/accounts/{depot['id']}/holdings", json=[{"name": "AI & Big Data", "quantity": None, "manual_price": 19000}])
    assert r.status_code == 200
    assert client.get("/api/portfolio").json()["positions"][0]["value_only"] is True
    r = client.put(f"/api/accounts/{depot['id']}/holdings", json=[{"name": "X", "symbol": "VT", "quantity": None}])
    assert r.status_code == 422


def test_existing_value_positions_are_marked_on_upgrade(tmp_path, monkeypatch):
    import sqlite3
    from app import db
    path = tmp_path / "old.db"
    monkeypatch.setenv("FINANCE_DB", str(path))
    conn = sqlite3.connect(path)
    conn.executescript(db.SCHEMA)  # the tables as they were, without value_only
    conn.execute("INSERT INTO accounts (name, institution, kind, valuation) VALUES ('Neon Invest', 'Neon', 'depot', 'holdings')")
    conn.executemany("INSERT INTO holdings (account_id, name, symbol, quantity, manual_price, currency) VALUES (1, ?, ?, ?, ?, 'CHF')",
                     [("AI & Big Data", None, 1, 19298.45), ("Cash", None, 420, 1), ("VT", "VT", 10, None)])
    conn.commit()
    conn.close()
    db.init()
    with db.connect() as c:
        assert {r["name"]: r["value_only"] for r in db.rows(c, "SELECT * FROM holdings")} == {
            "AI & Big Data": 1, "Cash": 0, "VT": 0}


def test_quantity_from_value_and_price():
    from app.match import derive_quantity
    assert derive_quantity(16605.28, 123.92) == 134
    assert derive_quantity(20.30, 10.15) == 2
    assert derive_quantity(100.0, 8.0) == 12.5  # fractional shares stay fractional
    assert derive_quantity(None, 10) is None and derive_quantity(10, 0) is None


def test_ticker_is_used_only_when_its_price_matches(monkeypatch):
    from app import match
    quotes = {"TSFA.F": ("EUR", 364.5), "TSM": ("USD", 404.8), "USDEUR=X": ("EUR", 0.89), "WRONG": ("EUR", 50.0)}
    monkeypatch.setattr(prices, "fetch", lambda s, range_="1mo", client=None: (quotes[s][0], [("2026-11-04", quotes[s][1])])
                        if s in quotes else (_ for _ in ()).throw(prices.PriceError("unknown")))
    searches = {"Taiwan Semiconductor": [{"symbol": "WRONG"}, {"symbol": "TSFA.F"}]}
    monkeypatch.setattr(prices, "search", lambda q, client=None: searches.get(q, []))
    p = {"name": "TAIWAN SEMICON.MANU.ADR/5", "price": 364.0, "value": 3276.0, "currency": "EUR",
         "ticker_hints": ["Taiwan Semiconductor"]}
    out = match.resolve_all({"accounts": [{"positions": [p, {**p, "ticker_hints": ["TSM"]}, {**p, "ticker_hints": []}]}]})
    found, via_usd, none = out["accounts"][0]["positions"]
    assert found["quantity"] == 9 and found["symbol"] == "TSFA.F" and found["match"]["price"] == 364.5
    assert via_usd["symbol"] == "TSM" and via_usd["match"]["quote_currency"] == "USD"  # 404.8 USD = 360.27 EUR
    assert none["match"] is None and none.get("symbol") is None and none["quantity"] == 9


def test_screenshot_positions_update_without_removing_others(client):
    depot = make_account(client, name="Neon Invest", kind="depot", valuation="holdings")
    client.put(f"/api/accounts/{depot['id']}/holdings", json=[
        {"name": "X(IE)-MSCI WORLD 1C", "quantity": None, "manual_price": 15000, "currency": "EUR"},
        {"name": "Gold (ZKB)", "quantity": None, "manual_price": 9577.60, "currency": "CHF"},
    ])
    client.post("/api/upload", files={"file": ("neon.png", b"\x89PNG", "image/png")})
    upload_id = client.get("/api/uploads").json()[0]["id"]
    apply = lambda **kw: client.post(f"/api/uploads/{upload_id}/apply", json={"as_of": TODAY, "items": [{
        "account_id": depot["id"], "replace_holdings": True, **kw,
        "positions": [{"name": "X(IE)-MSCI WORLD 1C", "symbol": "XDWD.DE", "quantity": 110, "value": 15540.80, "currency": "EUR"}]}]})
    assert apply().status_code == 200
    by = {h["name"]: h for h in client.get(f"/api/accounts/{depot['id']}/holdings").json()}
    assert set(by) == {"X(IE)-MSCI WORLD 1C", "Gold (ZKB)"}  # gold isn't on the screenshot and stays
    assert by["X(IE)-MSCI WORLD 1C"]["symbol"] == "XDWD.DE" and by["X(IE)-MSCI WORLD 1C"]["quantity"] == 110
    assert apply(remove_missing=True).status_code == 200
    assert [h["name"] for h in client.get(f"/api/accounts/{depot['id']}/holdings").json()] == ["X(IE)-MSCI WORLD 1C"]


def test_quote_endpoint(client, monkeypatch):
    monkeypatch.setattr(prices, "quote", lambda symbol, range_: {"symbol": symbol, "range": range_, "points": []})
    assert client.get("/api/quote/xdwd.de?range=5y").json() == {"symbol": "XDWD.DE", "range": "5y", "points": []}
    assert client.get("/api/quote/VT?range=2w").status_code == 422
    assert client.get("/api/quote/a%20b").status_code == 422


def test_same_name_handles_truncation_and_spelling():
    from app.match import same_name
    assert same_name("Nasdaq 100 (Xtrac...", "Nasdaq 100 (Xtrackers)")
    assert same_name("Eurozone Stocks (Swissc…", "Eurozone Stocks (Swisscanto)")
    assert same_name("TAIWAN SEMICON.MANU.ADR/5", "Taiwan Semicon. Manu. ADR/5")
    assert same_name("X(IE)-MSCI WORLD 1C", "X(IE)-MSCI WORLD 1C ")
    assert not same_name("X(IE)-MSCI WORLD 1C", "X(IE)-MSCI WO.IN.TE. 1CDL")
    assert not same_name("S&P 500 (Xtracker...", "Nasdaq 100 (Xtrackers)")
    assert not same_name("Gold", "Gold (ZKB)")  # untruncated prefix of a different name


def test_upload_updates_truncated_names_instead_of_duplicating(client):
    depot = make_account(client, name="Neon Invest", kind="depot", valuation="holdings")
    other = make_account(client, name="Other depot", kind="depot", valuation="holdings")
    client.put(f"/api/accounts/{depot['id']}/holdings", json=[
        {"name": "Nasdaq 100 (Xtrac...", "quantity": None, "manual_price": 16899.48, "currency": "CHF"},
        {"name": "Gold (ZKB)", "quantity": None, "manual_price": 9577.60, "currency": "CHF"},
    ])
    client.put(f"/api/accounts/{other['id']}/holdings", json=[{"name": "Foreign", "quantity": 1, "manual_price": 5}])
    foreign_id = client.get(f"/api/accounts/{other['id']}/holdings").json()[0]["id"]
    names = lambda: sorted(h["name"] for h in client.get(f"/api/accounts/{depot['id']}/holdings").json())

    def apply(positions, **kw):
        client.post("/api/upload", files={"file": ("neon.png", b"\x89PNG", "image/png")})
        upload_id = client.get("/api/uploads").json()[0]["id"]
        r = client.post(f"/api/uploads/{upload_id}/apply", json={"as_of": TODAY, "items": [{
            "account_id": depot["id"], "replace_holdings": True, "positions": positions, **kw}]})
        assert r.status_code == 200, r.text

    # automatic matching (no review): the truncated name is recognised
    apply([{"name": "Nasdaq 100 (Xtrackers)", "quantity": None, "value": 17000, "currency": "CHF"}])
    assert names() == ["Gold (ZKB)", "Nasdaq 100 (Xtrackers)"]
    # reviewed: the user's choice wins, even for a name that doesn't look alike
    gold_id = next(h["id"] for h in client.get(f"/api/accounts/{depot['id']}/holdings").json() if h["name"] == "Gold (ZKB)")
    apply([{"name": "ZKB Gold ETF AA CHF", "value": 9600, "currency": "CHF", "replaces_id": gold_id}], positions_reviewed=True)
    assert names() == ["Nasdaq 100 (Xtrackers)", "ZKB Gold ETF AA CHF"]
    # reviewed as new: added, nothing replaced; a holding of another account is never touched
    apply([{"name": "Nasdaq 100 (Xtrackers)", "value": 1, "currency": "CHF", "replaces_id": foreign_id}], positions_reviewed=True)
    assert names() == ["Nasdaq 100 (Xtrackers)", "Nasdaq 100 (Xtrackers)", "ZKB Gold ETF AA CHF"]
    assert len(client.get(f"/api/accounts/{other['id']}/holdings").json()) == 1


# --- pillar 2/3a reminders and the weekly briefing -------------------------------------------------

def _tasks(client):
    return {t["key"]: t for t in client.get("/api/tasks").json()["open"]}


def test_3a_reminders_follow_payments_and_calendar(client):
    from app import advice
    uid = client.user["id"]
    three_a = make_account(client, name="VIAC 3a", institution="VIAC", kind="pillar3a", owner_id=uid)
    advice.sync_tasks(date(2026, 1, 5))
    t = _tasks(client)[f"3a-pay-2026-{uid}"]
    assert "CHF 7'258 can still be paid in" in t["body"]
    client.post(f"/api/accounts/{three_a['id']}/contributions", json={"day": "2026-03-01", "amount": 3000})
    advice.sync_tasks(date(2026, 3, 2))
    assert "CHF 4'258 can still" in _tasks(client)[f"3a-pay-2026-{uid}"]["body"]
    advice.sync_tasks(date(2026, 11, 20))
    assert f"3a-deadline-2026-{uid}" in _tasks(client)
    client.post(f"/api/accounts/{three_a['id']}/contributions", json={"day": "2026-11-21", "amount": 4258})
    advice.sync_tasks(date(2026, 11, 22))
    assert not any(k.startswith("3a-") for k in _tasks(client))  # paid in full: closed
    # the next year: last year's payments complete, so no catch-up; a new pay reminder
    advice.sync_tasks(date(2027, 1, 3))
    assert set(k for k in _tasks(client) if k.startswith("3a-")) == {f"3a-pay-2027-{uid}"}


def test_3a_catch_up_and_statement_totals(client):
    from app import advice
    uid = client.user["id"]
    three_a = make_account(client, name="VIAC 3a", institution="VIAC", kind="pillar3a", owner_id=uid)
    client.post("/api/upload", files={"file": ("viac.png", b"\x89PNG", "image/png")})
    upload_id = client.get("/api/uploads").json()[0]["id"]
    r = client.post(f"/api/uploads/{upload_id}/apply", json={"as_of": "2025-12-31", "items": [
        {"account_id": three_a["id"], "balance": 30000, "contributions_ytd": 5000}]})
    assert r.status_code == 200
    advice.sync_tasks(date(2026, 2, 1))
    t = _tasks(client)[f"3a-catchup-2025-{uid}"]
    assert "gap of CHF 2'258" in t["body"]


def test_pillar2_certificate_then_buyin_reminder(client):
    from app import advice
    uid = client.user["id"]
    p2 = make_account(client, name="AXA BVG", institution="AXA", kind="pillar2", owner_id=uid)
    advice.sync_tasks(date(2026, 9, 10))
    assert f"p2-certificate-2026-{uid}" in _tasks(client)
    client.post("/api/upload", files={"file": ("axa.pdf", b"%PDF", "application/pdf")})
    upload_id = client.get("/api/uploads").json()[0]["id"]
    client.post(f"/api/uploads/{upload_id}/apply", json={"as_of": "2026-01-01", "items": [
        {"account_id": p2["id"], "balance": 120000, "pillar2_buyin": 85000}]})
    advice.sync_tasks(date(2026, 9, 10))
    t = _tasks(client)
    assert f"p2-certificate-2026-{uid}" not in t and "CHF 85'000" in t[f"p2-buyin-2026-{uid}"]["body"]
    assert client.get("/api/pension").json()[0]["pillar2_buyin"] == 85000


def test_task_snooze_done_and_dismiss(client):
    from app import advice
    uid = client.user["id"]
    make_account(client, name="VIAC 3a", institution="VIAC", kind="pillar3a", owner_id=uid)
    advice.sync_tasks(date(2026, 1, 5))
    task = _tasks(client)[f"3a-pay-2026-{uid}"]
    advice.set_status(task["id"], "snooze", 91, today=date(2026, 1, 5))
    assert client.get("/api/tasks").json()["snoozed"][0]["snooze_until"] == "2026-04-06"
    advice.sync_tasks(date(2026, 3, 1))
    assert f"3a-pay-2026-{uid}" not in _tasks(client)
    advice.sync_tasks(date(2026, 4, 6))
    assert f"3a-pay-2026-{uid}" in _tasks(client)  # back on its date
    assert client.post(f"/api/tasks/{task['id']}", json={"action": "dismissed"}).status_code == 200
    advice.sync_tasks(date(2026, 5, 1))
    assert f"3a-pay-2026-{uid}" not in _tasks(client)  # stays dismissed


def test_profile_update(client):
    uid = client.user["id"]
    r = client.patch(f"/api/users/{uid}", json={"birth_year": 1988, "has_pension_fund": False})
    assert r.status_code == 200
    p = client.get("/api/pension").json()[0]
    assert p["birth_year"] == 1988 and p["has_pension_fund"] == 0 and p["max_3a"] is None
    assert client.patch(f"/api/users/{uid}", json={"birth_year": 1800}).status_code == 422


def test_briefing_is_stored_with_tasks(client, monkeypatch):
    from app import briefing
    depot = make_account(client, name="Neon Invest", kind="depot", valuation="holdings")
    client.put(f"/api/accounts/{depot['id']}/holdings", json=[{"name": "MSCI World", "symbol": "XDWD.DE", "quantity": 10, "currency": "EUR"}])
    store_price("XDWD.DE", TODAY, 140.0, "EUR")
    store_price("EURCHF=X", TODAY, 0.93, "CHF")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test")
    seen = {}
    monkeypatch.setattr(briefing, "_research", lambda client_, data: (seen.setdefault("data", data), ("memo", [{"url": "https://x", "title": "X"}]))[1])
    monkeypatch.setattr(briefing, "_write", lambda client_, data, memo: briefing.Briefing(
        headline="Calm week", summary="Little changed.", market=[briefing.Note(title="Rates", detail="Unchanged.")],
        positions=[briefing.PositionView(name="MSCI World", symbol="XDWD.DE", stance="hold", reasoning="Core holding.")],
        considerations=[], tasks=[briefing.SuggestedTask(key="Check overlap!", title="Check ETF overlap", body="...")]))
    assert briefing.due()
    briefing.generate(client=object())
    assert not briefing.due()
    assert seen["data"]["positions"][0]["value_chf"] == round(10 * 140 * 0.93)
    b = client.get("/api/briefing").json()
    assert b["briefing"]["headline"] == "Calm week" and b["sources"][0]["url"] == "https://x" and not b["running"]
    assert "b-check-overlap" in _tasks(client)


def test_briefing_failure_is_recorded(client, monkeypatch):
    from app import briefing
    monkeypatch.setenv("ANTHROPIC_API_KEY", "")
    with pytest.raises(briefing.BriefingError):
        briefing.generate()
    b = client.get("/api/briefing").json()
    assert "API key" in b["last_error"] and not b["running"]


def test_usage_is_recorded_with_cost(client):
    from types import SimpleNamespace
    from app import usage
    usage.record("extract", "claude-haiku-5-5", SimpleNamespace(input_tokens=5000, output_tokens=1000))
    usage.record("research", "claude-sonnet-5-5", SimpleNamespace(input_tokens=60000, output_tokens=5000,
                                                                server_tool_use=SimpleNamespace(web_search_requests=15)))
    usage.record("research", "claude-haiku-5-5", SimpleNamespace(input_tokens=150000, output_tokens=10000))
    u = client.get("/api/usage").json()
    by = {(r["task"], r["model"]): r for r in u["by_task"]}
    assert by[("extract", "claude-haiku-5-5")]["cost_usd"] == pytest.approx(0.001)
    assert by[("research", "claude-sonnet-5-5")]["cost_usd"] == pytest.approx(0.12 + 0.05 + 0.15)
    assert by[("research", "claude-haiku-5-5")]["cost_usd"] == pytest.approx(0.075 + 0.025)  # long-prompt price
    assert u["total_usd"] == pytest.approx(0.001 + 0.32 + 0.1)


def test_model_per_task_can_be_overridden(monkeypatch):
    from app import config
    assert config.model_for("extract") == ("claude-haiku-5-5", "medium")
    assert config.model_for("briefing") == ("claude-opus-5-5", "high")
    monkeypatch.setenv("CLAUDE_MODEL_EXTRACT", "claude-sonnet-5-5")
    assert config.model_for("extract")[0] == "claude-sonnet-5-5"


def test_retry_reads_with_the_stronger_model(client, monkeypatch):
    seen = []
    monkeypatch.setattr(extract, "extract", lambda path, media_type, accounts, uploaded_by=None, task="extract":
                        (seen.append(task), extract.Extraction())[1])
    client.post("/api/upload", files={"file": ("s.png", b"\x89PNG", "image/png")})
    upload_id = client.get("/api/uploads").json()[0]["id"]
    client.post(f"/api/uploads/{upload_id}/retry")
    assert seen == ["extract", "extract_retry"]


# --- security -------------------------------------------------------------------------------------

def test_uploads_of_web_pages_and_scripts_are_refused(client):
    for name, data, mime in [("evil.html", b"<script>alert(1)</script>", "text/html"),
                             ("evil.svg", b"<svg><script>alert(1)</script></svg>", "image/svg+xml"),
                             ("evil.js", b"alert(1)", "application/javascript"),
                             ("evil", b"<script>", "text/html")]:
        assert client.post("/api/upload", files={"file": (name, data, mime)}).status_code == 415, name


def test_uploaded_files_are_served_sandboxed(client):
    client.post("/api/upload", files={"file": ("shot.png", b"\x89PNG", "image/png")})
    client.post("/api/upload", files={"file": ("dkb.csv", b"<script>alert(1)</script>", "text/csv")})
    png, csv = sorted(client.get("/api/uploads").json(), key=lambda u: u["id"])
    r = client.get(f"/api/uploads/{csv['id']}/file")
    assert r.headers["content-type"].startswith("text/plain") and r.headers["content-security-policy"].startswith("sandbox")
    assert client.get(f"/api/uploads/{png['id']}/file").headers["content-security-policy"].startswith("sandbox")
    # an older upload with a dangerous type is only offered as a download
    from app import db
    with db.connect() as conn:
        conn.execute("UPDATE uploads SET media_type = 'text/html' WHERE id = ?", (png["id"],))
    r = client.get(f"/api/uploads/{png['id']}/file")
    assert r.headers["content-type"] == "application/octet-stream" and r.headers["content-disposition"].startswith("attachment")


def test_upload_paths_outside_the_uploads_folder_are_not_served(client, tmp_path):
    from app import db
    secret = tmp_path / "secret.txt"
    secret.write_text("ANTHROPIC_API_KEY=sk-ant-secret")
    with db.connect() as conn:  # as a crafted database import could have it
        upload_id = conn.execute("INSERT INTO uploads (filename, media_type, path, status) VALUES ('x.txt', 'text/plain', ?, 'pending')",
                                 (str(secret),)).lastrowid
        traversal = conn.execute("INSERT INTO uploads (filename, media_type, path, status) VALUES ('y.txt', 'text/plain', ?, 'pending')",
                                 (str(db.uploads_dir() / ".." / "secret.txt"),)).lastrowid
    assert client.get(f"/api/uploads/{upload_id}/file").status_code == 404
    assert client.get(f"/api/uploads/{traversal}/file").status_code == 404


def test_cross_site_requests_are_refused(client):
    body = {"name": "Evil", "username": "evil", "password": "evil-password-123"}
    assert client.post("/api/users", json=body, headers={"Origin": "https://evil.example"}).status_code == 403
    assert client.post("/api/users", json=body, headers={"Sec-Fetch-Site": "cross-site"}).status_code == 403
    assert client.post("/api/users", json=body, headers={"Origin": "https://testserver"}).status_code == 200  # same host
    assert client.post("/api/users", json={**body, "username": "evil2"}, headers={"Origin": "null"}).status_code == 403
    assert client.get("/api/summary", headers={"Origin": "https://evil.example"}).status_code == 200  # reads are fine


def test_unknown_username_takes_as_long_as_a_wrong_password(client, monkeypatch):
    from app import auth
    calls = []
    real = auth.verify_password
    monkeypatch.setattr(auth, "verify_password", lambda p, h: (calls.append(h), real(p, h))[1])
    client.post("/api/logout")
    client.post("/api/login", json={"username": "nobody", "password": "whatever-1234"})
    client.post("/api/login", json={"username": "dominik", "password": "whatever-1234"})
    assert len(calls) == 2  # the password is hashed in both cases


def test_setup_token_guessing_is_rate_limited(anon, monkeypatch):
    monkeypatch.setenv("SETUP_TOKEN", "server-secret")
    form = {"name": "X", "username": "xy", "password": "long enough password"}
    codes = [anon.post("/api/setup", data={**form, "setup_token": f"guess{i}"}).status_code for i in range(7)]
    assert codes[:5] == [403] * 5 and codes[5:] == [429, 429]
    assert anon.post("/api/setup", data={**form, "setup_token": "server-secret"}).status_code == 429  # even the right one, for now


def test_setup_creates_only_one_first_user(anon):
    from app import auth
    auth.create_user("A", "a", "long enough password", first=True)
    with pytest.raises(Exception):
        auth.create_user("B", "b", "long enough password", first=True)


def test_tickers_must_look_like_tickers(client):
    depot = make_account(client, name="Depot", kind="depot", valuation="holdings")
    r = client.put(f"/api/accounts/{depot['id']}/holdings", json=[{"name": "X", "symbol": "../../v7/x?a=", "quantity": 1}])
    assert r.status_code == 422
    assert client.put(f"/api/accounts/{depot['id']}/holdings", json=[{"name": "X", "symbol": " xdwd.de ", "quantity": 1}]).status_code == 200


def test_claude_spending_is_capped(client, monkeypatch):
    from app import main
    monkeypatch.setattr(main, "MAX_UPLOADS_PER_HOUR", 2)
    monkeypatch.setattr(main, "MAX_BRIEFINGS_PER_DAY", 1)
    for _ in range(2):
        assert client.post("/api/upload", files={"file": ("s.png", b"\x89PNG", "image/png")}).status_code == 200
    assert client.post("/api/upload", files={"file": ("s.png", b"\x89PNG", "image/png")}).status_code == 429
    from app import db
    with db.connect() as conn:
        conn.execute("INSERT INTO briefings (week, status, error) VALUES ('2026-W45', 'failed', 'x')")
    assert client.post("/api/briefing").status_code == 429


def test_briefing_sources_are_web_links_only(monkeypatch):
    from types import SimpleNamespace
    from app import briefing
    block = SimpleNamespace(type="text", text="memo", citations=[SimpleNamespace(url="javascript:alert(1)", title="x"),
                                                                 SimpleNamespace(url="https://ok.example/a", title="ok")])
    response = SimpleNamespace(stop_reason="end_turn", content=[block], usage=None)

    class Stream:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def get_final_message(self): return response
    client = SimpleNamespace(messages=SimpleNamespace(stream=lambda **kw: Stream()))
    memo, sources = briefing._research(client, {"today": "2026-11-04", "positions": []})
    assert sources == [{"url": "https://ok.example/a", "title": "ok"}]


def test_iphone_photos_are_converted_to_jpeg(client):
    import io
    import pillow_heif
    from PIL import Image
    pillow_heif.register_heif_opener()
    buf = io.BytesIO()
    Image.new("RGB", (4032, 3024), (20, 120, 200)).save(buf, format="HEIF")
    r = client.post("/api/upload", files={"file": ("IMG_0042.HEIC", buf.getvalue(), "image/heic")})
    assert r.status_code == 200, r.text
    u = client.get("/api/uploads").json()[0]
    assert u["filename"] == "IMG_0042.jpg" and u["media_type"] == "image/jpeg"
    jpeg = client.get(f"/api/uploads/{u['id']}/file").content
    assert jpeg[:3] == b"\xff\xd8\xff" and max(Image.open(io.BytesIO(jpeg)).size) == 2400
    assert client.post("/api/upload", files={"file": ("broken.heic", b"not a photo", "image/heic")}).status_code == 415


def test_the_signed_shortcut_can_be_downloaded(anon):
    r = anon.get("/FinanceAgent.shortcut")
    assert r.status_code == 200 and r.content[:4] == b"AEA1"  # Apple-signed archive; contains no secrets


def test_shortcut_build_has_the_upload_request():
    import sys
    sys.path.insert(0, "scripts")
    import make_shortcut
    w = make_shortcut.build("https://x.example/api/upload", "fa_token")
    actions = [a["WFWorkflowActionIdentifier"] for a in w["WFWorkflowActions"]]
    assert actions == ["is.workflow.actions.gettext", "is.workflow.actions.gettext", "is.workflow.actions.repeat.each",
                       "is.workflow.actions.downloadurl", "is.workflow.actions.notification", "is.workflow.actions.repeat.each"]
    request = w["WFWorkflowActions"][3]["WFWorkflowActionParameters"]
    assert request["WFHTTPMethod"] == "POST" and request["WFHTTPBodyType"] == "Form"
    assert [q["DefaultValue"] for q in w["WFWorkflowImportQuestions"]] == ["https://x.example/api/upload", "fa_token"]


def test_upload_token_is_accepted_however_it_is_pasted(client):
    token = client.post("/api/me/upload-token").json()["token"]
    client.post("/api/logout")
    png = {"file": ("s.png", b"\x89PNG", "image/png")}
    for header in [f"Bearer {token}", f"bearer {token}\n", token, f'Bearer "{token}"', f"  {token}  "]:
        assert client.post("/api/upload", headers={"Authorization": header}, files=png).status_code == 200, repr(header)
    assert client.post("/api/upload", data={"token": token}, files=png).status_code == 200  # form field
    r = client.post("/api/upload", files=png)
    assert r.status_code == 401 and "No upload token sent" in r.json()["detail"]
    r = client.post("/api/upload", headers={"Authorization": "Bearer fa_old_token_abcd"}, files=png)
    assert r.status_code == 401 and "ends in …abcd" in r.json()["detail"]
    assert client.get("/api/uploads", headers={"Authorization": f"Bearer {token}"}).status_code == 401  # upload only
