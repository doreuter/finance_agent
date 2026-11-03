# FinanceAgent

One place for all accounts: Neon (cash + Neon Invest), DKB, VIAC (pillar 3a) and AXA (pillar 2).
Net worth in CHF, the split by pillar, and the history over time. Runs locally on the Mac.

## How data gets in

| Account | How | Effort |
|---|---|---|
| Neon Invest (depot) | Enter the positions once (ticker + quantity). Prices and FX come from Yahoo Finance every 6 hours. | Only after buying/selling |
| Neon, DKB, VIAC | Screenshot or statement → Inbox (web page or iPhone Shortcut). Claude reads the balance; you confirm. | ~1 minute a month |
| AXA (pillar 2) | Vorsorgeausweis PDF → Inbox, or type the number. | Once a year |

Every upload waits in the **Inbox** until you click *Save to account*: nothing from a file is booked without review.
A statement that lists fund positions (e.g. a Neon Invest PDF) can replace the depot's positions; tickers are looked up by ISIN.

## Run

```bash
cd ~/Documents/Test/finance-agent
cp .env.example .env        # then put your ANTHROPIC_API_KEY in it
.venv/bin/uvicorn app.main:app --host 127.0.0.1 --port 8765
```

Open http://127.0.0.1:8765, go to **Accounts** and click *Create my accounts*.

Data lives in `data/finance.db` (SQLite) and `data/uploads/`. Neither is in git.

## iPhone Shortcut (upload a screenshot in two taps)

Works once the Mac is reachable from the phone, e.g. with [Tailscale](https://tailscale.com) on both devices.
Then set `APP_TOKEN` in `.env` to a long random string and start uvicorn with `--host 0.0.0.0`.

In the Shortcuts app, create a shortcut "To FinanceAgent":

1. Shortcut settings → *Show in Share Sheet*, accept Images, PDFs and Files.
2. *Convert Image* → JPEG (only needed for HEIC photos; screenshots are PNG already).
3. *Get Contents of URL*: `http://<mac-tailscale-name>:8765/api/upload`, method **POST**,
   header `Authorization: Bearer <APP_TOKEN>`, request body **Form**, field `file` = Shortcut Input.
4. *Show Notification* with the result.

Then: screenshot in the VIAC or Neon app → Share → *To FinanceAgent*. The item shows up in the Inbox.

## Tests

```bash
.venv/bin/python -m pytest -q
```

## Next steps

- Phase 2: DKB automatically via FinTS (`python-fints`).
- Phase 3: daily digest — news about the holdings and a short summary by email/Telegram.
