"""Shared handling of web-search answers (briefing, tax estimate, chat)."""

from __future__ import annotations


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
