"""Daily closes and exchange rates from Yahoo Finance's public chart API (no key needed)."""

from __future__ import annotations

import logging
from datetime import datetime, timezone

import httpx

from . import db

logger = logging.getLogger("financeagent")
CHART_URL = "https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
SEARCH_URL = "https://query1.finance.yahoo.com/v1/finance/search"
HEADERS = {"User-Agent": "Mozilla/5.0"}  # Yahoo refuses requests without a browser-like agent


class PriceError(Exception):
    pass


def fx_symbol(currency: str, base: str) -> str:
    return f"{currency}{base}=X"


def _client() -> httpx.Client:
    return httpx.Client(headers=HEADERS, timeout=15)


def parse_chart(payload: dict) -> tuple[str | None, list[tuple[str, float]]]:
    """Currency and (day, close) pairs from a chart response. Pence quotes (GBp) become pounds."""
    try:
        result = payload["chart"]["result"][0]
    except (KeyError, IndexError, TypeError):
        error = (payload.get("chart") or {}).get("error") or {}
        raise PriceError(error.get("description") or "no data")
    meta = result.get("meta", {})
    currency = meta.get("currency")
    offset = meta.get("gmtoffset") or 0  # timestamps are session opens; shift to the exchange's local day
    closes = ((result.get("indicators") or {}).get("quote") or [{}])[0].get("close") or []
    divisor = 1.0
    if currency == "GBp":
        currency, divisor = "GBP", 100.0
    points = []
    for ts, close in zip(result.get("timestamp") or [], closes):
        if close is None:
            continue
        day = datetime.fromtimestamp(ts + offset, timezone.utc).date().isoformat()
        points.append((day, close / divisor))
    return currency, points


def fetch(symbol: str, range_: str = "1mo", client: httpx.Client | None = None) -> tuple[str | None, list[tuple[str, float]]]:
    own = client is None
    client = client or _client()
    try:
        resp = client.get(CHART_URL.format(symbol=symbol), params={"range": range_, "interval": "1d"})
        if resp.status_code == 404:
            raise PriceError(f"unknown symbol {symbol}")
        resp.raise_for_status()
        return parse_chart(resp.json())
    except httpx.HTTPError as e:
        raise PriceError(f"{symbol}: {e}") from e
    finally:
        if own:
            client.close()


def store(symbol: str, currency: str | None, points: list[tuple[str, float]]) -> None:
    with db.connect() as conn:
        conn.executemany(
            "INSERT INTO prices (symbol, day, close, currency) VALUES (?, ?, ?, ?) "
            "ON CONFLICT(symbol, day) DO UPDATE SET close = excluded.close, currency = excluded.currency",
            [(symbol, day, close, currency) for day, close in points],
        )


def latest(conn, symbol: str, on_or_before: str | None = None) -> dict | None:
    if on_or_before:
        return db.row(conn, "SELECT * FROM prices WHERE symbol = ? AND day <= ? ORDER BY day DESC LIMIT 1",
                      symbol, on_or_before)
    return db.row(conn, "SELECT * FROM prices WHERE symbol = ? ORDER BY day DESC LIMIT 1", symbol)


def search(query: str, client: httpx.Client | None = None) -> list[dict]:
    """Ticker candidates for an ISIN or a name, e.g. IE00B3RBWM25 -> VWRD.L."""
    own = client is None
    client = client or _client()
    try:
        resp = client.get(SEARCH_URL, params={"q": query, "quotesCount": 8, "newsCount": 0})
        resp.raise_for_status()
        quotes = resp.json().get("quotes") or []
    except httpx.HTTPError as e:
        raise PriceError(str(e)) from e
    finally:
        if own:
            client.close()
    return [
        {"symbol": q["symbol"], "name": q.get("longname") or q.get("shortname") or q["symbol"],
         "exchange": q.get("exchDisp") or q.get("exchange"), "type": q.get("typeDisp")}
        for q in quotes if q.get("symbol") and q.get("isYahooFinance", True)
    ]


def refresh(symbols: set[str], currencies: set[str], base: str) -> list[str]:
    """Fetch the last month for every holding symbol and every FX pair needed. Returns error messages."""
    wanted = set(symbols) | {fx_symbol(c, base) for c in currencies if c and c != base}
    errors = []
    with _client() as client:
        for symbol in sorted(wanted):
            try:
                currency, points = fetch(symbol, client=client)
                store(symbol, currency, points)
            except PriceError as e:
                logger.warning("price refresh failed: %s", e)
                errors.append(str(e))
    return errors
