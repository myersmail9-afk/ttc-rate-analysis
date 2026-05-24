#!/usr/bin/env python3
"""
TTC Rate Analysis — Jobber GraphQL puller.

Runs nightly. Pulls completed/invoiced jobs from Jobber, computes the
same per-job fields David's Excel tracks (client, salesperson, expenses,
hours, revenue, net $/hr), and writes docs/data.json — the file the
dashboard reads.

Source of truth = Jobber. The Excel workbook was only a one-time spec
of what David wants to see; it's not in the data path anymore.

Usage:
    python3 jobber_pull.py

Requires .env in the project root with:
    JOBBER_CLIENT_ID=...
    JOBBER_CLIENT_SECRET=...
    JOBBER_REFRESH_TOKEN=...
"""
import os, sys, json, time
from pathlib import Path
from datetime import datetime, timezone
from collections import defaultdict

try:
    import requests
except ImportError:
    print("ERROR: pip install requests", file=sys.stderr); sys.exit(1)

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
DOCS = ROOT / "docs"
DATA_OUT = DOCS / "data.json"
ENV_PATH = ROOT / ".env"

JOBBER_GRAPHQL = "https://api.getjobber.com/api/graphql"
JOBBER_TOKEN_URL = "https://api.getjobber.com/api/oauth/token"
JOBBER_API_VERSION = "2024-04-01"  # bump as needed

PAGE_SIZE = 100  # Jobber typically caps at 100 per page

# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------
def load_env():
    """Read .env into a dict. Tiny parser so we don't need python-dotenv."""
    env = {}
    if not ENV_PATH.exists():
        sys.exit(f"ERROR: {ENV_PATH} missing. Create it with JOBBER_CLIENT_ID, "
                 "JOBBER_CLIENT_SECRET, JOBBER_REFRESH_TOKEN.")
    for line in ENV_PATH.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line: continue
        k, _, v = line.partition("=")
        env[k.strip()] = v.strip().strip('"').strip("'")
    return env

def refresh_access_token(env):
    """Trade refresh token for a fresh access token."""
    r = requests.post(JOBBER_TOKEN_URL, data={
        "client_id": env["JOBBER_CLIENT_ID"],
        "client_secret": env["JOBBER_CLIENT_SECRET"],
        "grant_type": "refresh_token",
        "refresh_token": env["JOBBER_REFRESH_TOKEN"],
    }, timeout=30)
    if r.status_code != 200:
        sys.exit(f"ERROR refreshing token: HTTP {r.status_code} — {r.text[:500]}")
    body = r.json()
    # If Jobber returned a new refresh token (rotation on), save it back to .env
    new_refresh = body.get("refresh_token")
    if new_refresh and new_refresh != env["JOBBER_REFRESH_TOKEN"]:
        _update_env_value("JOBBER_REFRESH_TOKEN", new_refresh)
    return body["access_token"]

def _update_env_value(key, value):
    """Replace a single key in .env without disturbing the rest."""
    text = ENV_PATH.read_text()
    out = []
    seen = False
    for line in text.splitlines():
        if line.startswith(f"{key}="):
            out.append(f"{key}={value}"); seen = True
        else:
            out.append(line)
    if not seen: out.append(f"{key}={value}")
    ENV_PATH.write_text("\n".join(out) + "\n")

# ---------------------------------------------------------------------------
# GraphQL
# ---------------------------------------------------------------------------
def gql(access_token, query, variables=None):
    """POST a GraphQL query, return data dict. Raises on userErrors or HTTP errors."""
    r = requests.post(JOBBER_GRAPHQL,
        headers={
            "Authorization": f"Bearer {access_token}",
            "X-JOBBER-GRAPHQL-VERSION": JOBBER_API_VERSION,
            "Content-Type": "application/json",
        },
        json={"query": query, "variables": variables or {}},
        timeout=60,
    )
    if r.status_code != 200:
        sys.exit(f"ERROR: HTTP {r.status_code} — {r.text[:500]}")
    body = r.json()
    if "errors" in body:
        sys.exit(f"GraphQL errors: {json.dumps(body['errors'], indent=2)}")
    return body["data"]

# ---------------------------------------------------------------------------
# Queries
# ---------------------------------------------------------------------------
# NOTE: Jobber's exact field names need to be verified against GraphiQL on first
# run. Adjust here once we confirm the schema (e.g. salesperson nesting,
# expense totals, time-sheet entry shape). Treat this query as a first draft.

QUERY_JOBS = """
query CompletedJobs($cursor: String, $first: Int!) {
  jobs(
    first: $first
    after: $cursor
    filter: { completed: true }
  ) {
    nodes {
      id
      jobNumber
      title
      jobStatus
      createdAt
      completedAt
      client { id name }
      property { id street1 city }
      salesperson { name }
      total
      jobBalanceTotals { invoicedTotal }
      expenses { nodes { total } }
      timeSheetEntries { nodes { startAt endAt finalDuration } }
      invoices { nodes { netTotal issuedAt } }
    }
    pageInfo { hasNextPage endCursor }
  }
}
"""

# ---------------------------------------------------------------------------
# Transform
# ---------------------------------------------------------------------------
def normalize_bidder(name):
    if not name: return "No Bid"
    name = name.strip()
    aliases = {
        "Benjamin Ash (BASH)": "Ben Ash",
        "Benjamin Ash": "Ben Ash",
        "David Thunell": "David Thunell",
        "Gabriel Dye": "Gabe Dye",
        "Trevor Stevens": "Trevor Stevens",
        "Jedediah Thorpe": "Jed Thorpe",
    }
    return aliases.get(name, name)

def to_month_label(iso_date):
    """'2026-05-15T...' -> 'May'"""
    if not iso_date: return None
    try:
        dt = datetime.fromisoformat(iso_date.replace("Z", "+00:00"))
        return dt.strftime("%B")
    except Exception:
        return None

def to_record(job):
    """Flatten a Jobber Job node into the per-job record the dashboard expects."""
    expenses = sum((e.get("total") or 0) for e in (job.get("expenses", {}).get("nodes", []) or []))
    # Hours: prefer finalDuration if Jobber provides it; otherwise compute from start/end
    hours = 0.0
    for t in (job.get("timeSheetEntries", {}).get("nodes", []) or []):
        if t.get("finalDuration"):
            hours += float(t["finalDuration"]) / 3600.0  # seconds → hours
        elif t.get("startAt") and t.get("endAt"):
            try:
                s = datetime.fromisoformat(t["startAt"].replace("Z","+00:00"))
                e = datetime.fromisoformat(t["endAt"].replace("Z","+00:00"))
                hours += (e - s).total_seconds() / 3600.0
            except Exception:
                pass
    revenue = sum((inv.get("netTotal") or 0) for inv in (job.get("invoices", {}).get("nodes", []) or []))
    if not revenue:
        # Fall back to the job's own total if no invoice yet
        revenue = job.get("total") or job.get("jobBalanceTotals", {}).get("invoicedTotal") or 0
    bidder = normalize_bidder((job.get("salesperson") or {}).get("name"))
    month = to_month_label(job.get("completedAt") or job.get("createdAt"))
    year = None
    try:
        year = datetime.fromisoformat((job.get("completedAt") or job.get("createdAt")).replace("Z","+00:00")).year
    except Exception:
        pass
    rate = (revenue - expenses) / hours if hours > 0 else 0
    return {
        "job_num": int(job.get("jobNumber")) if str(job.get("jobNumber") or "").isdigit() else job.get("jobNumber"),
        "jobber_id": job.get("id"),
        "client": (job.get("client") or {}).get("name", ""),
        "bidder": bidder,
        "bidder_raw": (job.get("salesperson") or {}).get("name"),
        "expenses": round(expenses, 2),
        "hours": round(hours, 2),
        "revenue": round(revenue, 2),
        "rate": round(rate, 2),
        "month": month,
        "month_num": _month_num(month),
        "year": year,
        # Bid accuracy — needs a separate query for the linked quote(s)
        "quote_total": None,
        "bid_error_dollars": None,
        "bid_error_pct": None,
    }

MONTH_NUM = {m: i for i, m in enumerate(
    ["January","February","March","April","May","June","July","August","September","October","November","December"], 1)}
def _month_num(name): return MONTH_NUM.get(name)

# ---------------------------------------------------------------------------
# Aggregation
# ---------------------------------------------------------------------------
def aggregate(records):
    by_bidder = defaultdict(list)
    for r in records:
        by_bidder[r["bidder"]].append(r)
    bidder_summary = {}
    for b, rows in by_bidder.items():
        by_month = defaultdict(list)
        for r in rows:
            if r["month"]: by_month[r["month"]].append(r)
        bidder_summary[b] = {
            "overall": _agg(rows),
            "by_month": {m: _agg(rs) for m, rs in by_month.items()},
        }
    # Per-month totals
    by_month_all = defaultdict(list)
    for r in records:
        if r["month"]: by_month_all[r["month"]].append(r)
    month_summary = {m: _agg(rs) for m, rs in by_month_all.items()}
    return bidder_summary, month_summary

def _agg(rows):
    hrs = sum(r["hours"] for r in rows)
    rev = sum(r["revenue"] for r in rows)
    exp = sum(r["expenses"] for r in rows)
    return {
        "jobs": len(rows),
        "hours": round(hrs, 2),
        "revenue": round(rev, 2),
        "expenses": round(exp, 2),
        "net_revenue": round(rev - exp, 2),
        "avg_rate": round((rev - exp) / hrs, 2) if hrs > 0 else 0,
    }

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main():
    env = load_env()
    if not env.get("JOBBER_REFRESH_TOKEN"):
        sys.exit("ERROR: JOBBER_REFRESH_TOKEN not set in .env. Complete the OAuth grant first.")
    print("Refreshing access token...")
    access = refresh_access_token(env)

    print("Sanity-checking the connection...")
    who = gql(access, "query { account { id name } }")
    print(f"  Connected to: {who['account']['name']}")

    print("Pulling completed jobs...")
    records = []
    cursor = None
    page = 0
    while True:
        page += 1
        data = gql(access, QUERY_JOBS, {"cursor": cursor, "first": PAGE_SIZE})
        nodes = data["jobs"]["nodes"]
        print(f"  Page {page}: {len(nodes)} jobs")
        for node in nodes:
            records.append(to_record(node))
        if not data["jobs"]["pageInfo"]["hasNextPage"]:
            break
        cursor = data["jobs"]["pageInfo"]["endCursor"]
        time.sleep(0.5)  # be polite to the rate limiter

    print(f"Total: {len(records)} jobs")
    bidder_summary, month_summary = aggregate(records)
    bidders = sorted(bidder_summary.keys())
    months = sorted({r["month"] for r in records if r["month"]},
                    key=lambda m: MONTH_NUM.get(m, 99))

    output = {
        "meta": {
            "source": "Jobber GraphQL API",
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "note": "Live pull from Jobber. Quote/bid accuracy fields are stub until quoteTotal query is added.",
            "bidders": bidders,
            "months": months,
            "target_rate_per_hour": None,
        },
        "jobs": records,
        "bidder_summary": bidder_summary,
        "month_summary": month_summary,
    }
    DATA_OUT.parent.mkdir(parents=True, exist_ok=True)
    DATA_OUT.write_text(json.dumps(output, indent=2))
    print(f"Wrote {DATA_OUT}")
    print("\nPer-bidder snapshot:")
    for b in sorted(bidder_summary.keys(), key=lambda x: -bidder_summary[x]["overall"]["revenue"]):
        s = bidder_summary[b]["overall"]
        print(f"  {b:<20} jobs={s['jobs']:>3}  hrs={s['hours']:>7.1f}  rev=${s['revenue']:>10,.2f}  rate=${s['avg_rate']:>7.2f}/hr")

if __name__ == "__main__":
    main()
