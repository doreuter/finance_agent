"""Turn positions read from a screenshot into holdings that follow the market.

Banking apps rarely show the quantity or a ticker. The quantity follows from value / price per share,
and a ticker candidate (shown, found by ISIN or name, or suggested by Claude) is only used when its
current Yahoo price matches the price in the document.
"""

from __future__ import annotations

import logging
import re
from concurrent.futures import ThreadPoolExecutor

import httpx

from . import prices

logger = logging.getLogger("financeagent")
PRICE_TOLERANCE = 0.04  # documents can be a day old; a different security is almost never within 4%
MAX_CANDIDATES = 15
ISIN = re.compile(r"^[A-Z]{2}[A-Z0-9]{9}[0-9]$")


def derive_quantity(value: float | None, price: float | None) -> float | None:
    """value / price, snapped to a whole number when the gap is within the documents' cent rounding."""
    if not value or not price or price <= 0:
        return None
    q = value / price
    tolerance = (q * 0.005 + 0.005) / price + 1e-9
    n = round(q)
    if n > 0 and abs(q - n) <= tolerance:
        return float(n)
    return round(q, 4)


class _Quotes:
    """Latest closes and FX rates, fetched once per extraction."""

    def __init__(self, client: httpx.Client):
        self.client = client
        self.cache: dict[str, tuple[str | None, float] | None] = {}

    def last(self, symbol: str) -> tuple[str | None, float] | None:
        if symbol not in self.cache:
            try:
                currency, points = prices.fetch(symbol, range_="5d", client=self.client)
                self.cache[symbol] = (currency, points[-1][1]) if points else None
            except prices.PriceError:
                self.cache[symbol] = None
        return self.cache[symbol]

    def in_currency(self, symbol: str, currency: str) -> tuple[float, str] | None:
        """(latest price converted to `currency`, the quote's own currency)."""
        quote = self.last(symbol)
        if not quote:
            return None
        quote_currency, close = quote
        if not quote_currency or quote_currency == currency:
            return close, quote_currency or currency
        fx = self.last(prices.fx_symbol(quote_currency, currency))
        return (close * fx[1], quote_currency) if fx else None


def _search(query: str, client: httpx.Client) -> list[str]:
    try:
        return [r["symbol"] for r in prices.search(query, client=client)]
    except prices.PriceError:
        return []


def candidates(p: dict, client: httpx.Client) -> list[str]:
    """Tickers to check, most likely first: shown ticker, ISIN lookup, Claude's hints, the name as shown."""
    out: list[str] = []
    if p.get("symbol"):
        out.append(p["symbol"].upper())
    if p.get("isin"):
        out += _search(p["isin"], client)
    for hint in (p.get("ticker_hints") or [])[:6]:
        hint = hint.strip()
        if ISIN.match(hint.upper()):
            out += _search(hint.upper(), client)
        elif re.fullmatch(r"[A-Za-z0-9.\-^=]{1,15}", hint):  # looks like a ticker
            out += [hint.upper()] + _search(hint, client)[:2]
        elif hint:  # a name
            out += _search(hint, client)[:6]
    if p.get("name"):
        out += _search(p["name"], client)[:4]
    seen: list[str] = []
    for s in out:
        if s not in seen:
            seen.append(s)
    return seen[:MAX_CANDIDATES]


def resolve(p: dict, quotes: _Quotes) -> dict:
    """Fill quantity and a verified symbol into one position dict (as read by extract)."""
    p = dict(p)
    if p.get("quantity") is None:
        p["quantity"] = derive_quantity(p.get("value"), p.get("price"))
        p["quantity_derived"] = p["quantity"] is not None
    reference = p.get("price") or (p["value"] / p["quantity"] if p.get("value") and p.get("quantity") else None)
    currency = (p.get("currency") or "").upper()
    p["match"] = None
    if reference and currency:
        for symbol in candidates(p, quotes.client):
            converted = quotes.in_currency(symbol, currency)
            if converted and abs(converted[0] / reference - 1) <= PRICE_TOLERANCE:
                p["symbol"] = symbol
                p["match"] = {"symbol": symbol, "price": round(converted[0], 2), "currency": currency,
                              "quote_currency": converted[1]}
                break
    return p


def resolve_all(extracted: dict) -> dict:
    """Resolve the positions of every account found in an extraction (dict from Extraction.model_dump)."""
    positions = [(a, i) for a in extracted.get("accounts", []) for i in range(len(a.get("positions") or []))]
    if not positions:
        return extracted
    with httpx.Client(headers=prices.HEADERS, timeout=15) as client:
        quotes = _Quotes(client)
        with ThreadPoolExecutor(max_workers=6) as pool:
            done = list(pool.map(lambda ai: resolve(ai[0]["positions"][ai[1]], quotes), positions))
    for (account, i), p in zip(positions, done):
        account["positions"][i] = p
    return extracted
