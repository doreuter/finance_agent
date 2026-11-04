"""Settings from environment variables (and .env). Read at call time so tests can change them."""

from __future__ import annotations

import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def load_dotenv(path: Path = ROOT / ".env") -> None:
    """Minimal .env reader: KEY=value lines, existing environment wins."""
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def base_currency() -> str:
    return os.environ.get("BASE_CURRENCY", "CHF").upper()


def setup_token() -> str:
    """Needed to create the first login when set (always on a public server, so nobody else can
    claim an empty app). Empty = no token, fine while the app only listens on 127.0.0.1."""
    return os.environ.get("SETUP_TOKEN", "")


def anthropic_key() -> str:
    return os.environ.get("ANTHROPIC_API_KEY", "")


# Model and effort per task, chosen by value and cost (prices in app/usage.py):
# - extract: reading balances and positions from a screenshot is transcription, and every result is
#   reviewed in the inbox; the small model does it well at a fraction of the cost.
# - extract_retry: "Try again" after a failed or poor reading uses a stronger model.
# - research: the briefing's web research reads many search results (input-heavy); Sonnet is half Opus' price.
# - briefing: the stances and considerations are the judgment the family acts on; small input, so Opus is cheap here.
TASK_MODELS = {
    "extract": ("claude-haiku-5-5", "medium"),
    "extract_retry": ("claude-opus-5-5", "medium"),
    "research": ("claude-sonnet-5-5", "medium"),
    "briefing": ("claude-opus-5-5", "high"),
}


def model_for(task: str) -> tuple[str, str]:
    """(model, effort) for a task; CLAUDE_MODEL_<TASK> / CLAUDE_EFFORT_<TASK> override, e.g. CLAUDE_MODEL_EXTRACT."""
    model, effort = TASK_MODELS[task]
    key = task.upper()
    return os.environ.get(f"CLAUDE_MODEL_{key}") or model, os.environ.get(f"CLAUDE_EFFORT_{key}") or effort


def refresh_hours() -> float:
    """How often the background job fetches prices and records the day's values."""
    return float(os.environ.get("REFRESH_HOURS", "6"))


def weekly_briefing() -> bool:
    """Write the market briefing automatically every Monday (WEEKLY_BRIEFING=0 turns it off)."""
    return os.environ.get("WEEKLY_BRIEFING", "1") != "0"
