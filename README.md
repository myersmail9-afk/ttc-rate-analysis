# TTC Rate Analysis — Live Dashboard

A self-updating bid analysis tool for Total Tree Care. Pulls jobs
from Jobber once a night, computes net $/hr by bidder, and serves
a webpage David can bookmark.

## What it is

A replacement for David's hand-maintained `2026 Rate Analysis`
Excel workbook. Same math, same fields, but:
- Source of truth is **Jobber**, not Excel (no copy-paste).
- Always current — refreshed automatically.
- Per-bidder views, click to drill in.
- Ready to add bid accuracy (quote vs. actual) once we layer the
  quote query on top.

## Architecture

```
Jobber GraphQL API
  ↓ (nightly)
scripts/jobber_pull.py
  ↓
docs/data.json
  ↓ (browser fetch)
docs/index.html  ← David's bookmark
```

Three files. That's the whole system.

## Files

- `docs/index.html` — the dashboard (vanilla HTML/JS + Chart.js
  from CDN). Bidder tabs, monthly trends, job tables, bid-accuracy
  placeholder.
- `docs/data.json` — current data, regenerated nightly.
- `scripts/jobber_pull.py` — pulls from Jobber, writes data.json.
- `.env` — credentials (gitignored).

## Setup (one-time)

1. Verify the email on the Jobber Developer Center signup (code
   `488410`).
2. Configure scopes on the Jobber app — read scopes only:
   clients, properties, jobs, quotes, invoices, payments,
   scheduled items, time sheets, expenses, custom field
   configurations, products and services.
3. Use **Test in GraphiQL** to do the OAuth grant against TTC's
   Jobber account. Capture the refresh token.
4. Drop the refresh token into `.env`:
   ```
   JOBBER_REFRESH_TOKEN=<your-refresh-token>
   ```
5. Smoke test: `python3 scripts/jobber_pull.py`

## Running the dashboard locally

```bash
cd docs && python3 -m http.server 8000
# open http://localhost:8000
```

(Or just double-click `docs/index.html` — the current build embeds
preview data inline so it works without a server.)

## Production

GitHub Pages serves `docs/`. A GitHub Action runs
`scripts/jobber_pull.py` at 2 AM and commits the updated
`data.json`. David bookmarks the Pages URL.

## Status

- ✅ Dashboard frontend built.
- ✅ Excel parsed as preview data so the layout is testable today.
- ✅ Jobber puller scaffold written (`scripts/jobber_pull.py`).
- ⏳ Refresh token — waiting on OAuth grant in dev center.
- ⏳ First real pull — once token is in `.env`.
- ⏳ Bid accuracy view — second-pass query for quote totals.
- ⏳ GitHub Pages deploy — when ready to publish.
