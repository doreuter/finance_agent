"""A long-term ETF savings plan proposal for one person, at their preferred broker.

Claude first researches what the broker offers as savings plans (which ETFs, fees, which are free, minimums),
then proposes two to four funds with fixed monthly amounts that fit the person's age, risk profile, existing
holdings and pension, meant to run unchanged for years: new money does the rebalancing, a review once a year.
The person can adopt it as their savings plans in the app; they set it up at the broker themselves.
"""

from __future__ import annotations

import json
import logging
from datetime import date, datetime, timedelta, timezone
from typing import Optional

import anthropic
from fastapi import APIRouter, BackgroundTasks, HTTPException, Request
from pydantic import BaseModel, Field

from . import advice, config, db, research, savings, usage, valuation

logger = logging.getLogger("financeagent")
router = APIRouter()
MAX_PER_DAY = 3
MAX_SEARCHES = 6  # one broker's offer; each search loads long pages (10 searches cost ~230k input tokens)


class ProposalError(Exception):
    pass


class ProposedPlan(BaseModel):
    name: str = Field(description="ETF name as the broker lists it")
    isin: Optional[str] = None
    ticker: Optional[str] = Field(None, description="Yahoo Finance ticker if known, e.g. XDWD.DE, VWRL.SW")
    role: str = Field(description="Its job in the plan, e.g. 'core: developed world', 'Swiss home market', 'emerging markets'")
    monthly_chf: float = Field(description="Amount per month in CHF")
    ter: Optional[float] = Field(None, description="Total expense ratio as a fraction, e.g. 0.0019")
    broker_cost: str = Field(description="What the broker charges for this savings plan, e.g. 'free as savings plan'")
    why: str = Field(description="Two sentences: why this fund, given what is already held")


class Proposal(BaseModel):
    summary: str = Field(description="Three or four sentences: the idea of the plan and why it fits this person")
    broker: str
    monthly_total_chf: float
    plans: list[ProposedPlan] = Field(description="Two to four funds; amounts add up to the monthly total")
    changes: list[str] = Field(description="What changes compared with the current savings plans, one point each")
    review: str = Field(description="When to look at it again (e.g. once a year, or after a big life change) and "
                                    "what would justify a change; never monthly")
    caveats: list[str] = Field(description="Risks and assumptions, e.g. currency, fees that may change, availability")


RESEARCH_SYSTEM = """You research a broker's ETF savings plan offer for a Swiss investor. Search the web for the \
broker's current offer: which ETFs can be bought as a savings plan (Sparplan), which of them are free or \
discounted, trading and custody fees, currency exchange costs, minimum amounts and execution days. Prefer the \
broker's own pages and recent reviews; note dates. Also note each ETF's ISIN, TER, domicile and replication \
where you find them. Write a research memo with sources."""

PROPOSAL_SYSTEM = """You propose a long-term monthly ETF savings plan for one person in a Swiss family, at their \
preferred broker, from the research memo and their data. Rules:
- Only ETFs the research shows as available as savings plans at that broker; prefer the free or cheapest ones \
when they are equivalent.
- Two to four funds. Broad and simple beats clever. Avoid adding to overlaps and concentrations the person \
already has (look at their current positions), and consider their home currency (CHF), age and horizon, risk \
profile, their wishes, and that pillar 2 and 3a are invested separately.
- Amounts in round CHF adding up to their monthly budget; respect the broker's minimums.
- The plan must run unchanged for years: new money rebalances it; review once a year. Do not propose \
changing it monthly or timing the market.
- Be honest about fees, currency risk and uncertainty. This informs the person's own decision; it is not \
personal financial advice. Write in English."""


def _person(conn, user_id: int) -> dict:
    u = db.row(conn, "SELECT * FROM users WHERE id = ?", user_id)
    if not u:
        raise ProposalError("family member not found")
    return u


def context(user_id: int) -> dict:
    with db.connect() as conn:
        u = _person(conn, user_id)
    portfolio = valuation.portfolio(str(user_id))
    plans = savings.overview(str(user_id))
    pension = next((p for p in advice.pension_overview() if p["user_id"] == user_id), {})
    return {
        "today": date.today().isoformat(), "name": u["name"],
        "age": date.today().year - u["birth_year"] if u["birth_year"] else None,
        "broker": u["preferred_broker"], "monthly_budget_chf": u["savings_budget"],
        "risk_profile": u["risk_profile"], "wishes": u["invest_notes"],
        "positions": [{"name": p["name"], "symbol": p["symbol"], "isin": p["isin"],
                       "value_chf": round(p["value_base"]) if p["value_base"] is not None else None,
                       "share": round(p["share"], 3) if p["share"] is not None else None} for p in portfolio["positions"]],
        "current_savings_plans": [{"fund": p["name"], "symbol": p["symbol"], "chf_per_month": round(p["monthly_base"] or 0)}
                                  for p in plans["plans"]],
        "pillar3a": {"has_account": pension.get("has_3a"), "paid_this_year": pension.get("paid_3a"), "max": pension.get("max_3a")},
        "has_pillar2": pension.get("has_pillar2"),
    }


def _write(client: anthropic.Anthropic, data: dict, memo: str) -> Proposal:
    model, effort = config.model_for("proposal")
    response = client.messages.parse(
        model=model, max_tokens=12000, system=PROPOSAL_SYSTEM, output_config={"effort": effort},
        messages=[{"role": "user", "content": f"<person>\n{json.dumps(data, ensure_ascii=False, indent=1)}\n</person>\n\n"
                                              f"<research>\n{memo}\n</research>\n\nPropose the savings plan."}],
        output_format=Proposal)
    usage.record("proposal", model, response.usage)
    if response.stop_reason == "refusal" or response.parsed_output is None:
        raise ProposalError(f"No proposal (stop reason {response.stop_reason})")
    return response.parsed_output


def generate(user_id: int, client: anthropic.Anthropic | None = None) -> int:
    with db.connect() as conn:
        cutoff = (datetime.now(timezone.utc) - timedelta(minutes=30)).strftime("%Y-%m-%d %H:%M:%S")
        conn.execute("UPDATE savings_proposals SET status = 'failed', error = 'interrupted' "
                     "WHERE status = 'running' AND created_at < ?", (cutoff,))
        proposal_id = conn.execute("INSERT INTO savings_proposals (user_id, status) VALUES (?, 'running')", (user_id,)).lastrowid
    try:
        data = context(user_id)
        if not data["broker"] or not data["monthly_budget_chf"]:
            raise ProposalError("Set your broker and monthly amount first")
        if client is None:
            if not config.anthropic_key():
                raise ProposalError("Claude API key missing: set ANTHROPIC_API_KEY")
            client = anthropic.Anthropic()
        memo, sources = research.memo(client, "proposal_research", RESEARCH_SYSTEM,
                                      f"Broker: {data['broker']} (Switzerland). Today is {data['today']}. "
                                      "Research its ETF savings plan offer.", MAX_SEARCHES)
        result = _write(client, data, memo)
    except (ProposalError, research.ResearchError, anthropic.APIError) as e:
        message = research.claude_error(e)
        with db.connect() as conn:
            conn.execute("UPDATE savings_proposals SET status = 'failed', error = ? WHERE id = ?", (message, proposal_id))
        raise ProposalError(message) from e
    with db.connect() as conn:
        conn.execute("UPDATE savings_proposals SET status = 'done', content_json = ? WHERE id = ?",
                     (json.dumps({"proposal": result.model_dump(), "sources": sources[:25]}), proposal_id))
    return proposal_id


def latest(user_id: int) -> dict:
    with db.connect() as conn:
        done = db.row(conn, "SELECT * FROM savings_proposals WHERE user_id = ? AND status = 'done' ORDER BY id DESC LIMIT 1", user_id)
        last = db.row(conn, "SELECT status, error FROM savings_proposals WHERE user_id = ? ORDER BY id DESC LIMIT 1", user_id)
    out = {"running": bool(last and last["status"] == "running"),
           "last_error": last["error"] if last and last["status"] == "failed" else None}
    if done:
        out.update({"id": done["id"], "created_at": done["created_at"], **json.loads(done["content_json"])})
    return out


@router.get("/api/savings/proposal")
def get_proposal(request: Request):
    return latest(request.state.user["id"])


def _run(user_id: int) -> None:
    try:
        generate(user_id)
    except ProposalError as e:
        logger.warning("savings proposal failed: %s", e)


@router.post("/api/savings/proposal")
def start_proposal(request: Request, background: BackgroundTasks):
    user = request.state.user
    if not user["preferred_broker"] or not user["savings_budget"]:
        raise HTTPException(422, "Set your broker and monthly amount first")
    if latest(user["id"])["running"]:
        raise HTTPException(409, "A proposal is already being written")
    with db.connect() as conn:
        n = db.row(conn, "SELECT COUNT(*) AS n FROM savings_proposals WHERE user_id = ? AND created_at > datetime('now', '-1 day')",
                   user["id"])["n"]
    if n >= MAX_PER_DAY:
        raise HTTPException(429, f"At most {MAX_PER_DAY} proposals a day")
    background.add_task(_run, user["id"])
    return {"ok": True}


class AdoptIn(BaseModel):
    account_id: int


@router.post("/api/savings/proposal/{proposal_id}/adopt")
def adopt(proposal_id: int, body: AdoptIn, request: Request):
    """Make the proposal the savings plans of one of the person's depots (in the app; the broker is set up by hand)."""
    user = request.state.user
    with db.connect() as conn:
        row = db.row(conn, "SELECT * FROM savings_proposals WHERE id = ? AND user_id = ? AND status = 'done'", proposal_id, user["id"])
        if not row:
            raise HTTPException(404, "proposal not found")
        account = db.row(conn, "SELECT * FROM accounts WHERE id = ? AND archived = 0", body.account_id)
        if not account or account["kind"] != "depot":
            raise HTTPException(422, "choose a depot")
        plans = []
        for p in json.loads(row["content_json"])["proposal"]["plans"]:
            try:
                plans.append(savings.PlanIn(name=p["name"][:120], symbol=p.get("ticker"), isin=(p.get("isin") or "")[:12] or None,
                                            amount=round(p["monthly_chf"], 2), currency="CHF"))
            except ValueError:  # an odd ticker from the proposal: keep the plan without it
                plans.append(savings.PlanIn(name=p["name"][:120], amount=round(p["monthly_chf"], 2), currency="CHF"))
        savings.replace_plans(conn, account["id"], plans)
    return {"ok": True, "plans": len(plans)}
