"""Ask FinanceAgent: questions about the family's finances, answered by Claude with the family's data and
web search for current rules (tax, pension limits, markets). The conversation lives in the browser; the
server keeps only a count per question for the daily limit."""

from __future__ import annotations

import json
from datetime import date
from typing import Literal

import anthropic
from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from . import advice, config, db, proposal, research, savings, tax, valuation

router = APIRouter()
MAX_QUESTIONS_PER_DAY = 40

SYSTEM = """You are FinanceAgent, the assistant in a Swiss family's private finance app. You answer their \
questions about their own money: accounts, investments and ETF savings plans, pillar 2 and 3a, and Swiss \
taxes. You know their data (below, as of today) and can search the web for current rules, rates and market \
news; search when an answer depends on current figures or the canton's rules, and cite your sources.

- Be concrete and use their numbers. Keep answers short (a few paragraphs or a list) unless asked for detail.
- Investments: think mid-to-long term; explain trade-offs (diversification, costs, currency, taxes) and say \
what you would weigh, rather than giving orders.
- Saving tax is a main reason they ask: look at their 3a room, pillar 2 buy-in potential, canton, income, \
marital status and deductions, and name concrete steps with rough amounts and deadlines (e.g. paying the \
full 3a before 31 December, spreading buy-ins, staggering withdrawals over several 3a accounts). Search for \
the canton's current rules when the amount depends on them.
- Taxes and pensions: rules differ by canton and change every year; say which year and canton a figure is for.
- Savings plans are meant to run for years; don't suggest monthly changes.
- You inform their own decisions. This is not financial or tax advice; for large or irreversible steps \
(pillar 2 buy-ins, withdrawals, property) suggest checking with the pension fund, the tax office or an advisor.
- Use plain Markdown: short paragraphs, **bold**, bullet lists. No tables.
- Answer in the language the latest question is written in (English question, English answer), whatever the \
language of their banks or canton."""


class Message(BaseModel):
    role: Literal["user", "assistant"]
    content: str = Field(min_length=1, max_length=8000)


class ChatIn(BaseModel):
    messages: list[Message] = Field(min_length=1, max_length=30)


def _proposal_summary(user_id: int | None) -> dict | None:
    if user_id is None:
        return None
    p = proposal.latest(user_id).get("proposal")
    return {"summary": p["summary"], "plans": [{k: x[k] for k in ("name", "monthly_chf", "role")} for x in p["plans"]]} if p else None


def family_context(owner_name: str, owner_id: int | None = None) -> str:
    """Everything Claude should know, compact: who, what they own, what they save, pensions, taxes."""
    summary = valuation.summary("all")
    portfolio = valuation.portfolio("all")
    plans = savings.overview("all")
    with db.connect() as conn:
        users = db.rows(conn, "SELECT id, name FROM users")
        tasks = db.rows(conn, "SELECT title, status FROM tasks WHERE status = 'open' ORDER BY id DESC LIMIT 15")
        brief = db.row(conn, "SELECT content_json FROM briefings WHERE status = 'done' ORDER BY id DESC LIMIT 1")
    owners = {u["id"]: u["name"] for u in users}
    statement = tax.statement(date.today().year - 1)
    data = {
        "today": date.today().isoformat(), "asking": owner_name, "base_currency": summary["base_currency"],
        "net_worth": round(summary["total"]),
        "accounts": [{"name": a["name"], "bank": a["institution"], "owner": owners.get(a["owner_id"], "joint"),
                      "kind": a["kind"], "currency": a["currency"], "value": round(a["value"]) if a["value"] is not None else None,
                      "value_chf": round(a["value_base"]) if a["value_base"] is not None else None}
                     for a in summary["accounts"]],
        "positions": [{"name": p["name"], "symbol": p["symbol"], "isin": p["isin"], "quantity": p["quantity"],
                       "value_chf": round(p["value_base"]) if p["value_base"] is not None else None,
                       "share": round(p["share"], 3) if p["share"] is not None else None,
                       "owner": owners.get(p["owner_id"], "joint"), "account": p["account"]}
                      for p in portfolio["positions"]],
        "savings_plans": [{"fund": p["name"], "symbol": p["symbol"], "chf_per_month": round(p["monthly_base"] or 0),
                           "owner": p["owner_name"] or "joint"} for p in plans["plans"]],
        "people": [{k: v for k, v in p.items() if k not in ("user_id",)} for p in advice.pension_overview()],
        "savings_plan_proposal": _proposal_summary(owner_id),
        "taxable_wealth_last_31_dec": round(statement["taxable_wealth"]),
        "open_tasks": [t["title"] for t in tasks],
        "latest_briefing_headline": json.loads(brief["content_json"])["briefing"]["headline"] if brief else None,
    }
    return json.dumps(data, ensure_ascii=False, default=str)


def ask(client: anthropic.Anthropic, messages: list[dict], context: str) -> dict:
    system = f"{SYSTEM}\n\n<family_data>\n{context}\n</family_data>"
    try:
        content = research.run(client, "chat", system, messages, max_searches=5, max_tokens=6000)
    except research.ResearchError:
        return {"text": "I can't help with that one.", "sources": []}
    text, sources = research.text_and_sources(content, limit=10)
    return {"text": text or "(no answer)", "sources": sources}


@router.post("/api/chat")
def chat(body: ChatIn, request: Request):
    user = request.state.user
    if body.messages[-1].role != "user":
        raise HTTPException(422, "the last message must be the question")
    with db.connect() as conn:
        n = db.row(conn, "SELECT COUNT(*) AS n FROM chat_log WHERE user_id = ? AND created_at > datetime('now', '-1 day')",
                   user["id"])["n"]
        if n >= MAX_QUESTIONS_PER_DAY:
            raise HTTPException(429, f"At most {MAX_QUESTIONS_PER_DAY} questions a day; more tomorrow")
        conn.execute("INSERT INTO chat_log (user_id) VALUES (?)", (user["id"],))
    if not config.anthropic_key():
        raise HTTPException(503, "Claude API key missing: set ANTHROPIC_API_KEY")
    try:
        return ask(anthropic.Anthropic(), [m.model_dump() for m in body.messages], family_context(user["name"], user["id"]))
    except anthropic.APIError as e:
        raise HTTPException(502, research.claude_error(e))
