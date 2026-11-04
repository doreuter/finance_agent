"""Savings plans (e.g. Neon's monthly ETF purchases): what goes in every month, where it goes, and an
illustration of where it could lead."""

from __future__ import annotations

import re
from typing import Literal, Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field, field_validator

from . import config, db, valuation

router = APIRouter()
PER_MONTH = {"monthly": 1.0, "quarterly": 1 / 3, "yearly": 1 / 12}
SCENARIOS = {"cautious": 0.02, "middle": 0.05, "optimistic": 0.07}  # nominal annual returns, before costs and taxes
HORIZONS = [5, 10, 20, 30]


class PlanIn(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    symbol: Optional[str] = Field(None, max_length=30)
    isin: Optional[str] = Field(None, max_length=12)
    amount: float = Field(gt=0, le=1_000_000)
    currency: str = Field("CHF", pattern=r"^[A-Z]{3}$")
    frequency: Literal["monthly", "quarterly", "yearly"] = "monthly"
    day: Optional[int] = Field(None, ge=1, le=31)

    @field_validator("symbol")
    @classmethod
    def ticker(cls, value: Optional[str]) -> Optional[str]:
        value = (value or "").strip().upper()
        if value and not re.fullmatch(r"[A-Z0-9.\-^=]{1,30}", value):
            raise ValueError("not a valid ticker")
        return value or None


def replace_plans(conn, account_id: int, plans: list[PlanIn]) -> None:
    conn.execute("DELETE FROM savings_plans WHERE account_id = ?", (account_id,))
    conn.executemany(
        "INSERT INTO savings_plans (account_id, name, symbol, isin, amount, currency, frequency, day) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        [(account_id, p.name, p.symbol, p.isin, p.amount, p.currency, p.frequency, p.day) for p in plans])


@router.get("/api/accounts/{account_id}/savings-plans")
def list_plans(account_id: int):
    with db.connect() as conn:
        return db.rows(conn, "SELECT * FROM savings_plans WHERE account_id = ? ORDER BY amount DESC", account_id)


@router.put("/api/accounts/{account_id}/savings-plans")
def put_plans(account_id: int, body: list[PlanIn]):
    with db.connect() as conn:
        if not db.row(conn, "SELECT 1 FROM accounts WHERE id = ?", account_id):
            raise HTTPException(404, "account not found")
        replace_plans(conn, account_id, body)
    return {"ok": True}


def future_value(present: float, monthly: float, annual_return: float, years: int) -> float:
    """Today's value plus a monthly contribution, compounded monthly."""
    r = (1 + annual_return) ** (1 / 12) - 1
    n = years * 12
    return present * (1 + r) ** n + (monthly * ((1 + r) ** n - 1) / r if r else monthly * n)


def overview(owner: str = "all") -> dict:
    """Monthly savings in CHF, split per plan and per person, compared with where the money already sits,
    and a projection of the depot value with these savings (an illustration, not a forecast)."""
    base = config.base_currency()
    where, args = valuation.owner_filter(owner)
    with db.connect() as conn:
        rows = db.rows(conn, "SELECT p.*, a.name AS account, a.institution, a.owner_id, u.name AS owner_name "
                             "FROM savings_plans p JOIN accounts a ON a.id = p.account_id LEFT JOIN users u ON u.id = a.owner_id "
                             "WHERE p.active = 1 AND a.archived = 0" + where + " ORDER BY p.amount DESC", *args)
        plans = []
        for p in rows:
            rate = valuation.fx_rate(conn, p["currency"], base)
            monthly = p["amount"] * PER_MONTH[p["frequency"]]
            plans.append({**p, "monthly": monthly, "monthly_base": monthly * rate if rate else None,
                          "problem": None if rate else f"no {p['currency']}/{base} rate yet"})
    total = sum(p["monthly_base"] or 0 for p in plans)
    for p in plans:
        p["share"] = (p["monthly_base"] or 0) / total if total else None
    portfolio = valuation.portfolio(owner)
    invested = portfolio["total"]
    held = {}
    for pos in portfolio["positions"]:
        key = (pos["symbol"] or pos["name"]).upper()
        held[key] = held.get(key, 0) + (pos["value_base"] or 0)
    for p in plans:  # how much of this fund the depot holds already, for "where does new money go"
        value = held.get((p["symbol"] or p["name"]).upper())
        p["held_share"] = value / invested if value is not None and invested else None
    by_owner: dict[str, float] = {}
    for p in plans:
        name = p["owner_name"] or "Joint"
        by_owner[name] = by_owner.get(name, 0) + (p["monthly_base"] or 0)
    projection = {name: [{"years": y, "value": round(future_value(invested, total, r, y))} for y in HORIZONS]
                  for name, r in SCENARIOS.items()}
    return {
        "base_currency": base, "monthly_total": total, "yearly_total": total * 12, "plans": plans,
        "by_owner": [{"name": k, "monthly": v} for k, v in by_owner.items()],
        "invested_now": invested, "scenarios": SCENARIOS, "projection": projection,
        "paid_in": {str(y): round(invested + total * 12 * y) for y in HORIZONS},
    }


@router.get("/api/savings")
def get_overview(owner: str = "all"):
    return overview(owner)
