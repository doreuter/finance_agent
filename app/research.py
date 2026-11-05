"""Shared handling of web research with Claude (briefing, savings plan proposal, chat)."""

from __future__ import annotations

import anthropic

from . import config, usage

MAX_CONTINUATIONS = 5


class ResearchError(Exception):
    pass


def claude_error(e: Exception) -> str:
    """A message the family can act on, instead of the raw API error."""
    text = str(e)
    if "credit balance is too low" in text:
        return "Your Anthropic credit is used up: top it up under Plans & Billing in the Anthropic Console."
    if isinstance(e, anthropic.AuthenticationError):
        return "The Claude API key is missing or invalid: check ANTHROPIC_API_KEY."
    if isinstance(e, anthropic.RateLimitError) or getattr(e, "status_code", None) == 529:
        return "Claude is busy right now; try again in a few minutes."
    return text


def run(client: anthropic.Anthropic, task: str, system: str, messages: list[dict], max_searches: int,
        max_tokens: int = 16000) -> list:
    """One Claude turn with web search, resumed while the server's search loop pauses (pause_turn).
    Returns the turn's content blocks; usage is recorded under `task`."""
    model, effort = config.model_for(task)
    tools = [{"type": "web_search_20260209", "name": "web_search", "max_uses": max_searches}]
    content = []
    for _ in range(MAX_CONTINUATIONS + 1):
        convo = messages + ([{"role": "assistant", "content": content}] if content else [])
        with client.messages.stream(model=model, max_tokens=max_tokens, system=system, messages=convo, tools=tools,
                                    output_config={"effort": effort}) as stream:
            response = stream.get_final_message()
        usage.record(task, model, response.usage)
        if response.stop_reason == "refusal":
            raise ResearchError("Claude declined this request")
        content += response.content
        if response.stop_reason != "pause_turn":
            break
    return content


def memo(client: anthropic.Anthropic, task: str, system: str, question: str, max_searches: int) -> tuple[str, list[dict]]:
    """A research memo and its sources."""
    text, sources = text_and_sources(run(client, task, system, [{"role": "user", "content": question}], max_searches))
    if not text:
        raise ResearchError("The research step returned no text")
    return text, sources


def text_and_sources(content: list, limit: int = 40) -> tuple[str, list[dict]]:
    """The answer text and its web sources. With dynamic filtering, Claude filters results in a code step and
    the text carries no citations, so sources come from the search results too; cited ones first. Only
    plain web links are kept, since they come from web pages."""
    text, cited, found = [], {}, {}
    for block in content:
        if block.type == "text":
            text.append(block.text)
            for c in getattr(block, "citations", None) or []:
                if getattr(c, "url", None):
                    cited.setdefault(c.url, getattr(c, "title", None) or c.url)
        elif block.type == "web_search_tool_result" and isinstance(block.content, list):
            for r in block.content:
                if getattr(r, "url", None):
                    found.setdefault(r.url, getattr(r, "title", None) or r.url)
    sources = {**cited, **{u: t for u, t in found.items() if u not in cited}}
    links = [{"url": u, "title": t} for u, t in sources.items() if u.startswith(("https://", "http://"))]
    # text blocks after a search continue the same sentence, so they are joined without spacing
    return "".join(text).strip(), links[:limit]
