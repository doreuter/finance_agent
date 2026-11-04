"""Pillar 2 / 3a reminders from fixed rules, and the task list they share with the weekly briefing.

Tasks have a stable key, so a rule or a briefing never creates the same task twice; a task the user
marked done or not relevant stays that way, and a snoozed one comes back on its date.
"""

from __future__ import annotations

from datetime import date, timedelta

from . import db

# Maximum deductible pillar 3a payment for someone employed with a pension fund (CHF). Adjusted by the
# Federal Council every year or two; add the new year here. Without a pension fund it is 20% of net
# income, up to THREE_A_MAX_SELF_EMPLOYED.
THREE_A_MAX = {2025: 7258, 2026: 7258}
THREE_A_MAX_SELF_EMPLOYED = {2025: 36288, 2026: 36288}
CATCH_UP_FROM = 2025  # missed 3a years can be paid in later, but only gaps from 2025 onwards


def three_a_max(year: int, has_pension_fund: int | None) -> float | None:
    """The limit for an employee; None without a pension fund (it depends on income)."""
    if has_pension_fund == 0:
        return None
    known = [y for y in THREE_A_MAX if y <= year] or [min(THREE_A_MAX)]
    return THREE_A_MAX[max(known)]


def paid(conn, account_ids: list[int], year: int) -> float:
    """Paid into these accounts in `year`: a statement's year-to-date total, plus manual payments after it."""
    total = 0.0
    for account_id in account_ids:
        rows = db.rows(conn, "SELECT * FROM contributions WHERE account_id = ? AND substr(day, 1, 4) = ? ORDER BY day, id",
                       account_id, str(year))
        statement = next((r for r in reversed(rows) if r["source"] == "upload"), None)
        since = statement["day"] if statement else ""
        total += (statement["amount"] if statement else 0) + sum(
            r["amount"] for r in rows if r["source"] != "upload" and r["day"] > since)
    return total


def has_contributions(conn, account_ids: list[int], year: int) -> bool:
    marks = ",".join("?" * len(account_ids))
    return bool(account_ids) and db.row(
        conn, f"SELECT 1 FROM contributions WHERE account_id IN ({marks}) AND substr(day, 1, 4) = ?",
        *account_ids, str(year)) is not None


def pension_overview(today: date | None = None) -> list[dict]:
    """Per family member: 3a paid / limit / room left this year, and the pillar 2 buy-in potential."""
    today = today or date.today()
    out = []
    with db.connect() as conn:
        for u in db.rows(conn, "SELECT * FROM users ORDER BY id"):
            accounts = db.rows(conn, "SELECT id, kind FROM accounts WHERE owner_id = ? AND archived = 0", u["id"])
            three_a = [a["id"] for a in accounts if a["kind"] == "pillar3a"]
            limit = three_a_max(today.year, u["has_pension_fund"])
            done = paid(conn, three_a, today.year)
            out.append({
                "user_id": u["id"], "name": u["name"], "birth_year": u["birth_year"],
                "has_pension_fund": u["has_pension_fund"], "has_3a": bool(three_a),
                "has_pillar2": any(a["kind"] == "pillar2" for a in accounts),
                "year": today.year, "paid_3a": done, "max_3a": limit,
                "room_3a": max(limit - done, 0) if limit is not None else None,
                "pillar2_buyin": u["pillar2_buyin"], "pillar2_buyin_as_of": u["pillar2_buyin_as_of"],
                "three_a_accounts": len(three_a), "canton": u["canton"], "municipality": u["municipality"],
                "married": u["married"], "children": u["children"], "taxable_income": u["taxable_income"],
            })
    return out


def _upsert(conn, key: str, user_id: int | None, category: str, title: str, body: str, source: str = "rule") -> None:
    """Create the task once; later runs only refresh the text of a task that is still open."""
    existing = db.row(conn, "SELECT * FROM tasks WHERE key = ?", key)
    if existing is None:
        conn.execute("INSERT INTO tasks (key, user_id, category, title, body, source) VALUES (?, ?, ?, ?, ?, ?)",
                     (key, user_id, category, title, body, source))
    elif existing["status"] == "open" and (existing["title"], existing["body"]) != (title, body):
        conn.execute("UPDATE tasks SET title = ?, body = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                     (title, body, existing["id"]))


def _close(conn, key: str) -> None:
    """The reason for a rule's task is gone (e.g. 3a paid in full): mark it done."""
    conn.execute("UPDATE tasks SET status = 'done', updated_at = CURRENT_TIMESTAMP "
                 "WHERE key = ? AND status IN ('open', 'snoozed')", (key,))


def chf(amount: float) -> str:
    return f"CHF {amount:,.0f}".replace(",", "'")


def sync_tasks(today: date | None = None) -> None:
    today = today or date.today()
    year = today.year
    with db.connect() as conn:
        conn.execute("UPDATE tasks SET status = 'open', snooze_until = NULL, updated_at = CURRENT_TIMESTAMP "
                     "WHERE status = 'snoozed' AND snooze_until <= ?", (today.isoformat(),))
        for u in db.rows(conn, "SELECT * FROM users"):
            accounts = db.rows(conn, "SELECT id, kind FROM accounts WHERE owner_id = ? AND archived = 0", u["id"])
            three_a = [a["id"] for a in accounts if a["kind"] == "pillar3a"]
            if three_a:
                _three_a_tasks(conn, u, three_a, today)
                _stagger_task(conn, u, len(three_a), today)
            if any(a["kind"] == "pillar2" for a in accounts):
                _pillar2_tasks(conn, u, year, today)
        if today.month <= 4 and db.row(conn, "SELECT 1 FROM users LIMIT 1"):
            _tax_return_tasks(conn, year - 1)


def _three_a_tasks(conn, u: dict, accounts: list[int], today: date) -> None:
    year, uid, name = today.year, u["id"], u["name"]
    limit = three_a_max(year, u["has_pension_fund"])
    done = paid(conn, accounts, year)
    pay_key, deadline_key = f"3a-pay-{year}-{uid}", f"3a-deadline-{year}-{uid}"
    if limit is None:  # without a pension fund the limit depends on income; remind without an amount
        room_text = (f"Without a pension fund you can pay in up to 20% of your net income, at most "
                     f"{chf(THREE_A_MAX_SELF_EMPLOYED.get(year, max(THREE_A_MAX_SELF_EMPLOYED.values())))}.")
    elif done >= limit:
        _close(conn, pay_key)
        _close(conn, deadline_key)
        return
    else:
        room_text = (f"Recorded so far in {year}: {chf(done)} of {chf(limit)}, so {chf(limit - done)} can still "
                     f"be paid in and deducted from taxable income."
                     + ("" if u["has_pension_fund"] is not None else " (Assumes you're employed with a pension "
                        "fund; set this under Family.)"))
    if today.month <= 10:
        _upsert(conn, pay_key, uid, "pillar3a", f"Pay into pillar 3a for {year} ({name})",
                f"{room_text} Paying early in the year gives the money the whole year invested; it only has to "
                f"arrive before 31 December. Record payments under Accounts → your 3a account.")
    else:
        _upsert(conn, deadline_key, uid, "pillar3a", f"Pillar 3a deadline 31 December ({name})",
                f"{room_text} Payments must reach the 3a provider by 31 December, so transfer by mid-December. "
                f"After that the {year} deduction is lost (from {CATCH_UP_FROM} on, a gap can be paid in later "
                f"as a catch-up, but only on top of a full payment in that later year).")
    for missed in range(max(CATCH_UP_FROM, year - 10), year):
        limit_then = three_a_max(missed, u["has_pension_fund"])
        if limit_then is None or not has_contributions(conn, accounts, missed):
            continue  # unknown whether a gap exists
        gap = limit_then - paid(conn, accounts, missed)
        key = f"3a-catchup-{missed}-{uid}"
        if gap <= 0:
            _close(conn, key)
            continue
        _upsert(conn, key, uid, "pillar3a", f"Catch up the {missed} pillar 3a gap ({name})",
                f"In {missed} {chf(limit_then - gap)} of {chf(limit_then)} was paid in, a gap of {chf(gap)}. "
                f"Since 2025 such gaps can be paid in later (up to 10 years back), on top of the full payment "
                f"for the current year, and are tax-deductible in the year you pay them. Check the conditions "
                f"and how to declare it with your 3a provider.")


def _pillar2_tasks(conn, u: dict, year: int, today: date) -> None:
    uid, name = u["id"], u["name"]
    buyin = u["pillar2_buyin"]
    if buyin is None:
        _upsert(conn, f"p2-certificate-{year}-{uid}", uid, "pillar2", f"Upload your pension certificate ({name})",
                "Your pension fund's certificate (Vorsorgeausweis) shows how much you could buy into pillar 2. "
                "Upload it in the Inbox; the app reads the buy-in potential and can then remind you before "
                "year-end.")
        return
    _close(conn, f"p2-certificate-{year}-{uid}")
    if buyin > 0 and today.month >= 9:
        _upsert(conn, f"p2-buyin-{year}-{uid}", uid, "pillar2", f"Consider a pillar 2 buy-in for {year} ({name})",
                f"Your certificate shows a buy-in potential of {chf(buyin)}"
                + (f" (as of {u['pillar2_buyin_as_of']})" if u["pillar2_buyin_as_of"] else "") + ". "
                "A buy-in is deducted from taxable income in full; spreading it over several years usually saves "
                "more tax. Points to weigh: for 3 years afterwards no lump-sum withdrawal is possible (also not "
                "for buying a home); the money is tied up until retirement; compare the fund's interest rate "
                "and coverage ratio with investing it yourself. Many pension funds need the payment by early "
                "or mid-December; ask yours for the deadline.")


def _stagger_task(conn, u: dict, accounts: int, today: date) -> None:
    """Pillar 3a is taxed when withdrawn, at a reduced rate that rises with the amount: several accounts
    withdrawn in different years usually cost less tax than one."""
    if not u["birth_year"] or today.year - u["birth_year"] < 40 or accounts > 1:
        return
    _upsert(conn, f"3a-stagger-{u['id']}", u["id"], "tax", f"Open a second pillar 3a account ({u['name']})",
            "Pillar 3a is taxed when you withdraw it, at a rate that rises with the amount, and an account can only be "
            "withdrawn in full. With two to five 3a accounts you can withdraw them in different years (from about "
            "five years before retirement) and usually pay noticeably less tax. New payments can go into a second "
            "account at your provider; existing money stays where it is.")


def _tax_return_tasks(conn, year: int) -> None:
    """January to April: the return for last year, with what it needs from this app."""
    _upsert(conn, f"tax-return-{year}", None, "tax", f"Prepare the {year} tax return",
            f"Insights → Taxes has the list of all accounts and positions on 31 December {year} (also as CSV for the "
            "securities list) and the year's pillar 3a and pillar 2 deductions. The usual deadline is 31 March; "
            "most cantons extend it on request.")
    holdings = db.rows(conn, "SELECT h.isin FROM holdings h JOIN accounts a ON a.id = h.account_id "
                             "WHERE a.archived = 0 AND a.kind NOT IN ('pillar2', 'pillar3a') AND h.isin IS NOT NULL")
    countries = {h["isin"][:2].upper() for h in holdings}
    if "CH" in countries:
        _upsert(conn, f"tax-vst-{year}", None, "tax", f"Reclaim the Swiss withholding tax for {year}",
                "You hold Swiss securities or funds: 35% withholding tax was deducted from their distributions. List "
                "them with their income in the securities list of the return and you get the 35% back in full.")
    if "US" in countries:
        _upsert(conn, f"tax-da1-{year}", None, "tax", f"Credit the US withholding tax for {year} (form DA-1)",
                "US securities (e.g. Vanguard VT) had US tax withheld on dividends. With form DA-1 in the return, the "
                "part allowed by the tax treaty is credited; small amounts may not be worth it.")
    foreign = db.rows(conn, "SELECT DISTINCT institution, country FROM accounts WHERE archived = 0 AND country != 'CH'")
    if foreign:
        banks = ", ".join(f'{f["institution"]} ({f["country"]})' for f in foreign)
        _upsert(conn, f"tax-foreign-{year}", None, "tax", f"Declare the foreign accounts for {year}",
                f"Accounts abroad are part of your taxable wealth too: {banks}. List them with their balance on "
                f"31 December {year} and any interest; the tax list shows the values in CHF.")


def set_status(task_id: int, action: str, days: int | None = None, today: date | None = None) -> dict | None:
    today = today or date.today()
    with db.connect() as conn:
        task = db.row(conn, "SELECT * FROM tasks WHERE id = ?", task_id)
        if not task:
            return None
        if action == "snooze":
            until = (today + timedelta(days=days or 7)).isoformat()
            conn.execute("UPDATE tasks SET status = 'snoozed', snooze_until = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                         (until, task_id))
        elif action in ("done", "dismissed", "open"):
            conn.execute("UPDATE tasks SET status = ?, snooze_until = NULL, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                         (action, task_id))
        return db.row(conn, "SELECT * FROM tasks WHERE id = ?", task_id)


def list_tasks() -> dict:
    with db.connect() as conn:
        rows = db.rows(conn, "SELECT t.*, u.name AS user_name FROM tasks t LEFT JOIN users u ON u.id = t.user_id "
                             "WHERE t.status IN ('open', 'snoozed') ORDER BY t.created_at DESC, t.id DESC")
    return {"open": [t for t in rows if t["status"] == "open"], "snoozed": [t for t in rows if t["status"] == "snoozed"]}
