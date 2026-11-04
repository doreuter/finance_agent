"""The weekly market briefing: Claude researches the family's holdings on the web, then writes a
structured briefing (a view per position, points to consider, suggested tasks).

Two calls, because structured output can't be combined with the citations web search produces:
1. research (web search, free text with sources), 2. briefing (structured output, no tools).
The result informs; it is not financial advice, and the app says so.
"""

from __future__ import annotations

import json
import logging
import re
from datetime import date, datetime, timedelta, timezone
from typing import Literal

import anthropic
from pydantic import BaseModel, Field

from . import advice, config, db, usage, valuation

logger = logging.getLogger("financeagent")
MAX_CONTINUATIONS = 5  # pause_turn resumes of the research call
MAX_SEARCHES = 15
STALE_RUNNING = timedelta(minutes=30)  # a 'running' briefing older than this died with a restart


class BriefingError(Exception):
    pass


class Note(BaseModel):
    title: str
    detail: str = Field(description="Two to four sentences")


class PositionView(BaseModel):
    name: str = Field(description="Position name as in the portfolio data")
    symbol: str | None = None
    stance: Literal["buy", "hold", "reduce", "watch"] = Field(
        description="buy = adding looks attractive for a long-term investor; hold = keep; reduce = consider "
                    "trimming (e.g. concentration, broken thesis); watch = something to follow before deciding")
    reasoning: str = Field(description="Two or three sentences: why, for a mid-to-long-term horizon")


class SuggestedTask(BaseModel):
    key: str = Field(description="Short stable slug for the topic, e.g. 'reduce-world-etf-overlap'; reuse the "
                                 "same slug when the same topic comes up again")
    title: str
    body: str = Field(description="What to do or check, and why; two to four sentences")


class Briefing(BaseModel):
    headline: str = Field(description="One line: the most important thing this week")
    summary: str = Field(description="Three to five sentences on the week for this portfolio")
    market: list[Note] = Field(description="Two to five market developments relevant to these holdings")
    positions: list[PositionView] = Field(description="One entry per position in the portfolio data")
    considerations: list[Note] = Field(description="Portfolio-level points to consider: allocation, overlap, "
                                                   "concentration, currency, cash, risks; two to five")
    tasks: list[SuggestedTask] = Field(default_factory=list, description="Zero to three concrete portfolio actions "
                                       "worth a reminder; skip topics listed as done, dismissed or snoozed")


RESEARCH_SYSTEM = """You prepare the research for a weekly briefing on a Swiss family's long-term investment \
portfolio (base currency CHF). Search the web for what happened in the past 7-10 days that matters for these \
holdings: news on each stock, the indices and sectors behind each ETF, and the overall market (central banks \
SNB/ECB/Fed, CHF exchange rates, inflation, earnings, valuations). Prefer reputable financial sources and \
recent articles; note dates. Then write a research memo: per holding what happened and how it moved, the \
market backdrop, and risks or opportunities a long-term investor should know. Be factual and cite sources by \
URL. Do not write the final recommendations; that is the next step."""

BRIEFING_SYSTEM = """You write the weekly briefing for a Swiss family's long-term portfolio from the research \
memo and the portfolio data. The family invests with a mid-to-long-term horizon (years, not weeks): no \
trading on short-term moves; weigh diversification, concentration, overlap between funds, costs, currency \
exposure, valuation and the family's ages and pension situation. For every position give a stance (buy, \
hold, reduce or watch) with reasoning; 'buy' and 'reduce' need a clear long-term reason, otherwise hold or \
watch. Be concrete and honest about uncertainty. This is information to support the family's own decisions, \
not personal financial advice; never promise returns. Suggest tasks only for portfolio actions worth a \
reminder, never for topics listed as done, dismissed or snoozed. Pillar 2 and 3a reminders (payments, \
deadlines, buy-ins) come from the app's own rules, so mention those topics in considerations if relevant \
but don't create tasks for them. Write in English."""


def iso_week(day: date) -> str:
    y, w, _ = day.isocalendar()
    return f"{y}-W{w:02d}"


def context() -> dict:
    """What Claude gets to know: positions with recent moves, allocation, ages and pension situation."""
    portfolio = valuation.portfolio("all")
    summary = valuation.summary("all")
    with db.connect() as conn:
        for p in portfolio["positions"]:
            if p["symbol"]:
                rows = db.rows(conn, "SELECT close FROM prices WHERE symbol = ? ORDER BY day", p["symbol"])
                p["change_1m"] = round(rows[-1]["close"] / rows[0]["close"] - 1, 4) if len(rows) > 1 else None
        tasks = db.rows(conn, "SELECT key, title, status, snooze_until FROM tasks "
                              "WHERE status IN ('open', 'snoozed', 'done', 'dismissed') ORDER BY updated_at DESC LIMIT 60")
        last = db.row(conn, "SELECT content_json FROM briefings WHERE status = 'done' ORDER BY id DESC LIMIT 1")
    owners = {u["user_id"]: u for u in advice.pension_overview()}
    return {
        "today": date.today().isoformat(),
        "base_currency": portfolio["base_currency"],
        "net_worth": round(summary["total"]),
        "allocation": {k["label"]: round(k["value"]) for k in summary["by_kind"]},
        "positions": [{
            "name": p["name"], "symbol": p["symbol"], "account": f'{p["institution"]} {p["account"]}',
            "owner": owners.get(p["owner_id"], {}).get("name", "joint"), "quantity": p["quantity"],
            "price": p["price"], "price_currency": p["price_currency"],
            "value_chf": round(p["value_base"]) if p["value_base"] is not None else None,
            "share_of_portfolio": round(p["share"], 3) if p["share"] is not None else None,
            "change_1m": p.get("change_1m"),
        } for p in portfolio["positions"]],
        "family": [{
            "name": o["name"], "age": date.today().year - o["birth_year"] if o["birth_year"] else None,
            "employed_with_pension_fund": {1: True, 0: False}.get(o["has_pension_fund"]),
            **({"pillar3a_paid_this_year": round(o["paid_3a"]), "pillar3a_max": o["max_3a"]} if o["has_3a"]
               else {"pillar3a": "no 3a account recorded"}),
            **({"pillar2_buyin_potential": o["pillar2_buyin"]} if o["has_pillar2"] else {}),
        } for o in owners.values()],
        "tasks": tasks,
        "previous_headline": json.loads(last["content_json"])["briefing"]["headline"] if last else None,
    }


def _research(client: anthropic.Anthropic, data: dict) -> tuple[str, list[dict]]:
    holdings = "\n".join(f'- {p["name"]} ({p["symbol"] or "no ticker"}), {p["share_of_portfolio"]} of portfolio'
                         for p in data["positions"])
    messages = [{"role": "user", "content": f"Today is {data['today']}. The holdings:\n{holdings}\n\n"
                                            "Research the past week for these holdings and the market."}]
    tools = [{"type": "web_search_20260209", "name": "web_search", "max_uses": MAX_SEARCHES}]
    model, effort = config.model_for("research")
    response, content = None, []
    for _ in range(MAX_CONTINUATIONS + 1):
        with client.messages.stream(model=model, max_tokens=16000, system=RESEARCH_SYSTEM, messages=messages,
                                    tools=tools, output_config={"effort": effort}) as stream:
            response = stream.get_final_message()
        usage.record("research", model, response.usage)
        if response.stop_reason == "refusal":
            raise BriefingError("Claude declined the research step")
        content += response.content
        if response.stop_reason != "pause_turn":
            break
        # the server's search loop paused; send the turn so far back and it resumes where it stopped
        messages = [messages[0], {"role": "assistant", "content": content}]
    text, sources = [], {}
    for block in content:
        if block.type == "text":
            text.append(block.text)
            for c in getattr(block, "citations", None) or []:
                if getattr(c, "url", None):
                    sources.setdefault(c.url, getattr(c, "title", None) or c.url)
        elif block.type == "web_search_tool_result" and isinstance(block.content, list):
            for r in block.content:
                if getattr(r, "url", None):
                    sources.setdefault(r.url, getattr(r, "title", None) or r.url)
    memo = "\n".join(text).strip()
    if not memo:
        raise BriefingError(f"The research step returned no text (stop reason {response.stop_reason})")
    # links come from web pages: only plain web links reach the page
    return memo, [{"url": u, "title": t} for u, t in sources.items() if u.startswith(("https://", "http://"))]


def _write(client: anthropic.Anthropic, data: dict, memo: str) -> Briefing:
    model, effort = config.model_for("briefing")
    response = client.messages.parse(
        model=model, max_tokens=16000, system=BRIEFING_SYSTEM, output_config={"effort": effort},
        messages=[{"role": "user", "content": f"<portfolio>\n{json.dumps(data, ensure_ascii=False, indent=1)}\n</portfolio>\n\n"
                                              f"<research>\n{memo}\n</research>\n\nWrite this week's briefing."}],
        output_format=Briefing,
    )
    usage.record("briefing", model, response.usage)
    if response.stop_reason == "refusal":
        raise BriefingError("Claude declined to write the briefing")
    if response.parsed_output is None:
        raise BriefingError(f"No briefing (stop reason {response.stop_reason})")
    return response.parsed_output


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:60] or "task"


def generate(client: anthropic.Anthropic | None = None) -> int:
    """Run a briefing now and store it; returns its id. Raises BriefingError when one is already running."""
    with db.connect() as conn:
        cutoff = (datetime.now(timezone.utc) - STALE_RUNNING).strftime("%Y-%m-%d %H:%M:%S")
        conn.execute("UPDATE briefings SET status = 'failed', error = 'interrupted' WHERE status = 'running' AND created_at < ?",
                     (cutoff,))
        if db.row(conn, "SELECT 1 FROM briefings WHERE status = 'running'"):
            raise BriefingError("A briefing is already being written")
        briefing_id = conn.execute("INSERT INTO briefings (week, status) VALUES (?, 'running')",
                                   (iso_week(date.today()),)).lastrowid
    try:
        if client is None:
            if not config.anthropic_key():
                raise BriefingError("Claude API key missing: set ANTHROPIC_API_KEY")
            client = anthropic.Anthropic()
        data = context()
        if not data["positions"]:
            raise BriefingError("No positions yet: add your depot's positions first")
        memo, sources = _research(client, data)
        result = _write(client, data, memo)
    except (BriefingError, anthropic.APIError) as e:
        with db.connect() as conn:
            conn.execute("UPDATE briefings SET status = 'failed', error = ? WHERE id = ?", (str(e), briefing_id))
        raise BriefingError(str(e)) from e
    with db.connect() as conn:
        conn.execute("UPDATE briefings SET status = 'done', content_json = ? WHERE id = ?",
                     (json.dumps({"briefing": result.model_dump(), "sources": sources[:40]}), briefing_id))
        for t in result.tasks:
            advice._upsert(conn, f"b-{_slug(t.key)}", None, "portfolio", t.title, t.body, source="briefing")
    return briefing_id


def due(today: date | None = None) -> bool:
    """Weekly from Monday: no finished briefing this week yet and fewer than 3 failed tries."""
    today = today or date.today()
    with db.connect() as conn:
        rows = db.rows(conn, "SELECT status FROM briefings WHERE week = ?", iso_week(today))
    return not any(r["status"] in ("done", "running") for r in rows) and sum(r["status"] == "failed" for r in rows) < 3


def latest() -> dict | None:
    with db.connect() as conn:
        row = db.row(conn, "SELECT * FROM briefings WHERE status = 'done' ORDER BY id DESC LIMIT 1")
        running = db.row(conn, "SELECT id, created_at FROM briefings WHERE status = 'running' ORDER BY id DESC LIMIT 1")
        failed = db.row(conn, "SELECT error, created_at FROM briefings ORDER BY id DESC LIMIT 1")
    out = {"running": bool(running),
           "last_error": failed["error"] if failed and failed.get("error") else None}
    if row:
        out.update({"id": row["id"], "week": row["week"], "created_at": row["created_at"], **json.loads(row["content_json"])})
    return out
