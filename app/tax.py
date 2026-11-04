"""Taxes: the year-end list for the tax return, withholding tax per position, and Claude's estimate of
wealth/income tax with ways to save (researched on the web, since tariffs differ by canton and municipality).

Pension money (pillar 2 and 3a) is not taxable wealth; payments into it are deductions.
"""

from __future__ import annotations

import csv
import io
import json
import logging
from datetime import date, datetime, timedelta, timezone
from typing import Optional

import anthropic
from fastapi import APIRouter, BackgroundTasks, HTTPException
from fastapi.responses import Response
from pydantic import BaseModel, Field

from . import advice, config, db, prices, research, usage, valuation

logger = logging.getLogger("financeagent")
router = APIRouter()
PENSION_KINDS = {"pillar2", "pillar3a"}
MAX_ESTIMATES_PER_DAY = 3
MAX_CONTINUATIONS = 5

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


# --- estimate (Claude + web search) -----------------------------------------------------------------

class Saving(BaseModel):
    title: str
    detail: str = Field(description="What to do and why, two or three sentences")
    estimated_saving_chf: Optional[float] = Field(None, description="Rough yearly tax saving, if it can be estimated")
    deadline: Optional[str] = Field(None, description="By when, e.g. '31 December'")


class PersonEstimate(BaseModel):
    name: str = Field(description="Family member, or both names for a married couple taxed jointly")
    wealth_tax_chf: Optional[float] = Field(None, description="Estimated yearly wealth tax (canton + municipality)")
    income_tax_marginal_rate: Optional[float] = Field(None, description="Estimated combined marginal income tax rate "
                                                      "(federal + cantonal + municipal) as a fraction, e.g. 0.28")
    income_tax_chf: Optional[float] = Field(None, description="Estimated yearly income tax, if taxable income is known")


class TaxEstimate(BaseModel):
    summary: str = Field(description="Three or four sentences: the family's tax picture and the biggest lever")
    people: list[PersonEstimate]
    savings: list[Saving] = Field(description="Concrete ways to save tax, biggest first; three to six")
    assumptions: list[str] = Field(description="What the estimate assumes (tariff year, rates used, church tax, ...)")


RESEARCH_SYSTEM = """You research Swiss tax rules for a family's tax estimate. Search the web for the current \
tax year's rules for the given canton and municipality: the wealth tax tariff and allowances, the cantonal and \
municipal tax multipliers (Steuerfuss), the income tax tariff (single/married), the federal income tax tariff, \
and the current limits for pillar 3a and the rules for pillar 2 buy-ins. Prefer official sources (ESTV, \
cantonal tax offices, the cantonal tax calculator) and note the year each figure is for. Write a research memo \
with the figures and their sources (URLs)."""

ESTIMATE_SYSTEM = """You estimate a Swiss family's taxes from the research memo and their data, and list \
concrete ways to save tax (pillar 3a up to the maximum, pillar 2 buy-ins and spreading them over years, \
staggering pension withdrawals over several accounts and years, deductible costs, timing). Show rough figures \
where the data allows and say which numbers are uncertain. Taxable wealth excludes pension money (pillar 2 and \
3a). A married couple is taxed jointly. This is an estimate to support planning, not tax advice; recommend the \
official cantonal tax calculator or an advisor for exact figures. Write in English."""


def _context(year: int) -> dict:
    s = statement(year)
    pension = {p["user_id"]: p for p in advice.pension_overview()}
    with db.connect() as conn:
        users = db.rows(conn, "SELECT * FROM users ORDER BY id")
    return {
        "tax_year": year,
        "people": [{
            "name": u["name"], "canton": u["canton"], "municipality": u["municipality"],
            "married": {1: True, 0: False}.get(u["married"]), "children": u["children"],
            "age": date.today().year - u["birth_year"] if u["birth_year"] else None,
            "approx_taxable_income_chf": u["taxable_income"],
            "employed_with_pension_fund": {1: True, 0: False}.get(u["has_pension_fund"]),
            "pillar3a_paid_this_year": round(pension.get(u["id"], {}).get("paid_3a") or 0),
            "pillar3a_max": pension.get(u["id"], {}).get("max_3a"),
            "pillar3a_accounts": sum(1 for a in s["accounts"] if a["kind"] == "pillar3a" and a["owner"] == u["name"]),
            "pillar2_buyin_potential": u["pillar2_buyin"],
        } for u in users],
        "taxable_wealth_chf_by_owner": {o: round(sum(a["value_base"] or 0 for a in s["accounts"] if a["taxable"] and a["owner"] == o))
                                        for o in {a["owner"] for a in s["accounts"]}},
        "foreign_accounts": [f'{a["institution"]} {a["name"]} ({a["country"]})' for a in s["foreign_accounts"]],
        "positions_by_domicile": sorted({p["domicile"] or "unknown" for a in s["accounts"] for p in a["positions"]}),
    }


def _research(client: anthropic.Anthropic, data: dict) -> tuple[str, list[dict]]:
    model, effort = config.model_for("tax_research")
    places = sorted({f'{p["municipality"] or "?"}, {p["canton"] or "?"}' for p in data["people"]})
    messages = [{"role": "user", "content": f"Tax year {data['tax_year']}. Taxed in: {'; '.join(places)}. "
                                            f"Married: {[p['married'] for p in data['people']]}. Research the rules."}]
    tools = [{"type": "web_search_20260209", "name": "web_search", "max_uses": 8}]
    content, response = [], None
    for _ in range(MAX_CONTINUATIONS + 1):
        with client.messages.stream(model=model, max_tokens=12000, system=RESEARCH_SYSTEM, messages=messages,
                                    tools=tools, output_config={"effort": effort}) as stream:
            response = stream.get_final_message()
        usage.record("tax_research", model, response.usage)
        if response.stop_reason == "refusal":
            raise TaxError("Claude declined the research step")
        content += response.content
        if response.stop_reason != "pause_turn":
            break
        messages = [messages[0], {"role": "assistant", "content": content}]
    memo, sources = research.text_and_sources(content, limit=30)
    if not memo:
        raise TaxError("The research step returned no text")
    return memo, sources


def _estimate(client: anthropic.Anthropic, data: dict, memo: str) -> TaxEstimate:
    model, effort = config.model_for("tax_estimate")
    response = client.messages.parse(
        model=model, max_tokens=12000, system=ESTIMATE_SYSTEM, output_config={"effort": effort},
        messages=[{"role": "user", "content": f"<family>\n{json.dumps(data, ensure_ascii=False, indent=1)}\n</family>\n\n"
                                              f"<research>\n{memo}\n</research>\n\nEstimate the taxes and list ways to save."}],
        output_format=TaxEstimate)
    usage.record("tax_estimate", model, response.usage)
    if response.stop_reason == "refusal" or response.parsed_output is None:
        raise TaxError(f"No estimate (stop reason {response.stop_reason})")
    return response.parsed_output


class TaxError(Exception):
    pass


def generate(year: int, client: anthropic.Anthropic | None = None) -> int:
    with db.connect() as conn:
        cutoff = (datetime.now(timezone.utc) - timedelta(minutes=30)).strftime("%Y-%m-%d %H:%M:%S")
        conn.execute("UPDATE tax_estimates SET status = 'failed', error = 'interrupted' WHERE status = 'running' AND created_at < ?",
                     (cutoff,))
        estimate_id = conn.execute("INSERT INTO tax_estimates (year, status) VALUES (?, 'running')", (year,)).lastrowid
    try:
        if client is None:
            if not config.anthropic_key():
                raise TaxError("Claude API key missing: set ANTHROPIC_API_KEY")
            client = anthropic.Anthropic()
        data = _context(year)
        if not any(p["canton"] for p in data["people"]):
            raise TaxError("Add your canton under Family first")
        memo, sources = _research(client, data)
        result = _estimate(client, data, memo)
    except (TaxError, anthropic.APIError) as e:
        with db.connect() as conn:
            conn.execute("UPDATE tax_estimates SET status = 'failed', error = ? WHERE id = ?", (str(e), estimate_id))
        raise TaxError(str(e)) from e
    with db.connect() as conn:
        conn.execute("UPDATE tax_estimates SET status = 'done', content_json = ? WHERE id = ?",
                     (json.dumps({"estimate": result.model_dump(), "sources": sources[:30]}), estimate_id))
    return estimate_id


def latest_estimate() -> dict:
    with db.connect() as conn:
        done = db.row(conn, "SELECT * FROM tax_estimates WHERE status = 'done' ORDER BY id DESC LIMIT 1")
        last = db.row(conn, "SELECT status, error FROM tax_estimates ORDER BY id DESC LIMIT 1")
    out = {"running": bool(last and last["status"] == "running"),
           "last_error": last["error"] if last and last["status"] == "failed" else None}
    if done:
        out.update({"id": done["id"], "year": done["year"], "created_at": done["created_at"], **json.loads(done["content_json"])})
    return out


@router.get("/api/tax/estimate")
def get_estimate():
    return latest_estimate()


def _run(year: int) -> None:
    try:
        generate(year)
    except TaxError as e:
        logger.warning("tax estimate failed: %s", e)


@router.post("/api/tax/estimate")
def start_estimate(background: BackgroundTasks):
    if latest_estimate()["running"]:
        raise HTTPException(409, "An estimate is already being made")
    with db.connect() as conn:
        n = db.row(conn, "SELECT COUNT(*) AS n FROM tax_estimates WHERE created_at > datetime('now', '-1 day')")["n"]
    if n >= MAX_ESTIMATES_PER_DAY:
        raise HTTPException(429, f"At most {MAX_ESTIMATES_PER_DAY} estimates a day")
    background.add_task(_run, date.today().year)
    return {"ok": True}
