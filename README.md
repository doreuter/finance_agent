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
- Phase 3: daily digest — news about the holdings and a short summary by email/Telegram.
