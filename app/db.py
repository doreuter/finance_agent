"""SQLite storage for the family's users, accounts, balances, holdings, prices and uploaded statements."""

from __future__ import annotations

import os
import sqlite3
from contextlib import contextmanager
from pathlib import Path

from .config import ROOT

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (  -- family members; everyone sees the whole family's accounts
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    username TEXT NOT NULL UNIQUE COLLATE NOCASE,
    password_hash TEXT NOT NULL,
    upload_token_hash TEXT,  -- sha256 of the personal token the iPhone Shortcut uploads with
    created_at TEXT DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS sessions (
    token_hash TEXT PRIMARY KEY,  -- sha256 of the cookie value
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    expires_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS accounts (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    institution TEXT NOT NULL,
    kind TEXT NOT NULL CHECK (kind IN ('cash', 'depot', 'pillar3a', 'pillar2')),
    currency TEXT NOT NULL DEFAULT 'CHF',
    -- 'balance': value = latest balance entered/imported; 'holdings': value = positions x prices
    valuation TEXT NOT NULL DEFAULT 'balance' CHECK (valuation IN ('balance', 'holdings')),
    archived INTEGER NOT NULL DEFAULT 0,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS balances (  -- an account's value on a day, in the account currency
    id INTEGER PRIMARY KEY,
    account_id INTEGER NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    day TEXT NOT NULL,
    amount REAL NOT NULL,
    source TEXT NOT NULL DEFAULT 'manual',  -- manual | upload
    note TEXT,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_balances_account_day ON balances(account_id, day);
CREATE TABLE IF NOT EXISTS holdings (  -- current positions of a 'holdings' account
    id INTEGER PRIMARY KEY,
    account_id INTEGER NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    name TEXT NOT NULL,
    symbol TEXT,          -- Yahoo Finance ticker, e.g. VT or CHSPI.SW; empty = no price feed
    isin TEXT,
    quantity REAL NOT NULL,
    manual_price REAL,    -- used when there is no symbol (e.g. cash in the depot: quantity = amount, price 1)
    currency TEXT NOT NULL DEFAULT 'CHF',
    updated_at TEXT DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS prices (  -- daily closes from Yahoo Finance; FX pairs are stored as e.g. EURCHF=X
    symbol TEXT NOT NULL,
    day TEXT NOT NULL,
    close REAL NOT NULL,
    currency TEXT,
    PRIMARY KEY (symbol, day)
);
CREATE TABLE IF NOT EXISTS daily_values (  -- what each account was worth, recorded by the refresh job
    day TEXT NOT NULL,
    account_id INTEGER NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    value REAL NOT NULL,       -- account currency
    value_base REAL NOT NULL,  -- base currency (CHF)
    PRIMARY KEY (day, account_id)
);
CREATE TABLE IF NOT EXISTS uploads (  -- statements, CSVs and screenshots waiting for review
    id INTEGER PRIMARY KEY,
    filename TEXT NOT NULL,
    media_type TEXT NOT NULL,
    path TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',  -- pending | applied | dismissed | failed
    extracted_json TEXT,
    error TEXT,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP
);
"""


def db_path() -> Path:
    return Path(os.environ.get("FINANCE_DB", ROOT / "data" / "finance.db"))


def uploads_dir() -> Path:
    return db_path().parent / "uploads"


# Columns added after the first release; init() adds them to an existing database.
MIGRATIONS = {
    "accounts": {"owner_id": "INTEGER REFERENCES users(id) ON DELETE SET NULL"},  # NULL = joint
    "uploads": {"user_id": "INTEGER REFERENCES users(id) ON DELETE SET NULL"},
}


def init() -> None:
    db_path().parent.mkdir(parents=True, exist_ok=True)
    uploads_dir().mkdir(parents=True, exist_ok=True)
    with connect() as conn:
        conn.executescript(SCHEMA)
        for table, columns in MIGRATIONS.items():
            have = {r["name"] for r in rows(conn, f"PRAGMA table_info({table})")}
            for column, definition in columns.items():
                if column not in have:
                    conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")


@contextmanager
def connect():
    conn = sqlite3.connect(db_path())
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def rows(conn: sqlite3.Connection, sql: str, *args) -> list[dict]:
    return [dict(r) for r in conn.execute(sql, args).fetchall()]


def row(conn: sqlite3.Connection, sql: str, *args) -> dict | None:
    r = conn.execute(sql, args).fetchone()
    return dict(r) if r else None
