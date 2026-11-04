"""What each account is worth now, in its own currency and in the base currency (CHF)."""

from __future__ import annotations

import logging
from datetime import date

from . import config, db, prices

logger = logging.getLogger("financeagent")
KINDS = {"cash": "Cash", "depot": "Investments", "pillar3a": "Pillar 3a", "pillar2": "Pillar 2"}
# After this many days without a new balance the dashboard asks for an update.
STALE_DAYS = {"cash": 35, "depot": 35, "pillar3a": 35, "pillar2": 400}


def fx_rate(conn, currency: str, base: str, day: str | None = None) -> float | None:
    if currency == base:
        return 1.0
    p = prices.latest(conn, prices.fx_symbol(currency, base), day)
    return p["close"] if p else None


def holding_value_base(conn, h: dict, base: str, day: str | None = None) -> tuple[float | None, str | None, str | None]:
    """(value in base currency, price day, problem) for one position."""
    if h["symbol"]:
        p = prices.latest(conn, h["symbol"], day)
        if not p:
            return None, None, f"no price yet for {h['symbol']}"
        currency = p["currency"] or h["currency"]
        rate = fx_rate(conn, currency, base, day)
        if rate is None:
            return None, p["day"], f"no {currency}/{base} rate yet"
        return h["quantity"] * p["close"] * rate, p["day"], None
    if h["manual_price"] is None:
        return None, None, f"{h['name']} has neither a ticker nor a price"
    rate = fx_rate(conn, h["currency"], base, day)
    if rate is None:
        return None, None, f"no {h['currency']}/{base} rate yet"
    return h["quantity"] * h["manual_price"] * rate, None, None


def account_value(conn, account: dict, day: str | None = None) -> dict:
    base = config.base_currency()
    problems: list[str] = []
    value_base = None
    as_of = None
    if account["valuation"] == "holdings":
        holdings = db.rows(conn, "SELECT * FROM holdings WHERE account_id = ?", account["id"])
        if holdings:
            value_base = 0.0
            for h in holdings:
                v, price_day, problem = holding_value_base(conn, h, base, day)
                if problem:
                    problems.append(problem)
                    continue
                value_base += v
                if price_day and (as_of is None or price_day > as_of):
                    as_of = price_day
        else:
            problems.append("no positions yet")
    else:
        sql = "SELECT * FROM balances WHERE account_id = ?" + (" AND day <= ?" if day else "") + \
              " ORDER BY day DESC, id DESC LIMIT 1"
        b = db.row(conn, sql, *([account["id"], day] if day else [account["id"]]))
        if b:
            as_of = b["day"]
            rate = fx_rate(conn, account["currency"], base, day)
            if rate is None:
                problems.append(f"no {account['currency']}/{base} rate yet")
            else:
                value_base = b["amount"] * rate
        else:
            problems.append("no balance yet")

    value = None
    if value_base is not None:
        rate = fx_rate(conn, account["currency"], base, day)
        value = value_base / rate if rate else None
    stale = None
    if account["valuation"] == "balance" and as_of:
        age = (date.fromisoformat(day or date.today().isoformat()) - date.fromisoformat(as_of)).days
        if age > STALE_DAYS[account["kind"]]:
            stale = age
    return {"value": value, "value_base": value_base, "as_of": as_of, "problems": problems, "stale_days": stale}


def owner_filter(owner: str) -> tuple[str, list]:
    """SQL condition on accounts `a` for the overview's person filter: all, joint, or a user id."""
    if owner in ("", "all"):
        return "", []
    if owner == "joint":
        return " AND a.owner_id IS NULL", []
    try:
        return " AND a.owner_id = ?", [int(owner)]
    except ValueError:
        return "", []


def summary(owner: str = "all") -> dict:
    base = config.base_currency()
    where, args = owner_filter(owner)
    with db.connect() as conn:
        accounts = db.rows(conn, "SELECT a.*, u.name AS owner_name FROM accounts a LEFT JOIN users u ON u.id = a.owner_id "
                                 "WHERE a.archived = 0" + where + " ORDER BY a.institution, a.kind, a.name", *args)
        out = []
        for a in accounts:
            out.append({**a, **account_value(conn, a)})
    by_kind = {k: 0.0 for k in KINDS}
    for a in out:
        if a["value_base"] is not None:
            by_kind[a["kind"]] += a["value_base"]
    return {
        "base_currency": base,
        "total": sum(by_kind.values()),
        "by_kind": [{"kind": k, "label": KINDS[k], "value": v} for k, v in by_kind.items()],
        "accounts": out,
    }


def portfolio(owner: str = "all") -> dict:
    """Every position in accounts valued by positions, with its share of the total and the change since
    the previous close (in the quote currency, so it shows how the security moved, not the CHF/USD rate)."""
    base = config.base_currency()
    where, args = owner_filter(owner)
    with db.connect() as conn:
        rows = db.rows(conn, "SELECT h.*, a.name AS account, a.institution, a.owner_id FROM holdings h "
                             "JOIN accounts a ON a.id = h.account_id WHERE a.archived = 0 AND a.valuation = 'holdings'"
                             + where + " ORDER BY h.name", *args)
        out = []
        for h in rows:
            value_base, price_day, problem = holding_value_base(conn, h, base)
            price = prices.latest(conn, h["symbol"]) if h["symbol"] else None
            prev = None
            if price:
                prev = db.row(conn, "SELECT close FROM prices WHERE symbol = ? AND day < ? ORDER BY day DESC LIMIT 1",
                              h["symbol"], price["day"])
            change = price["close"] / prev["close"] - 1 if price and prev and prev["close"] else None
            out.append({
                "id": h["id"], "name": h["name"], "symbol": h["symbol"], "isin": h["isin"], "quantity": h["quantity"],
                "account": h["account"], "institution": h["institution"], "owner_id": h["owner_id"],
                "price": price["close"] if price else h["manual_price"],
                "price_currency": (price["currency"] if price else None) or h["currency"],
                "price_day": price_day, "value_base": value_base, "day_change": change,
                "day_change_base": value_base * change / (1 + change) if value_base is not None and change is not None else None,
                "problem": problem,
            })
    total = sum(p["value_base"] for p in out if p["value_base"] is not None)
    for p in out:
        p["share"] = p["value_base"] / total if total and p["value_base"] is not None else None
    out.sort(key=lambda p: p["value_base"] if p["value_base"] is not None else -1, reverse=True)
    moved = [p["day_change_base"] for p in out if p["day_change_base"] is not None]
    return {"base_currency": base, "total": total, "day_change_base": sum(moved) if moved else None, "positions": out}


def record_today() -> None:
    """Store today's value of every account, for the history chart."""
    today = date.today().isoformat()
    with db.connect() as conn:
        for a in db.rows(conn, "SELECT * FROM accounts WHERE archived = 0"):
            v = account_value(conn, a)
            if v["value_base"] is None:
                continue
            conn.execute(
                "INSERT INTO daily_values (day, account_id, value, value_base) VALUES (?, ?, ?, ?) "
                "ON CONFLICT(day, account_id) DO UPDATE SET value = excluded.value, value_base = excluded.value_base",
                (today, a["id"], v["value"] if v["value"] is not None else v["value_base"], v["value_base"]),
            )


def history(owner: str = "all") -> list[dict]:
    where, args = owner_filter(owner)
    with db.connect() as conn:
        return db.rows(
            conn,
            "SELECT d.day, SUM(d.value_base) AS total, "
            + ", ".join(f"SUM(CASE WHEN a.kind = '{k}' THEN d.value_base ELSE 0 END) AS {k}" for k in KINDS)
            + " FROM daily_values d JOIN accounts a ON a.id = d.account_id WHERE a.archived = 0" + where
            + " GROUP BY d.day ORDER BY d.day",
            *args,
        )


def refresh() -> list[str]:
    """Fetch prices and FX for everything held, then record today's values."""
    base = config.base_currency()
    with db.connect() as conn:
        symbols = {r["symbol"] for r in db.rows(conn, "SELECT DISTINCT symbol FROM holdings WHERE symbol IS NOT NULL AND symbol != ''")}
        currencies = {r["c"] for r in db.rows(conn, "SELECT currency AS c FROM accounts UNION SELECT currency FROM holdings")}
    errors = prices.refresh(symbols, currencies, base)
    # A position quoted in another currency (e.g. a USD ETF in a CHF depot) needs that FX pair too.
    with db.connect() as conn:
        quoted = {r["currency"] for r in db.rows(conn, "SELECT DISTINCT currency FROM prices WHERE currency IS NOT NULL AND symbol NOT LIKE '%=X'")}
    missing = quoted - currencies - {base}
    if missing:
        errors += prices.refresh(set(), missing, base)
    record_today()
    return errors
