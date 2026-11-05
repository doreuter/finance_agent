# FinanceAgent

One place for the family's accounts: Neon (cash + Neon Invest), DKB, VIAC (pillar 3a) and AXA (pillar 2),
grouped by bank. Net worth in CHF, the split by pillar, and the history over time, for the whole family
or per person. Each family member has their own login and sees all accounts; every account belongs to
one person or is joint.

## How data gets in

| Account | How | Effort |
|---|---|---|
| Neon Invest (depot) | Enter the positions once (ticker + quantity). Prices and FX come from Yahoo Finance every 6 hours. | Only after buying/selling |
| Neon, DKB, VIAC | Screenshot or statement → Inbox (web page or iPhone Shortcut). Claude reads the balance; you confirm. | ~1 minute a month |
| AXA (pillar 2) | Vorsorgeausweis PDF → Inbox, or type the number. | Once a year |

Every upload waits in the **Inbox** until you click *Save to account*: nothing from a file is booked without review.
A statement that lists fund positions (e.g. a Neon Invest PDF) can replace the depot's positions; tickers are looked up by ISIN.

## Insights: reminders and the weekly briefing

The **Insights** tab collects what is worth acting on:

- **To do:** reminders from fixed rules for pillar 3a (pay in early in the year, the 31 December deadline,
  catching up a missed year since 2025) and pillar 2 (upload the pension certificate, consider a buy-in
  from September), plus portfolio actions suggested by the briefing. Each can be marked done, snoozed
  (1 week to 6 months) or dismissed; a snoozed one comes back on its date. The 3a limits live in
  `app/advice.py` (`THREE_A_MAX`); add each new year there.
- **Pillar 2 & 3a:** per person, how much of this year's 3a maximum is paid and the pillar 2 buy-in
  potential. Fill in year of birth and employment under **Family**; 3a payments under **Accounts** (or a
  VIAC statement upload fills in the year's total; the AXA certificate fills in the buy-in potential).
- **Weekly briefing:** every Monday Claude searches the web for the week's news on the holdings and the
  market, then writes a view per position (buy / hold / reduce / watch, with reasoning for a long-term
  horizon), points to consider, and up to three portfolio tasks. About $0.50 per briefing.
  `WEEKLY_BRIEFING=0` turns the automatic run off; *Write now* runs it on demand.

- **Monthly savings:** the ETF savings plans per depot (enter them under Accounts, or upload a screenshot
  of the savings plans screen): how much goes in per month and year, per fund and person, how new money is
  split compared with what the depot already holds, and a 30-year illustration at 2/5/7% a year.
- **Taxes:** every account and position on 31 December (CSV for the securities list), foreign accounts and
  pension money marked, the year's 3a and pillar 2 deductions, and per position how distributions are taxed
  (Swiss 35% withholding tax reclaimable, US via DA-1, Irish/Luxembourg funds). From January to April,
  to-dos remind you of the return, withholding tax, foreign accounts; from 40, of staggering 3a. Ways to
  save tax: ask in **Ask**.

**Ask** is a chat with Claude that knows the family's data and searches the web for current rules
(about $0.08 per question, 40 questions a day per person). The conversation stays in the browser tab.

Briefing, proposals and chat inform your own decisions; they are not financial or tax advice.

## Claude models and cost

Each task uses the cheapest model that does it well (`TASK_MODELS` in `app/config.py`), measured on real
documents in November 2026:

| Task | Model | Why | Cost |
|---|---|---|---|
| Reading an upload | Haiku 5.5 | Read a Neon screenshot exactly like Opus (5/5 positions, right account), 33× cheaper | ~$0.001 |
| *Try again* on an upload | Opus 5.5 | A second reading after a failed or poor one; Sonnet missed the account match in the test | ~$0.05 |
| Briefing: web research | Sonnet 5.5 | Input-heavy (search results); Haiku ran out of searches and left gaps | ~$0.15 (up to 8 searches, cached) |
| Briefing: writing | Opus 5.5, effort high | The judgment you act on; small input, so the best model costs little | ~$0.14 |
| Savings plan proposal | Sonnet 5.5 researches the broker, Opus 5.5 proposes | The broker's offer is reused for 30 days, so a new proposal usually costs only the writing | ~$0.12, or ~$0.30 with fresh research |
| Ask (chat) | Sonnet 5.5 with web search | Good answers with up to 3 searches; per question | ~$0.04 |

About $2 a month with weekly briefings. **Family → Claude usage** shows this month's cost per task.

Keeping it low:
- **Monthly budget** (`CLAUDE_MONTHLY_BUDGET_USD`, default 10; 0 = none): once reached, briefings, proposals
  and Ask pause until the 1st; uploads keep working.
- **Prompt caching** on all web research: each search step re-reads the results before it, and cached
  re-reads cost a tenth.
- **One automatic briefing a week:** one that failed or was cut off by a restart (e.g. a deploy) is not
  started over; the next comes the following Monday, or press *Write now*.
Override per task with e.g. `CLAUDE_MODEL_EXTRACT=claude-sonnet-5-5` or `CLAUDE_EFFORT_BRIEFING=medium`.

## Security

- Logins: scrypt password hashes, server-side sessions (HttpOnly, SameSite, Secure over HTTPS), 5 failed
  attempts per username in 15 minutes (also for the setup token), same response time for unknown usernames.
- Requests that change data from another site are refused (Origin / Sec-Fetch-Site check, on top of SameSite).
- Uploads: only screenshots (PNG/JPEG/WebP/GIF), PDF and CSV; files are served from the uploads folder only,
  in a sandbox (no scripts), CSV as plain text.
- Strict Content-Security-Policy, no framing, HSTS.
- Claude spending is capped: 30 uploads an hour per person, 10 second readings an hour, 3 briefings and
  3 savings plan proposals a day per person, 40 chat questions a day per person.
- The upload token for the iPhone Shortcut can only upload; replace it under Family if a phone is lost.

## Run locally

```bash
cd ~/Documents/Test/finance-agent
cp .env.example .env        # then put your ANTHROPIC_API_KEY in it
.venv/bin/uvicorn app.main:app --host 127.0.0.1 --port 8765
```

Open http://127.0.0.1:8765. The first visit asks you to create your login; then go to **Accounts** and
click *Create my accounts*. Add family members under **Family**.

Data lives in `data/finance.db` (SQLite) and `data/uploads/`. Neither is in git.

## Deploy on Render (access from every device)

1. Render dashboard → **New → Blueprint** → this repository. `render.yaml` creates the web service
   (Frankfurt, Starter plan) with a 1 GB disk, about $7.25 a month.
2. When asked, paste a new `ANTHROPIC_API_KEY` made just for this app.
3. Open the service's **Environment** tab and copy `SETUP_TOKEN`.
4. Open the app's address. Create your login with that setup token. To bring over what you entered
   locally, choose your Mac's `data/finance.db` under *Import data* (do it now: importing only works
   while the app has no logins). Your accounts then belong to you.
5. **Family** → *Add a family member* for your wife, and tell her the password in person; she can change it.

Download a backup under **Family → Data** from time to time. Render's disk has no automatic backups.
Uploaded files stay on the server's disk; the backup file holds the database only.

## iPhone Shortcut (upload a screenshot in two taps)

Each family member makes their own: **Family → Create my upload token**. The token can only upload
files, nothing else; uploads show who sent them.

In the Shortcuts app, create a shortcut "To FinanceAgent":

1. Shortcut settings → *Show in Share Sheet*, accept Images, PDFs and Files.
2. *Convert Image* → JPEG (only needed for HEIC photos; screenshots are PNG already).
3. *Get Contents of URL*: `https://<your-app>/api/upload`, method **POST**,
   header `Authorization: Bearer <your upload token>`, request body **Form**, field `file` = Shortcut Input.
4. *Show Notification* with the result.

Then: screenshot in the VIAC or Neon app → Share → *To FinanceAgent*. The item shows up in the Inbox.

## Tests

```bash
.venv/bin/python -m pytest -q
```

## Next steps

- Phase 2: DKB automatically via FinTS (`python-fints`).
- Briefing by email.
