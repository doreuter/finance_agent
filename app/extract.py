"""Read the balances and positions of one or more accounts out of a screenshot, PDF statement or CSV export with Claude."""

from __future__ import annotations

import base64
import json
import os
from pathlib import Path
from typing import Literal, Optional

import anthropic
from pydantic import BaseModel, Field

from . import config, usage

IMAGE_TYPES = {"image/png", "image/jpeg", "image/gif", "image/webp"}
TEXT_TYPES = {"text/csv", "text/plain", "application/csv", "application/vnd.ms-excel"}
MAX_TEXT_CHARS = 300_000  # a year of bank transactions is far below this


class ExtractError(Exception):
    pass


class Position(BaseModel):
    name: str = Field(description="Security or fund name as shown")
    isin: Optional[str] = Field(None, description="ISIN if shown")
    symbol: Optional[str] = Field(None, description="Ticker if shown")
    quantity: Optional[float] = Field(None, description="Number of units/shares if shown")
    price: Optional[float] = Field(None, description="Current price of one unit/share if shown (not the buy price)")
    value: Optional[float] = Field(None, description="Market value of the whole position if shown")
    currency: Optional[str] = Field(None, description="ISO currency of price and value, e.g. EUR")
    ticker_hints: list[str] = Field(default_factory=list, description="If the document shows no ticker/ISIN but you "
                                    "recognise the security: up to 6 search hints, best first - likely Yahoo Finance "
                                    "tickers (e.g. XDWT.DE, TSM), the ISIN, and the full plain name (e.g. 'Taiwan "
                                    "Semiconductor', 'Amundi MSCI EM Asia'). If an abbreviated name could mean "
                                    "several funds, give hints for each. They are checked against market prices "
                                    "before use.")


class FoundAccount(BaseModel):
    account_id: Optional[int] = Field(None, description="id of the matching account from the user's list, or null if it is not clearly the same account")
    name: str = Field(description="Account name as shown, e.g. 'Privatkonto', 'Space Ferien', 'Tagesgeld', 'Visa Card'")
    kind: Literal["cash", "depot", "pillar3a", "pillar2"] = Field(description="cash for current/savings/credit card accounts, depot for securities")
    balance: Optional[float] = Field(None, description="Value/balance of this account on as_of, in currency; credit card debt is negative")
    currency: Optional[str] = Field(None, description="ISO currency of balance, e.g. CHF")
    positions: list[Position] = Field(default_factory=list, description="Securities/funds held in this account, if listed")
    contributions_ytd: Optional[float] = Field(None, description="Pillar 3a: total paid in during the current calendar "
                                               "year so far, if the statement shows it (Einzahlungen / versements)")
    pillar2_buyin: Optional[float] = Field(None, description="Pillar 2 certificate: maximum possible buy-in "
                                           "(Einkaufspotenzial / möglicher Einkauf / rachat possible), if shown")


class Extraction(BaseModel):
    institution: Optional[str] = Field(None, description="Bank or provider the document is from, e.g. Neon, DKB, VIAC, AXA")
    as_of: Optional[str] = Field(None, description="Date the balances refer to, YYYY-MM-DD")
    accounts: list[FoundAccount] = Field(default_factory=list, description="One entry per account shown in the document")
    notes: str = Field("", description="One or two sentences: what the document is and anything uncertain")


SYSTEM = """You read personal finance documents (banking app screenshots, PDF statements, CSV exports \
from banks, brokers and Swiss pension providers) and extract the current value of each account shown.

- A document can show several accounts of one bank: a current account, savings pots or "Spaces", a credit \
card, a depot, several 3a portfolios. Return one entry in accounts per account, each with its own balance. \
Do not add an extra entry for a grand total that only sums the listed accounts.
- balance is the value of the account on as_of: the account balance for a bank account, the total \
portfolio or pension value for a depot, pillar 3a or pillar 2 account. For pillar 2 statements \
(Vorsorgeausweis) use the current retirement savings (Altersguthaben / avoir de vieillesse), not projections. \
An outstanding credit card amount is a negative balance.
- For a CSV of transactions, use a stated closing balance (e.g. "Kontostand") if there is one; never \
compute a balance by summing transactions, leave balance null and say so in notes instead.
- For securities, read the position's market value and the current price per unit where shown. Banking \
apps often show the price per share under the name and the position value on the right; the percentage \
and amount since buy are gains, not prices. Gains are not needed.
- For a depot, set balance only when the document states the portfolio total; never add up positions.
- Swiss and German documents write numbers like 1'234.50 or 1.234,50 - return plain numbers.
- Only report what the document shows. Use null where something is not visible.
- Set account_id from the family's accounts only when it is clearly the same account (same institution, \
type and name or currency); give each account_id to at most one entry. Otherwise leave it null. Several \
family members can have accounts at the same bank: unless the document names the holder, prefer \
accounts of the person who uploaded it, then joint accounts.
- A depot's currency is only its reference currency; its securities often trade in other currencies (EUR, \
USD), so a different currency does not rule out a match. When the document doesn't name the bank, a \
securities overview most likely belongs to the family's existing depot (of the uploader, if several)."""


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


def extract(path: Path, media_type: str, accounts: list[dict], uploaded_by: str | None = None,
            client: anthropic.Anthropic | None = None, task: str = "extract") -> Extraction:
    account_list = json.dumps(
        [{"id": a["id"], "name": a["name"], "institution": a["institution"], "kind": a["kind"],
          "currency": a["currency"], "owner": a.get("owner", "")} for a in accounts],
        ensure_ascii=False,
    )
    who = f"This file was uploaded by {uploaded_by}. " if uploaded_by else ""
    if client is None:
        if not os.environ.get("ANTHROPIC_API_KEY"):
            raise ExtractError("Claude API key missing or invalid: set ANTHROPIC_API_KEY in .env")
        client = anthropic.Anthropic()
    try:
        model, effort = config.model_for(task)
        response = client.messages.parse(
            model=model,
            output_config={"effort": effort},
            max_tokens=16000,
            system=SYSTEM,
            messages=[{
                "role": "user",
                "content": content_blocks(path, media_type) + [
                    {"type": "text", "text": f"The family's accounts: {account_list}\n\n{who}Extract the value of each account in this file."},
                ],
            }],
            output_format=Extraction,
        )
        usage.record(task, model, response.usage)
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
    known, used = {a["id"] for a in accounts}, set()
    for found in result.accounts:
        if found.account_id not in known or found.account_id in used:
            found.account_id = None
        used.add(found.account_id)
    return result
