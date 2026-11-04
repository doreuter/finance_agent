"""What each Claude call cost, so the family sees what the app spends per month and on what."""

from __future__ import annotations

from datetime import date

from . import db

# USD per million tokens (input, output), as of 2026-11. Haiku 5.5 costs more above 100K prompt tokens.
PRICES = {
    "claude-opus-5-5": (4.0, 20.0),
    "claude-sonnet-5-5": (2.0, 10.0),
    "claude-haiku-5-5": (0.10, 0.50),
}
HAIKU_LONG_PROMPT = (0.50, 2.50)
WEB_SEARCH_USD = 10 / 1000  # per search


def cost(model: str, input_tokens: int, output_tokens: int, web_searches: int = 0) -> float | None:
    price = PRICES.get(model)
    if price is None:
        return None
    if model == "claude-haiku-5-5" and input_tokens > 100_000:
        price = HAIKU_LONG_PROMPT
    return (input_tokens * price[0] + output_tokens * price[1]) / 1e6 + web_searches * WEB_SEARCH_USD


def record(task: str, model: str, usage) -> None:
    """Store one call's usage (the `usage` object of a Messages API response)."""
    if usage is None:
        return
    # cache reads/writes are billed as input too; the app doesn't use prompt caching, but count them if present
    tokens_in = (usage.input_tokens or 0) + (getattr(usage, "cache_read_input_tokens", 0) or 0) \
        + (getattr(usage, "cache_creation_input_tokens", 0) or 0)
    tool = getattr(usage, "server_tool_use", None)
    searches = (getattr(tool, "web_search_requests", 0) or 0) if tool else 0
    with db.connect() as conn:
        conn.execute("INSERT INTO llm_usage (task, model, input_tokens, output_tokens, web_searches, cost_usd) "
                     "VALUES (?, ?, ?, ?, ?, ?)",
                     (task, model, tokens_in, usage.output_tokens or 0, searches,
                      cost(model, tokens_in, usage.output_tokens or 0, searches)))


def month_summary(today: date | None = None) -> dict:
    today = today or date.today()
    month = today.strftime("%Y-%m")
    with db.connect() as conn:
        rows = db.rows(conn, "SELECT task, model, COUNT(*) AS calls, SUM(input_tokens) AS input_tokens, "
                             "SUM(output_tokens) AS output_tokens, SUM(web_searches) AS web_searches, "
                             "SUM(cost_usd) AS cost_usd FROM llm_usage WHERE substr(created_at, 1, 7) = ? "
                             "GROUP BY task, model ORDER BY cost_usd DESC", month)
    return {"month": month, "total_usd": sum(r["cost_usd"] or 0 for r in rows), "by_task": rows}
