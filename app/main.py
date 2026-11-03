"""FastAPI app: accounts, balances, holdings, statement uploads and the net-worth dashboard."""

from __future__ import annotations

import hmac
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

from fastapi import BackgroundTasks, FastAPI, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import config, db, extract, prices, valuation

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


@app.middleware("http")
async def require_token(request: Request, call_next):
    """With APP_TOKEN set, every API call needs it as a bearer token (iPhone Shortcut) or the login cookie."""
    token = config.app_token()
    path = request.url.path
    if token and path.startswith("/api/") and path != "/api/login":
        supplied = request.cookies.get("fa_token") or ""
        auth = request.headers.get("authorization", "")
        if auth.lower().startswith("bearer "):
            supplied = auth[7:].strip()
        if not hmac.compare_digest(supplied, token):
            return JSONResponse({"detail": "login required"}, status_code=401)
    return await call_next(request)


class Login(BaseModel):
    token: str


@app.post("/api/login")
def login(body: Login):
    token = config.app_token()
    if token and not hmac.compare_digest(body.token, token):
        raise HTTPException(401, "wrong token")
    resp = JSONResponse({"ok": True})
    if token:
        resp.set_cookie("fa_token", token, httponly=True, samesite="strict", max_age=365 * 86400)
    return resp


# --- dashboard ---------------------------------------------------------------

@app.get("/api/summary")
def summary():
    return valuation.summary()


@app.get("/api/history")
def history():
    return valuation.history()


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


class AccountPatch(BaseModel):
    name: Optional[str] = Field(None, min_length=1, max_length=80)
    institution: Optional[str] = Field(None, min_length=1, max_length=80)
    kind: Optional[Kind] = None
    currency: Optional[str] = Field(None, pattern=r"^[A-Z]{3}$")
    valuation: Optional[Literal["balance", "holdings"]] = None
    archived: Optional[bool] = None


def _account(conn, account_id: int) -> dict:
    a = db.row(conn, "SELECT * FROM accounts WHERE id = ?", account_id)
    if not a:
        raise HTTPException(404, "account not found")
    return a


@app.get("/api/accounts")
def list_accounts():
    with db.connect() as conn:
        return db.rows(conn, "SELECT * FROM accounts WHERE archived = 0 ORDER BY kind, name")


@app.post("/api/accounts")
def create_account(body: AccountIn):
    with db.connect() as conn:
        cur = conn.execute(
            "INSERT INTO accounts (name, institution, kind, currency, valuation) VALUES (?, ?, ?, ?, ?)",
            (body.name, body.institution, body.kind, body.currency, body.valuation),
        )
        return _account(conn, cur.lastrowid)


@app.patch("/api/accounts/{account_id}")
def update_account(account_id: int, body: AccountPatch):
    changes = body.model_dump(exclude_none=True)
    with db.connect() as conn:
        _account(conn, account_id)
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
        accounts = db.rows(conn, "SELECT * FROM accounts WHERE archived = 0")
    try:
        result = extract.extract(Path(up["path"]), up["media_type"], accounts)
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
async def upload(file: UploadFile, background: BackgroundTasks):
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
        cur = conn.execute("INSERT INTO uploads (filename, media_type, path, status) VALUES (?, ?, ?, 'reading')",
                           (name, media_type, str(stored)))
        upload_id = cur.lastrowid
    background.add_task(_run_extraction, upload_id)
    return {"id": upload_id, "message": "Received. It will be in the FinanceAgent inbox in a few seconds."}


def _upload_out(u: dict) -> dict:
    u = {k: v for k, v in u.items() if k != "path"}
    u["extracted"] = json.loads(u.pop("extracted_json")) if u.get("extracted_json") else None
    return u


@app.get("/api/uploads")
def list_uploads(include_done: bool = False):
    with db.connect() as conn:
        where = "" if include_done else "WHERE status IN ('reading', 'pending', 'failed')"
        return [_upload_out(u) for u in db.rows(conn, f"SELECT * FROM uploads {where} ORDER BY id DESC LIMIT 50")]


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


class ApplyIn(BaseModel):
    account_id: int
    as_of: date
    balance: Optional[float] = None
    replace_holdings: bool = False
    positions: list[PositionIn] = []


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
    notes: list[str] = []
    with db.connect() as conn:
        _upload(conn, upload_id)
        account = _account(conn, body.account_id)
    if body.replace_holdings and body.positions:
        holdings, notes = _positions_to_holdings(body.positions, account["currency"])
        with db.connect() as conn:
            _replace_holdings(conn, account["id"], holdings)
    with db.connect() as conn:
        if body.balance is not None:
            _add_balance(conn, account["id"], body.as_of.isoformat(), body.balance, "upload", f"upload #{upload_id}")
        conn.execute("UPDATE uploads SET status = 'applied' WHERE id = ?", (upload_id,))
    errors = valuation.refresh() if body.replace_holdings else []
    if not body.replace_holdings:
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
