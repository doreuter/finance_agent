"""Taxes: the year-end list for the tax return and withholding tax per position. Questions about saving tax
go to Ask, which knows this data and looks up the canton's current rules.

Pension money (pillar 2 and 3a) is not taxable wealth; payments into it are deductions.
"""

from __future__ import annotations

import csv
import io
from datetime import date
from typing import Optional

from fastapi import APIRouter, HTTPException
from fastapi.responses import Response

from . import advice, config, db, prices, valuation

router = APIRouter()
PENSION_KINDS = {"pillar2", "pillar3a"}

# What happens to distributions, by the fund's or company's domicile (first two letters of the ISIN).
WITHHOLDING = {
    "CH": "Swiss withholding tax (35%) is deducted from distributions. You get it all back by listing the "
          "position and its income in the tax return's securities list.",
    "US": "US tax is withheld on dividends (15% if the bank has your W-8BEN form). Part of it can be credited "
          "in the Swiss return with form DA-1.",
    "IE": "Irish fund: no Swiss withholding tax; taxes withheld inside the fund can't be reclaimed.",
    "LU": "Luxembourg fund: no Swiss withholding tax; taxes withheld inside the fund can't be reclaimed.",
}
OTHER_WITHHOLDING = "Foreign withholding tax may be deducted; part of it can often be credited with form DA-1."


def withholding_note(isin: str | None) -> tuple[str | None, str]:
    if not isin or len(isin) < 2:
        return None, "Domicile unknown: add the ISIN under Accounts to see how distributions are taxed."
    country = isin[:2].upper()
    return country, WITHHOLDING.get(country, OTHER_WITHHOLDING)


def statement(year: int) -> dict:
    """Every account and position valued on 31 December of `year` (or today, for the running year),
    in its own currency and in CHF, plus the year's pension deductions per person."""
    base = config.base_currency()
    today = date.today()
    day = min(date(year, 12, 31), today).isoformat()
    with db.connect() as conn:
        accounts = db.rows(conn, "SELECT a.*, u.name AS owner_name FROM accounts a LEFT JOIN users u ON u.id = a.owner_id "
                                 "WHERE a.archived = 0 ORDER BY a.owner_id IS NULL, a.owner_id, a.institution, a.name")
        out = []
        for a in accounts:
            v = valuation.account_value(conn, a, day)
            entry = {"id": a["id"], "name": a["name"], "institution": a["institution"], "kind": a["kind"],
                     "owner": a["owner_name"] or "Joint", "country": a["country"], "currency": a["currency"],
                     "value": v["value"], "value_base": v["value_base"], "as_of": v["as_of"],
                     "taxable": a["kind"] not in PENSION_KINDS, "positions": []}
            if a["valuation"] == "holdings":
                for h in db.rows(conn, "SELECT * FROM holdings WHERE account_id = ? ORDER BY name", a["id"]):
                    value_base, price_day, problem = valuation.holding_value_base(conn, h, base, day)
                    price = prices.latest(conn, h["symbol"], day) if h["symbol"] else None
                    country, note = withholding_note(h["isin"])
                    entry["positions"].append({
                        "name": h["name"], "isin": h["isin"], "symbol": h["symbol"],
                        "quantity": None if h["value_only"] else h["quantity"],
                        "price": price["close"] if price else None, "price_currency": price["currency"] if price else h["currency"],
                        "price_day": price_day, "value_base": value_base, "problem": problem,
                        "domicile": country, "withholding": note})
            out.append(entry)
        deductions = []
        for u in db.rows(conn, "SELECT * FROM users ORDER BY id"):
            own = db.rows(conn, "SELECT id, kind FROM accounts WHERE owner_id = ?", u["id"])
            deductions.append({
                "name": u["name"],
                "pillar3a": advice.paid(conn, [a["id"] for a in own if a["kind"] == "pillar3a"], year),
                "pillar3a_max": advice.three_a_max(year, u["has_pension_fund"]),
                "pillar2_buyins": advice.paid(conn, [a["id"] for a in own if a["kind"] == "pillar2"], year),
            })
    taxable = sum(a["value_base"] or 0 for a in out if a["taxable"])
    return {"year": year, "as_of": day, "final": day == f"{year}-12-31", "base_currency": base, "accounts": out,
            "taxable_wealth": taxable, "foreign_accounts": [a for a in out if a["country"] != "CH"],
            "deductions": deductions}


@router.get("/api/tax/statement")
def get_statement(year: Optional[int] = None):
    year = year or date.today().year - (1 if date.today().month <= 4 else 0)  # until April: last year's return
    if not 2000 <= year <= date.today().year:
        raise HTTPException(422, "year out of range")
    return statement(year)


@router.get("/api/tax/statement.csv")
def statement_csv(year: int):
    s = get_statement(year)
    buf = io.StringIO()
    w = csv.writer(buf, delimiter=";")
    w.writerow(["Owner", "Bank", "Country", "Account", "Kind", "Position", "ISIN", "Quantity", "Price", "Price currency",
                "Value", "Currency", f"Value {s['base_currency']}", "Taxable wealth", "Valued on"])
    for a in s["accounts"]:
        w.writerow([a["owner"], a["institution"], a["country"], a["name"], a["kind"], "", "", "", "", "",
                    _num(a["value"]), a["currency"], _num(a["value_base"]), "yes" if a["taxable"] else "no (pension)", a["as_of"]])
        for p in a["positions"]:
            w.writerow([a["owner"], a["institution"], a["country"], a["name"], a["kind"], p["name"], p["isin"] or "",
                        _num(p["quantity"]), _num(p["price"]), p["price_currency"] or "", "", "", _num(p["value_base"]),
                        "yes" if a["taxable"] else "no (pension)", p["price_day"] or ""])
    return Response(buf.getvalue().encode("utf-8-sig"), media_type="text/csv",
                    headers={"Content-Disposition": f'attachment; filename="financeagent-tax-{year}.csv"'})


def _num(v) -> str:
    return "" if v is None else f"{v:.2f}"
