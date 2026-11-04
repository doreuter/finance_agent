"""FastAPI app: family logins, accounts, balances, holdings, statement uploads and the net-worth dashboard."""

from __future__ import annotations

import hmac
import shutil
import sqlite3
import tempfile
import json
import logging
import mimetypes
import os
import secrets
import threading
from contextlib import asynccontextmanager
from datetime import date
from pathlib import Path
from typing import Literal, Optional

from fastapi import BackgroundTasks, FastAPI, File, Form, HTTPException, Request, Response, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import auth, config, db, extract, prices, valuation

config.load_dotenv()
logger = logging.getLogger("financeagent")
logging.basicConfig(level=logging.INFO)
STATIC = config.ROOT / "static"
MAX_UPLOAD_BYTES = 20 * 1024 * 1024
SUFFIX_TYPES = {".csv": "text/csv", ".txt": "text/plain", ".pdf": "application/pdf",
                ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".webp": "image/webp"}


def _refresh_loop(stop: threading.Event) -> None:
    while not stop.is_set():
        try:
            errors = valuation.refresh()
            if errors:
                logger.warning("refresh finished with %d price errors", len(errors))
        except Exception:  # keep the loop alive; the next run may succeed
            logger.exception("refresh failed")
        stop.wait(config.refresh_hours() * 3600)


@asynccontextmanager
async def lifespan(app: FastAPI):
    db.init()
    stop = threading.Event()
    if not os.environ.get("FINANCE_NO_REFRESH"):
        threading.Thread(target=_refresh_loop, args=(stop,), daemon=True).start()
    yield
    stop.set()


app = FastAPI(title="FinanceAgent", lifespan=lifespan)


OPEN_PATHS = {"/api/status", "/api/login", "/api/setup", "/healthz"}
SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "same-origin",
    # Everything comes from our own origin; inline style attributes are used for chart widths and colours.
    "Content-Security-Policy": "default-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; "
    "object-src 'none'; base-uri 'none'; frame-ancestors 'none'; form-action 'self'",
    "Permissions-Policy": "geolocation=(), microphone=(), camera=(), payment=(), usb=()",
}


@app.middleware("http")
async def require_login(request: Request, call_next):
    """Every API call needs a signed-in family member: the session cookie (browser), or a personal
    upload token as bearer header, which only works for uploads (iPhone Shortcut)."""
    path = request.url.path
    request.state.user = None
    if path.startswith("/api/") and path not in OPEN_PATHS:
        user = auth.user_for_session(request.cookies.get(auth.SESSION_COOKIE, ""))
        header = request.headers.get("authorization", "")
        if user is None and header.lower().startswith("bearer ") and path == "/api/upload" and request.method == "POST":
            user = auth.user_for_upload_token(header[7:].strip())
        if user is None:
            return JSONResponse({"detail": "login required"}, status_code=401)
        request.state.user = user
    response = await call_next(request)
    response.headers.update(SECURITY_HEADERS)
    if request.url.scheme == "https":
        response.headers["Strict-Transport-Security"] = "max-age=31536000"
    if not path.startswith("/api/"):
        response.headers["Cache-Control"] = "no-cache"  # revalidate, so an updated app.js is picked up
    return response


def _user(request: Request) -> dict:
    return request.state.user


@app.get("/healthz")
def healthz():
    return {"ok": True}


# --- login & family ----------------------------------------------------------

def _set_session_cookie(response: Response, request: Request, token: str) -> None:
    response.set_cookie(auth.SESSION_COOKIE, token, httponly=True, samesite="lax", secure=request.url.scheme == "https",
                        max_age=auth.SESSION_DAYS * 86400)


@app.get("/api/status")
def status():
    return {"needs_setup": not auth.has_users(), "setup_token_required": bool(config.setup_token())}


def _check_password(password: str) -> None:
    if len(password) < auth.MIN_PASSWORD:
        raise HTTPException(422, f"Password needs at least {auth.MIN_PASSWORD} characters")


def _import_database(upload: UploadFile) -> None:
    """Replace the (empty) database with one from the local app, e.g. when moving to the server."""
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as tmp:
        shutil.copyfileobj(upload.file, tmp)
        tmp_path = Path(tmp.name)
    try:
        try:
            src = sqlite3.connect(f"file:{tmp_path}?mode=ro", uri=True)
            tables = {r[0] for r in src.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
            ok = src.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        except sqlite3.DatabaseError:
            raise HTTPException(422, "That file isn't a FinanceAgent database (data/finance.db)")
        if not ok or not {"accounts", "balances", "holdings"} <= tables:
            src.close()
            raise HTTPException(422, "That file isn't a FinanceAgent database (data/finance.db)")
        if "users" in tables and src.execute("SELECT COUNT(*) FROM users").fetchone()[0]:
            src.close()
            raise HTTPException(422, "That database already has logins; sign in with those instead")
        dest = sqlite3.connect(db.db_path())
        src.backup(dest)
        dest.close()
        src.close()
    finally:
        tmp_path.unlink(missing_ok=True)
    db.init()  # add tables and columns the older file doesn't have yet


@app.post("/api/setup")
def setup(request: Request, name: str = Form(..., min_length=1, max_length=60),
          username: str = Form(..., min_length=2, max_length=40), password: str = Form(...),
          setup_token: str = Form(""), import_db: Optional[UploadFile] = File(None)):
    """Create the first login. Only possible while the app has no users."""
    if auth.has_users():
        raise HTTPException(409, "The app is already set up; sign in instead")
    expected = config.setup_token()
    if expected and not hmac.compare_digest(setup_token, expected):
        auth.record_login_failure(request, "setup")
        raise HTTPException(403, "Wrong setup token")
    auth.check_login_allowed(request, "setup")
    _check_password(password)
    if import_db is not None and import_db.filename:
        _import_database(import_db)
    user = auth.create_user(name.strip(), username.strip(), password)
    with db.connect() as conn:  # accounts made before there were logins belong to the first user
        conn.execute("UPDATE accounts SET owner_id = ? WHERE owner_id IS NULL", (user["id"],))
    response = JSONResponse({"user": user})
    _set_session_cookie(response, request, auth.start_session(user["id"]))
    return response


class Login(BaseModel):
    username: str = Field(max_length=40)
    password: str = Field(max_length=200)


@app.post("/api/login")
def login(body: Login, request: Request):
    auth.check_login_allowed(request, body.username)
    with db.connect() as conn:
        user = db.row(conn, "SELECT * FROM users WHERE username = ?", body.username.strip())
    if not user or not auth.verify_password(body.password, user["password_hash"]):
        auth.record_login_failure(request, body.username)
        raise HTTPException(401, "Wrong username or password")
    response = JSONResponse({"user": auth.public_user(user)})
    _set_session_cookie(response, request, auth.start_session(user["id"]))
    return response


@app.post("/api/logout")
def logout(request: Request):
    auth.end_session(request.cookies.get(auth.SESSION_COOKIE, ""))
    response = JSONResponse({"ok": True})
    response.delete_cookie(auth.SESSION_COOKIE)
    return response


@app.get("/api/me")
def me(request: Request):
    with db.connect() as conn:
        users = db.rows(conn, "SELECT * FROM users ORDER BY id")
    return {"user": auth.public_user(_user(request)), "family": [auth.public_user(u) for u in users]}


class NewUser(BaseModel):
    name: str = Field(min_length=1, max_length=60)
    username: str = Field(min_length=2, max_length=40)
    password: str = Field(max_length=200)


@app.post("/api/users")
def add_family_member(body: NewUser):
    """Any family member can add another one; they then sign in with this username and password."""
    _check_password(body.password)
    return auth.create_user(body.name.strip(), body.username.strip(), body.password)


class PasswordChange(BaseModel):
    current: str = Field(max_length=200)
    new: str = Field(max_length=200)


@app.post("/api/me/password")
def change_password(body: PasswordChange, request: Request):
    user = _user(request)
    if not auth.verify_password(body.current, user["password_hash"]):
        raise HTTPException(403, "Current password is wrong")
    _check_password(body.new)
    with db.connect() as conn:
        conn.execute("UPDATE users SET password_hash = ? WHERE id = ?", (auth.hash_password(body.new), user["id"]))
    auth.end_other_sessions(user["id"], request.cookies.get(auth.SESSION_COOKIE, ""))
    return {"ok": True}


@app.post("/api/me/upload-token")
def upload_token(request: Request):
    """A new personal token for the iPhone Shortcut; it is shown once, and the old one stops working."""
    return {"token": auth.new_upload_token(_user(request)["id"])}


@app.get("/api/backup")
def backup(background: BackgroundTasks):
    """A consistent copy of the database (SQLite online backup), safe while the app runs."""
    fd, name = tempfile.mkstemp(prefix="financeagent-", suffix=".db")
    os.close(fd)
    src, dest = sqlite3.connect(db.db_path()), sqlite3.connect(name)
    src.backup(dest)
    dest.close()
    src.close()
    background.add_task(os.unlink, name)
    return FileResponse(name, media_type="application/vnd.sqlite3",
                        filename=f"financeagent-{date.today().isoformat()}.db")


# --- dashboard ---------------------------------------------------------------

# owner: "all" (family), "joint", or a user id
@app.get("/api/summary")
def summary(owner: str = "all"):
    return valuation.summary(owner)


@app.get("/api/history")
def history(owner: str = "all"):
    return valuation.history(owner)


@app.post("/api/refresh")
def refresh():
    return {"errors": valuation.refresh()}


# --- accounts ----------------------------------------------------------------

Kind = Literal["cash", "depot", "pillar3a", "pillar2"]


class AccountIn(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    institution: str = Field(min_length=1, max_length=80)
    kind: Kind
    currency: str = Field("CHF", pattern=r"^[A-Z]{3}$")
    valuation: Literal["balance", "holdings"] = "balance"
    owner_id: Optional[int] = None  # a family member; None = joint account


class AccountPatch(BaseModel):
    name: Optional[str] = Field(None, min_length=1, max_length=80)
    institution: Optional[str] = Field(None, min_length=1, max_length=80)
    kind: Optional[Kind] = None
    currency: Optional[str] = Field(None, pattern=r"^[A-Z]{3}$")
    valuation: Optional[Literal["balance", "holdings"]] = None
    archived: Optional[bool] = None
    owner_id: Optional[int] = None  # send null explicitly to make it a joint account


def _check_owner(conn, owner_id: int | None) -> None:
    if owner_id is not None and not db.row(conn, "SELECT 1 FROM users WHERE id = ?", owner_id):
        raise HTTPException(422, "unknown family member")


def _account(conn, account_id: int) -> dict:
    a = db.row(conn, "SELECT * FROM accounts WHERE id = ?", account_id)
    if not a:
        raise HTTPException(404, "account not found")
    return a


@app.get("/api/accounts")
def list_accounts():
    with db.connect() as conn:
        return db.rows(conn, "SELECT * FROM accounts WHERE archived = 0 ORDER BY institution, kind, name")


@app.post("/api/accounts")
def create_account(body: AccountIn):
    with db.connect() as conn:
        return _account(conn, _insert_account(conn, body))


def _insert_account(conn, body: AccountIn) -> int:
    _check_owner(conn, body.owner_id)
    cur = conn.execute(
        "INSERT INTO accounts (name, institution, kind, currency, valuation, owner_id) VALUES (?, ?, ?, ?, ?, ?)",
        (body.name, body.institution, body.kind, body.currency, body.valuation, body.owner_id),
    )
    return cur.lastrowid


@app.patch("/api/accounts/{account_id}")
def update_account(account_id: int, body: AccountPatch):
    changes = body.model_dump(exclude_unset=True)
    nullable = {"owner_id"}
    if any(v is None for k, v in changes.items() if k not in nullable):
        raise HTTPException(422, "only owner_id can be null")
    with db.connect() as conn:
        _account(conn, account_id)
        if "owner_id" in changes:
            _check_owner(conn, changes["owner_id"])
        for field, value in changes.items():
            conn.execute(f"UPDATE accounts SET {field} = ? WHERE id = ?", (value, account_id))
        return _account(conn, account_id)


# --- balances ----------------------------------------------------------------

class BalanceIn(BaseModel):
    day: date
    amount: float
    note: Optional[str] = Field(None, max_length=200)


def _add_balance(conn, account_id: int, day: str, amount: float, source: str, note: str | None) -> int:
    cur = conn.execute(
        "INSERT INTO balances (account_id, day, amount, source, note) VALUES (?, ?, ?, ?, ?)",
        (account_id, day, amount, source, note),
    )
    return cur.lastrowid


@app.get("/api/accounts/{account_id}/balances")
def list_balances(account_id: int):
    with db.connect() as conn:
        _account(conn, account_id)
        return db.rows(conn, "SELECT * FROM balances WHERE account_id = ? ORDER BY day DESC, id DESC", account_id)


@app.post("/api/accounts/{account_id}/balances")
def add_balance(account_id: int, body: BalanceIn):
    with db.connect() as conn:
        _account(conn, account_id)
        _add_balance(conn, account_id, body.day.isoformat(), body.amount, "manual", body.note)
    valuation.record_today()
    return {"ok": True}


@app.delete("/api/balances/{balance_id}")
def delete_balance(balance_id: int):
    with db.connect() as conn:
        conn.execute("DELETE FROM balances WHERE id = ?", (balance_id,))
    valuation.record_today()
    return {"ok": True}


# --- holdings ----------------------------------------------------------------

class HoldingIn(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    symbol: Optional[str] = Field(None, max_length=30)
    isin: Optional[str] = Field(None, max_length=12)
    quantity: float
    manual_price: Optional[float] = None
    currency: str = Field("CHF", pattern=r"^[A-Z]{3}$")


@app.get("/api/accounts/{account_id}/holdings")
def list_holdings(account_id: int):
    base = config.base_currency()
    with db.connect() as conn:
        _account(conn, account_id)
        out = []
        for h in db.rows(conn, "SELECT * FROM holdings WHERE account_id = ? ORDER BY name", account_id):
            value_base, price_day, problem = valuation.holding_value_base(conn, h, base)
            price = prices.latest(conn, h["symbol"]) if h["symbol"] else None
            out.append({**h, "price": price["close"] if price else h["manual_price"],
                        "price_currency": price["currency"] if price else h["currency"],
                        "price_day": price_day, "value_base": value_base, "problem": problem})
        return out


def _replace_holdings(conn, account_id: int, holdings: list[HoldingIn]) -> None:
    conn.execute("DELETE FROM holdings WHERE account_id = ?", (account_id,))
    conn.executemany(
        "INSERT INTO holdings (account_id, name, symbol, isin, quantity, manual_price, currency) VALUES (?, ?, ?, ?, ?, ?, ?)",
        [(account_id, h.name, (h.symbol or "").strip().upper() or None, h.isin, h.quantity, h.manual_price, h.currency)
         for h in holdings],
    )


@app.put("/api/accounts/{account_id}/holdings")
def replace_holdings(account_id: int, body: list[HoldingIn]):
    with db.connect() as conn:
        _account(conn, account_id)
        _replace_holdings(conn, account_id, body)
    errors = valuation.refresh()
    return {"ok": True, "errors": errors}


@app.get("/api/symbols")
def search_symbols(q: str):
    try:
        return prices.search(q)
    except prices.PriceError as e:
        raise HTTPException(502, f"symbol search failed: {e}")


# --- uploads (statements, CSVs, screenshots) ---------------------------------

def _run_extraction(upload_id: int) -> None:
    with db.connect() as conn:
        up = db.row(conn, "SELECT * FROM uploads WHERE id = ?", upload_id)
        accounts = db.rows(conn, "SELECT a.*, COALESCE(u.name, 'joint') AS owner FROM accounts a "
                                 "LEFT JOIN users u ON u.id = a.owner_id WHERE a.archived = 0")
        uploader = db.row(conn, "SELECT name FROM users WHERE id = ?", up["user_id"]) if up["user_id"] else None
    try:
        result = extract.extract(Path(up["path"]), up["media_type"], accounts, uploader["name"] if uploader else None)
        status, payload, error = "pending", result.model_dump_json(), None
    except extract.ExtractError as e:
        status, payload, error = "failed", None, str(e)
    except Exception as e:  # unexpected: keep the upload so it can be retried
        logger.exception("extraction failed")
        status, payload, error = "failed", None, f"Unexpected error: {e}"
    with db.connect() as conn:
        conn.execute("UPDATE uploads SET status = ?, extracted_json = ?, error = ? WHERE id = ?",
                     (status, payload, error, upload_id))


def _media_type(file: UploadFile) -> str:
    suffix = Path(file.filename or "").suffix.lower()
    if suffix in SUFFIX_TYPES:
        return SUFFIX_TYPES[suffix]
    return (file.content_type or mimetypes.guess_type(file.filename or "")[0] or "application/octet-stream").split(";")[0]


@app.post("/api/upload")
async def upload(file: UploadFile, background: BackgroundTasks, request: Request):
    """Accepts a screenshot, PDF statement or CSV (from the web page or the iPhone Shortcut); Claude reads it
    in the background and the result waits in the inbox for review."""
    data = await file.read()
    if not data:
        raise HTTPException(400, "empty file")
    if len(data) > MAX_UPLOAD_BYTES:
        raise HTTPException(413, "file too large (max 20 MB)")
    media_type = _media_type(file)
    name = Path(file.filename or "upload").name
    stored = db.uploads_dir() / f"{date.today().isoformat()}-{secrets.token_hex(4)}-{name}"
    stored.parent.mkdir(parents=True, exist_ok=True)
    stored.write_bytes(data)
    with db.connect() as conn:
        cur = conn.execute("INSERT INTO uploads (filename, media_type, path, status, user_id) VALUES (?, ?, ?, 'reading', ?)",
                           (name, media_type, str(stored), _user(request)["id"]))
        upload_id = cur.lastrowid
    background.add_task(_run_extraction, upload_id)
    return {"id": upload_id, "message": "Received. It will be in the FinanceAgent inbox in a few seconds."}


def _upload_out(u: dict) -> dict:
    u = {k: v for k, v in u.items() if k != "path"}
    x = json.loads(u.pop("extracted_json")) if u.get("extracted_json") else None
    if x is not None and "accounts" not in x:  # read before multi-account extraction: one account
        x["accounts"] = [{"account_id": x.get("account_id"), "name": x.get("institution") or "", "kind": "cash",
                          "balance": x.get("balance"), "currency": x.get("currency"), "positions": x.get("positions", [])}]
    u["extracted"] = x
    return u


@app.get("/api/uploads")
def list_uploads(include_done: bool = False):
    with db.connect() as conn:
        where = "" if include_done else "WHERE status IN ('reading', 'pending', 'failed')"
        return [_upload_out(u) for u in db.rows(
            conn, f"SELECT up.*, us.name AS uploaded_by FROM uploads up LEFT JOIN users us ON us.id = up.user_id "
                  f"{where} ORDER BY up.id DESC LIMIT 50")]


def _upload(conn, upload_id: int) -> dict:
    u = db.row(conn, "SELECT * FROM uploads WHERE id = ?", upload_id)
    if not u:
        raise HTTPException(404, "upload not found")
    return u


@app.get("/api/uploads/{upload_id}/file")
def upload_file(upload_id: int):
    with db.connect() as conn:
        u = _upload(conn, upload_id)
    return FileResponse(u["path"], media_type=u["media_type"], filename=u["filename"],
                        content_disposition_type="inline")


class PositionIn(BaseModel):
    name: str
    isin: Optional[str] = None
    symbol: Optional[str] = None
    quantity: Optional[float] = None
    value: Optional[float] = None
    currency: Optional[str] = None


class ApplyItem(BaseModel):
    """One account found in the file: booked to an existing account, or to a new one (new_account)."""
    account_id: Optional[int] = None
    new_account: Optional[AccountIn] = None
    balance: Optional[float] = None
    replace_holdings: bool = False
    positions: list[PositionIn] = []


class ApplyIn(BaseModel):
    as_of: date
    items: list[ApplyItem] = Field(min_length=1)


def _positions_to_holdings(positions: list[PositionIn], account_currency: str) -> tuple[list[HoldingIn], list[str]]:
    """Positions with a quantity get a ticker (given, or looked up by ISIN) so they follow market prices;
    the rest are kept at their stated value."""
    holdings, notes = [], []
    for p in positions:
        symbol = p.symbol
        if p.quantity and not symbol and p.isin:
            try:
                found = prices.search(p.isin)
                symbol = found[0]["symbol"] if found else None
            except prices.PriceError:
                symbol = None
            if not symbol:
                notes.append(f"no ticker found for {p.name} ({p.isin}); kept at its stated value")
        currency = (p.currency or account_currency).upper()
        if p.quantity and symbol:
            holdings.append(HoldingIn(name=p.name, symbol=symbol, isin=p.isin, quantity=p.quantity, currency=currency))
        elif p.value is not None:
            holdings.append(HoldingIn(name=p.name, isin=p.isin, quantity=1, manual_price=p.value, currency=currency))
        else:
            notes.append(f"skipped {p.name}: neither quantity with ticker nor value")
    return holdings, notes


@app.post("/api/uploads/{upload_id}/apply")
def apply_upload(upload_id: int, body: ApplyIn):
    for item in body.items:
        if (item.account_id is None) == (item.new_account is None):
            raise HTTPException(422, "each item needs either account_id or new_account")
    notes: list[str] = []
    replaced = False
    with db.connect() as conn:
        _upload(conn, upload_id)
        accounts = [_account(conn, i.account_id) if i.account_id is not None else None for i in body.items]
    # Look up tickers before writing anything, so a slow search doesn't hold the database.
    holdings_by_item = {}
    for n, item in enumerate(body.items):
        if item.replace_holdings and item.positions:
            currency = accounts[n]["currency"] if accounts[n] else item.new_account.currency
            holdings_by_item[n], item_notes = _positions_to_holdings(item.positions, currency)
            notes += item_notes
    with db.connect() as conn:
        for n, item in enumerate(body.items):
            if item.new_account:
                account_id = _insert_account(conn, item.new_account)
            else:
                account_id = item.account_id
            if n in holdings_by_item:
                _replace_holdings(conn, account_id, holdings_by_item[n])
                replaced = True
            if item.balance is not None:
                _add_balance(conn, account_id, body.as_of.isoformat(), item.balance, "upload", f"upload #{upload_id}")
        conn.execute("UPDATE uploads SET status = 'applied' WHERE id = ?", (upload_id,))
    errors = valuation.refresh() if replaced else []
    if not replaced:
        valuation.record_today()
    return {"ok": True, "notes": notes, "errors": errors}


@app.post("/api/uploads/{upload_id}/dismiss")
def dismiss_upload(upload_id: int):
    with db.connect() as conn:
        _upload(conn, upload_id)
        conn.execute("UPDATE uploads SET status = 'dismissed' WHERE id = ?", (upload_id,))
    return {"ok": True}


@app.post("/api/uploads/{upload_id}/retry")
def retry_upload(upload_id: int, background: BackgroundTasks):
    with db.connect() as conn:
        _upload(conn, upload_id)
        conn.execute("UPDATE uploads SET status = 'reading', error = NULL WHERE id = ?", (upload_id,))
    background.add_task(_run_extraction, upload_id)
    return {"ok": True}


app.mount("/", StaticFiles(directory=STATIC, html=True), name="static")
