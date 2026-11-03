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


def app_token() -> str:
    """APP_TOKEN protects the app (web UI and the iPhone Shortcut upload). Empty = no protection,
    fine while the app only listens on 127.0.0.1."""
    return os.environ.get("APP_TOKEN", "")


def claude_model() -> str:
    return os.environ.get("CLAUDE_MODEL") or "claude-opus-5-5"


def refresh_hours() -> float:
    """How often the background job fetches prices and records the day's values."""
    return float(os.environ.get("REFRESH_HOURS", "6"))
