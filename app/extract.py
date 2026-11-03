"""Read a balance and positions out of a screenshot, PDF statement or CSV export with Claude."""

from __future__ import annotations

import base64
import json
from pathlib import Path
from typing import Optional

import anthropic
from pydantic import BaseModel, Field

from . import config

IMAGE_TYPES = {"image/png", "image/jpeg", "image/gif", "image/webp"}
TEXT_TYPES = {"text/csv", "text/plain", "application/csv", "application/vnd.ms-excel"}
MAX_TEXT_CHARS = 300_000  # a year of bank transactions is far below this


class ExtractError(Exception):
    pass


class Position(BaseModel):
    name: str = Field(description="Security or fund name as shown")
    isin: Optional[str] = Field(None, description="ISIN if shown")
    symbol: Optional[str] = Field(None, description="Ticker if shown; do not guess")
    quantity: Optional[float] = Field(None, description="Number of units/shares if shown")
    value: Optional[float] = Field(None, description="Market value of the position if shown")
    currency: Optional[str] = Field(None, description="ISO currency of the value, e.g. CHF")


class Extraction(BaseModel):
    account_id: Optional[int] = Field(None, description="id of the matching account from the list, or null if unsure")
    institution: Optional[str] = Field(None, description="Bank or provider the document is from, e.g. Neon, DKB, VIAC, AXA")
    as_of: Optional[str] = Field(None, description="Date the balance refers to, YYYY-MM-DD")
    balance: Optional[float] = Field(None, description="Total account value/balance on as_of, in currency")
    currency: Optional[str] = Field(None, description="ISO currency of balance")
    positions: list[Position] = Field(default_factory=list, description="Securities/funds held, if the document lists them")
    notes: str = Field("", description="One or two sentences: what the document is and anything uncertain")


SYSTEM = """You read personal finance documents (banking app screenshots, PDF statements, CSV exports \
from banks, brokers and Swiss pension providers) and extract the account's current value.

- balance is the total value of the account on as_of: the account balance for a bank account, the total \
portfolio or pension value for a depot, pillar 3a or pillar 2 account. For pillar 2 statements \
(Vorsorgeausweis) use the current retirement savings (Altersguthaben / avoir de vieillesse), not projections.
- For a CSV of transactions, use a stated closing balance (e.g. "Kontostand") if there is one; never \
compute a balance by summing transactions, leave balance null and say so in notes instead.
- Swiss and German documents write numbers like 1'234.50 or 1.234,50 - return plain numbers.
- Only report what the document shows. Use null where something is not visible.
- Pick account_id from the user's accounts only when institution and type clearly match."""


def content_blocks(path: Path, media_type: str) -> list[dict]:
    data = path.read_bytes()
    if media_type in IMAGE_TYPES:
        return [{"type": "image", "source": {"type": "base64", "media_type": media_type,
                                             "data": base64.standard_b64encode(data).decode()}}]
    if media_type == "application/pdf":
        return [{"type": "document", "source": {"type": "base64", "media_type": "application/pdf",
                                                "data": base64.standard_b64encode(data).decode()}}]
    if media_type in TEXT_TYPES or path.suffix.lower() in {".csv", ".txt"}:
        try:
            text = data.decode("utf-8-sig")
        except UnicodeDecodeError:
            text = data.decode("cp1252", errors="replace")  # DKB exports are sometimes Windows-1252
        if len(text) > MAX_TEXT_CHARS:
            raise ExtractError(f"File is too long to read ({len(text):,} characters); export a shorter period.")
        return [{"type": "text", "text": f"<file name=\"{path.name}\">\n{text}\n</file>"}]
    raise ExtractError(f"Unsupported file type {media_type}. Use PNG/JPEG screenshots, PDF or CSV.")


def extract(path: Path, media_type: str, accounts: list[dict], client: anthropic.Anthropic | None = None) -> Extraction:
    account_list = json.dumps(
        [{"id": a["id"], "name": a["name"], "institution": a["institution"], "kind": a["kind"],
          "currency": a["currency"]} for a in accounts],
        ensure_ascii=False,
    )
    client = client or anthropic.Anthropic()
    try:
        response = client.messages.parse(
            model=config.claude_model(),
            max_tokens=16000,
            system=SYSTEM,
            messages=[{
                "role": "user",
                "content": content_blocks(path, media_type) + [
                    {"type": "text", "text": f"The user's accounts: {account_list}\n\nExtract the account value from this file."},
                ],
            }],
            output_format=Extraction,
        )
    except anthropic.AuthenticationError as e:
        raise ExtractError("Claude API key missing or invalid: set ANTHROPIC_API_KEY in .env") from e
    except anthropic.APIConnectionError as e:
        raise ExtractError("Could not reach the Claude API") from e
    except anthropic.APIStatusError as e:
        raise ExtractError(f"Claude API error {e.status_code}: {e.message}") from e
    if response.stop_reason == "refusal":
        raise ExtractError("Claude declined to read this file")
    if response.parsed_output is None:
        raise ExtractError(f"No result (stop reason {response.stop_reason})")
    result = response.parsed_output
    if result.account_id is not None and result.account_id not in {a["id"] for a in accounts}:
        result.account_id = None
    return result
