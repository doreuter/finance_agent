"""What each Claude call cost, so the family sees what the app spends per month and on what."""

from __future__ import annotations

from datetime import date

from . import config, db

# USD per million tokens (input, output), as of 2026-11. Haiku 5.5 costs more above 100K prompt tokens.
PRICES = {
    "claude-opus-5-5": (4.0, 20.0),
    "claude-sonnet-5-5": (2.0, 10.0),
    "claude-haiku-5-5": (0.10, 0.50),
}
HAIKU_LONG_PROMPT = (0.50, 2.50)
CACHE_READ = {"claude-opus-5-5": 0.05}  # share of the input price; 0.1 for the others
CACHE_WRITE = 1.25  # 5-minute cache
WEB_SEARCH_USD = 10 / 1000  # per search


class BudgetExceeded(Exception):
    pass


def cost(model: str, input_tokens: int, output_tokens: int, web_searches: int = 0,
         cache_read: int = 0, cache_write: int = 0) -> float | None:
    """USD for one call; `input_tokens` are the uncached ones."""
    price = PRICES.get(model)
    if price is None:
        return None
    if model == "claude-haiku-5-5" and input_tokens + cache_read + cache_write > 100_000:
        price = HAIKU_LONG_PROMPT
    tokens_in = input_tokens + cache_read * CACHE_READ.get(model, 0.1) + cache_write * CACHE_WRITE
    return (tokens_in * price[0] + output_tokens * price[1]) / 1e6 + web_searches * WEB_SEARCH_USD


def record(task: str, model: str, usage) -> None:
    """Store one call's usage (the `usage` object of a Messages API response)."""
    if usage is None:
        return
    read = getattr(usage, "cache_read_input_tokens", 0) or 0
    write = getattr(usage, "cache_creation_input_tokens", 0) or 0
    tool = getattr(usage, "server_tool_use", None)
    searches = (getattr(tool, "web_search_requests", 0) or 0) if tool else 0
    with db.connect() as conn:
        conn.execute("INSERT INTO llm_usage (task, model, input_tokens, output_tokens, web_searches, cost_usd) "
                     "VALUES (?, ?, ?, ?, ?, ?)",
                     (task, model, (usage.input_tokens or 0) + read + write, usage.output_tokens or 0, searches,
                      cost(model, usage.input_tokens or 0, usage.output_tokens or 0, searches, read, write)))


def month_total(today: date | None = None) -> float:
    month = (today or date.today()).strftime("%Y-%m")
    with db.connect() as conn:
        return db.row(conn, "SELECT COALESCE(SUM(cost_usd), 0) AS usd FROM llm_usage "
                            "WHERE substr(created_at, 1, 7) = ?", month)["usd"]


def check_budget() -> None:
    """Raise BudgetExceeded once this month's spending reached CLAUDE_MONTHLY_BUDGET_USD. Briefings, proposals
    and Ask stop; reading uploads (a fraction of a cent each) keeps working."""
    budget = config.monthly_budget()
    if budget and month_total() >= budget:
        raise BudgetExceeded(f"This month's Claude budget of ${budget:.0f} is used up; briefings, proposals and Ask "
                             "resume on the 1st. Uploads keep working. Raise CLAUDE_MONTHLY_BUDGET_USD to allow more.")


def month_summary(today: date | None = None) -> dict:
    today = today or date.today()
    month = today.strftime("%Y-%m")
    with db.connect() as conn:
        rows = db.rows(conn, "SELECT task, model, COUNT(*) AS calls, SUM(input_tokens) AS input_tokens, "
                             "SUM(output_tokens) AS output_tokens, SUM(web_searches) AS web_searches, "
                             "SUM(cost_usd) AS cost_usd FROM llm_usage WHERE substr(created_at, 1, 7) = ? "
                             "GROUP BY task, model ORDER BY cost_usd DESC", month)
    return {"month": month, "total_usd": sum(r["cost_usd"] or 0 for r in rows), "budget_usd": config.monthly_budget(),
            "by_task": rows}
