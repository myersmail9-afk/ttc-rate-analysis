#!/usr/bin/env python3
"""
TTC Rate Analysis - Jobber GraphQL puller (full-fidelity refresh).

Pulls everything the dashboard needs:
  - Completed jobs (with visits, jobCosting)
  - Converted quotes (with title, sentAt, jobs)
  - Archived (lost) quotes
  - Upcoming jobs (pending pipeline)
  - Awaiting-response quotes
Then computes all aggregations: per-bidder, per-category, win/loss,
time-to-close, dollar-weighted predictive analysis.

Writes the result to docs/data.json. Designed to run nightly via
GitHub Actions OR locally.

Requires .env in the project root (or env vars) with:
    JOBBER_CLIENT_ID
    JOBBER_CLIENT_SECRET
    JOBBER_REFRESH_TOKEN
"""
import os, sys, json, time, re
from pathlib import Path
from datetime import datetime, timezone
from collections import defaultdict
import statistics
import urllib.request, urllib.error, urllib.parse

# ---------------------------------------------------------------------------
HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
DOCS = ROOT / "docs"
DATA_OUT = DOCS / "data.json"
ENV_PATH = ROOT / ".env"

JOBBER_GRAPHQL    = "https://api.getjobber.com/api/graphql"
JOBBER_TOKEN_URL  = "https://api.getjobber.com/api/oauth/token"
API_VERSION       = "2025-04-16"

# Date window. Update annually or compute relative.
DATE_AFTER  = "2025-09-01T00:00:00Z"
DATE_BEFORE = "2026-12-31T23:59:59Z"
COMPLETED_AFTER = "2026-01-01T00:00:00Z"

# ---------------------------------------------------------------------------
def load_env():
    """Read .env file AND environment variables. Env vars win."""
    env = {}
    if ENV_PATH.exists():
        for line in ENV_PATH.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line: continue
            k, _, v = line.partition("=")
            env[k.strip()] = v.strip().strip('"').strip("'")
    for k in ["JOBBER_CLIENT_ID","JOBBER_CLIENT_SECRET","JOBBER_REFRESH_TOKEN"]:
        if os.environ.get(k):
            env[k] = os.environ[k]
    return env

def refresh_access_token(env):
    """Trade refresh token for a fresh access token."""
    data = urllib.parse.urlencode({
        "client_id": env["JOBBER_CLIENT_ID"],
        "client_secret": env["JOBBER_CLIENT_SECRET"],
        "grant_type": "refresh_token",
        "refresh_token": env["JOBBER_REFRESH_TOKEN"],
    }).encode()
    req = urllib.request.Request(JOBBER_TOKEN_URL, data=data,
        headers={"Content-Type":"application/x-www-form-urlencoded"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            body = json.loads(r.read())
    except urllib.error.HTTPError as e:
        sys.exit(f"Token refresh failed: HTTP {e.code} - {e.read().decode()[:500]}")
    return body["access_token"]

def gql(token, query, variables=None, retries=20):
    """POST a GraphQL query. Handles throttling with backoff."""
    payload = json.dumps({"query":query,"variables":variables or {}}).encode()
    for attempt in range(retries):
        req = urllib.request.Request(JOBBER_GRAPHQL, data=payload,
            headers={
                "Authorization": f"Bearer {token}",
                "X-JOBBER-GRAPHQL-VERSION": API_VERSION,
                "Content-Type": "application/json",
            }, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                body = json.loads(r.read())
                if "errors" in body and any(e.get("extensions",{}).get("code")=="THROTTLED" for e in body["errors"]):
                    print(f"  throttled, backing off 20s...", flush=True)
                    time.sleep(30); continue
                if "errors" in body:
                    sys.exit(f"GraphQL errors: {json.dumps(body['errors'], indent=2)[:1000]}")
                return body
        except urllib.error.HTTPError as e:
            print(f"  HTTP {e.code}, retrying...", flush=True); time.sleep(10)
    sys.exit("Max retries exceeded")

def page_through(token, query, key, page_size=25, max_pages=80):
    """Generic pagination helper for connection-style GraphQL queries."""
    nodes = []
    cursor = None
    for page in range(1, max_pages+1):
        r = gql(token, query, {"cursor": cursor})
        d = r["data"][key]
        nodes.extend(d["nodes"])
        cost = r["extensions"]["cost"]
        print(f"  {key} page {page}: {len(d['nodes'])} (total {len(nodes)}) remaining={cost['throttleStatus']['currentlyAvailable']}", flush=True)
        if not d["pageInfo"]["hasNextPage"]: break
        cursor = d["pageInfo"]["endCursor"]
        time.sleep(2 if cost["throttleStatus"]["currentlyAvailable"] > 5000 else 15)
    return nodes

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def categorize(title):
    if not title: return "Other"
    t = title.lower()
    if re.search(r'\b(phc|plant health|spray|fungicide|fertiliz|deep root|injection|herbicide|insecticide)\b', t): return "PHC / Sprays"
    if "crane" in t: return "Crane work"
    if "removal" in t or "remove" in t or "fell" in t or "cut down" in t: return "Removals"
    if "prune" in t or "pruning" in t or "trim" in t or "thinning" in t: return "Pruning"
    if "stump" in t: return "Stump grinding"
    if "storm" in t or "emergency" in t or "hazard" in t: return "Storm / Hazard"
    if "planting" in t or "plant " in t: return "Planting"
    if "consult" in t or "estimate" in t or "assessment" in t: return "Consult / Assessment"
    return "Other"

def norm_bidder(name):
    if not name: return "No Bid"
    return {
        "Benjamin Ash (BASH)":"Ben Ash","David Thunell":"David Thunell",
        "Gabriel Dye":"Gabe Dye","Trevor Stevens":"Trevor Stevens","Jedediah Thorpe":"Jed Thorpe",
    }.get(name.strip(), name.strip())

def to_month(iso):
    if not iso: return (None, None, None)
    try:
        dt = datetime.fromisoformat(iso.replace("Z","+00:00"))
        return (dt.strftime("%B"), dt.month, dt.year)
    except: return (None, None, None)

MONTH_NUM = {m:i for i,m in enumerate(
    ["January","February","March","April","May","June","July","August","September","October","November","December"], 1)}

def days_between(a, b):
    if not a or not b: return None
    try:
        da = datetime.fromisoformat(a.replace("Z","+00:00"))
        db = datetime.fromisoformat(b.replace("Z","+00:00"))
        return round((db-da).total_seconds()/86400, 1)
    except: return None

def size_bucket(amount):
    if amount < 250: return "<250"
    if amount < 500: return "250-500"
    if amount < 1000: return "500-1k"
    if amount < 2000: return "1k-2k"
    if amount < 5000: return "2k-5k"
    return "5k+"

# ---------------------------------------------------------------------------
# Queries
# ---------------------------------------------------------------------------
Q_COMPLETED_JOBS = """
query Jobs($cursor: String) {
  jobs(first: 25, after: $cursor,
    filter: { completedAt: { after: "%s", before: "%s" } }) {
    nodes {
      id jobNumber title jobStatus completedAt createdAt jobberWebUri
      client { name }
      salesperson { name { full } }
      total invoicedTotal
      visits { totalCount }
      jobCosting { totalRevenue expenseCost labourCost labourDuration lineItemCost profitAmount profitPercentage totalCost }
      quote { quoteNumber amounts { total } }
    }
    pageInfo { hasNextPage endCursor }
  }
}
""" % (COMPLETED_AFTER, DATE_BEFORE)

Q_CONVERTED_QUOTES = """
query Quotes($cursor: String) {
  quotes(first: 10, after: $cursor,
    filter: { createdAt: { after: "%s", before: "%s" }, status: converted }) {
    nodes {
      quoteNumber title createdAt sentAt transitionedAt
      client { name } salesperson { name { full } }
      amounts { total }
      jobs { nodes { jobNumber jobCosting { totalRevenue } completedAt } }
    }
    pageInfo { hasNextPage endCursor }
  }
}
""" % (DATE_AFTER, DATE_BEFORE)

Q_LOST_QUOTES = """
query Lost($cursor: String) {
  quotes(first: 10, after: $cursor,
    filter: { createdAt: { after: "%s", before: "%s" }, status: archived }) {
    nodes {
      quoteNumber title createdAt sentAt transitionedAt
      client { name } salesperson { name { full } } amounts { total }
    }
    pageInfo { hasNextPage endCursor }
  }
}
""" % (DATE_AFTER, DATE_BEFORE)

Q_UPCOMING_JOBS = """
query Up($cursor: String) {
  jobs(first: 25, after: $cursor, filter: { status: upcoming }) {
    nodes {
      id jobNumber title jobStatus startAt endAt createdAt
      client { name } salesperson { name { full } }
      total visits { totalCount }
      quote { quoteNumber amounts { total } }
    }
    pageInfo { hasNextPage endCursor }
  }
}
"""

Q_AWAITING_QUOTES = """
query Aw($cursor: String) {
  quotes(first: 25, after: $cursor, filter: { status: awaiting_response }) {
    nodes {
      quoteNumber title createdAt sentAt
      client { name } salesperson { name { full } } amounts { total }
    }
    pageInfo { hasNextPage endCursor }
  }
}
"""

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main():
    env = load_env()
    for k in ["JOBBER_CLIENT_ID","JOBBER_CLIENT_SECRET","JOBBER_REFRESH_TOKEN"]:
        if not env.get(k):
            sys.exit(f"Missing {k} in .env or environment")

    print("Refreshing access token...", flush=True)
    token = refresh_access_token(env)

    print("Connection check:", flush=True)
    who = gql(token, "query { account { name } }")
    print(f"  Connected to: {who['data']['account']['name']}", flush=True)

    print("\nPulling completed jobs...", flush=True)
    completed_jobs = page_through(token, Q_COMPLETED_JOBS, "jobs")

    print("\nPulling converted quotes...", flush=True)
    converted_quotes = page_through(token, Q_CONVERTED_QUOTES, "quotes")

    print("\nPulling lost (archived) quotes...", flush=True)
    lost_quotes_raw = page_through(token, Q_LOST_QUOTES, "quotes")

    print("\nPulling upcoming jobs (pending pipeline)...", flush=True)
    upcoming_jobs = page_through(token, Q_UPCOMING_JOBS, "jobs")

    print("\nPulling awaiting-response quotes...", flush=True)
    awaiting_quotes = page_through(token, Q_AWAITING_QUOTES, "quotes")

    # ---- Build per-job records ----
    jobs = []
    for j in completed_jobs:
        jc = j.get("jobCosting") or {}
        sp = (j.get("salesperson") or {}).get("name", {}) or {}
        client = (j.get("client") or {})
        quote = j.get("quote") or {}
        qamts = (quote.get("amounts") or {})
        revenue = float(jc.get("totalRevenue") or 0)
        expenses = float(jc.get("expenseCost") or 0)
        labour   = float(jc.get("labourCost") or 0)
        hours    = float(jc.get("labourDuration") or 0) / 3600
        if hours <= 0: continue
        month, mnum, yr = to_month(j.get("completedAt") or j.get("createdAt"))
        quote_total = qamts.get("total")
        bid_err_d = round(revenue - quote_total, 2) if quote_total else None
        bid_err_p = round((revenue - quote_total)/quote_total, 4) if quote_total else None
        jobs.append({
            "job_num": j.get("jobNumber"),
            "jobber_id": j.get("id"),
            "jobber_uri": j.get("jobberWebUri"),
            "client": client.get("name",""),
            "bidder": norm_bidder(sp.get("full")),
            "bidder_raw": sp.get("full"),
            "category": categorize(j.get("title")),
            "title": j.get("title"),
            "expenses": round(expenses, 2),
            "labour_cost": round(labour, 2),
            "hours": round(hours, 2),
            "revenue": round(revenue, 2),
            "rate": round((revenue-expenses)/hours, 2),
            "true_rate": round((revenue-expenses-labour)/hours, 2),
            "profit_amount": round(jc.get("profitAmount") or 0, 2),
            "profit_pct": round(jc.get("profitPercentage") or 0, 2),
            "month": month, "month_num": mnum, "year": yr,
            "visit_count": (j.get("visits") or {}).get("totalCount"),
            "quote_number": quote.get("quoteNumber"),
            "quote_total": round(quote_total, 2) if quote_total else None,
            "bid_error_dollars": bid_err_d,
            "bid_error_pct": bid_err_p,
            "job_status": j.get("jobStatus"),
        })
    jobs.sort(key=lambda r: (r.get("month_num") or 99, r["bidder"], r["job_num"] or 0))

    # ---- Quote records (per-quote bid accuracy) ----
    job_to_visits = {j["job_num"]: j.get("visit_count") or 0 for j in jobs}
    quote_records = []
    for q in converted_quotes:
        total = (q.get("amounts") or {}).get("total")
        if not total or total <= 0: continue
        qjobs = (q.get("jobs") or {}).get("nodes") or []
        if not qjobs: continue
        actual = sum((j.get("jobCosting") or {}).get("totalRevenue") or 0 for j in qjobs)
        if actual <= 0: continue
        completion = [j.get("completedAt") for j in qjobs if j.get("completedAt")]
        completed = max(completion) if completion else None
        month, mnum, yr = to_month(completed)
        if mnum is None or yr != 2026: continue
        job_nums = [j.get("jobNumber") for j in qjobs]
        cat_counts = defaultdict(int)
        for jn in job_nums:
            cat_counts[next((jj["category"] for jj in jobs if jj["job_num"]==jn), "Other")] += 1
        category = max(cat_counts, key=cat_counts.get) if cat_counts else "Other"
        sp = (q.get("salesperson") or {}).get("name",{}) or {}
        quote_records.append({
            "quote_number": q.get("quoteNumber"),
            "client": (q.get("client") or {}).get("name",""),
            "bidder": norm_bidder(sp.get("full")),
            "category": category,
            "quote_total": round(total, 2),
            "actual_total": round(actual, 2),
            "bid_error_dollars": round(actual - total, 2),
            "bid_error_pct": round((actual-total)/total, 4),
            "jobs_count": len(qjobs),
            "job_numbers": job_nums,
            "visit_count": sum(job_to_visits.get(jn, 0) for jn in job_nums),
            "completed_at": completed,
            "sent_at": q.get("sentAt"),
            "created_at": q.get("createdAt"),
            "transitioned_at": q.get("transitionedAt"),
            "days_to_close": days_between(q.get("sentAt") or q.get("createdAt"), q.get("transitionedAt")),
            "month": month, "month_num": mnum, "year": yr,
        })

    # ---- Lost quotes ----
    lost_records = []
    for q in lost_quotes_raw:
        sp = (q.get("salesperson") or {}).get("name",{}) or {}
        total = (q.get("amounts") or {}).get("total") or 0
        lost_records.append({
            "quote_number": q.get("quoteNumber"),
            "client": (q.get("client") or {}).get("name",""),
            "bidder": norm_bidder(sp.get("full")),
            "category": categorize(q.get("title") or ""),
            "quote_total": round(total, 2),
            "created_at": q.get("createdAt"),
            "sent_at": q.get("sentAt"),
            "transitioned_at": q.get("transitionedAt"),
            "days_to_close": days_between(q.get("sentAt") or q.get("createdAt"), q.get("transitionedAt")),
        })

    # ---- Aggregations ----
    def agg(rows):
        hrs = sum(r["hours"] for r in rows)
        rev = sum(r["revenue"] for r in rows)
        exp = sum(r["expenses"] for r in rows)
        lab = sum(r["labour_cost"] for r in rows)
        return {
            "jobs": len(rows), "hours": round(hrs, 2), "revenue": round(rev, 2),
            "expenses": round(exp, 2), "labour_cost": round(lab, 2),
            "net_revenue": round(rev - exp, 2),
            "avg_rate": round((rev - exp) / hrs, 2) if hrs > 0 else 0,
            "true_rate": round((rev - exp - lab) / hrs, 2) if hrs > 0 else 0,
        }

    by_bidder = defaultdict(list)
    for r in jobs: by_bidder[r["bidder"]].append(r)
    bidder_summary = {}
    for b, rows in by_bidder.items():
        monthly = defaultdict(list)
        for r in rows:
            if r["month"]: monthly[r["month"]].append(r)
        bidder_summary[b] = {"overall": agg(rows), "by_month": {m: agg(rs) for m, rs in monthly.items()}}

    months_seen = sorted({r["month"] for r in jobs if r["month"]}, key=lambda m: MONTH_NUM.get(m, 99))
    month_summary = {m: agg([r for r in jobs if r["month"] == m]) for m in months_seen}

    # ---- Win/loss ----
    won_by_bc = defaultdict(int); won_d_by_bc = defaultdict(float)
    for q in quote_records:
        k = (q["bidder"], q["category"])
        won_by_bc[k] += 1
        won_d_by_bc[k] += q["quote_total"]
    lost_by_bc = defaultdict(int); lost_d_by_bc = defaultdict(float)
    for q in lost_records:
        k = (q["bidder"], q["category"])
        lost_by_bc[k] += 1
        lost_d_by_bc[k] += q["quote_total"]
    bidders_all = sorted({q["bidder"] for q in quote_records} | {q["bidder"] for q in lost_records})
    cats_all = sorted({q["category"] for q in quote_records} | {q["category"] for q in lost_records})
    win_loss_by_category = {}
    for b in bidders_all:
        win_loss_by_category[b] = {}
        for c in cats_all:
            w = won_by_bc.get((b,c),0); l = lost_by_bc.get((b,c),0); t = w+l
            if t < 1: continue
            win_loss_by_category[b][c] = {
                "won": w, "lost": l, "total": t,
                "win_rate": round(w/t, 4),
                "won_dollars": round(won_d_by_bc.get((b,c),0), 2),
                "lost_dollars": round(lost_d_by_bc.get((b,c),0), 2),
            }

    win_loss_summary = {}
    for b in bidders_all:
        w = sum(1 for q in quote_records if q["bidder"]==b)
        l = sum(1 for q in lost_records if q["bidder"]==b)
        wd = sum(q["quote_total"] for q in quote_records if q["bidder"]==b)
        ld = sum(q["quote_total"] for q in lost_records if q["bidder"]==b)
        t = w+l
        win_loss_summary[b] = {
            "won": w, "lost": l, "won_dollars": round(wd,2), "lost_dollars": round(ld,2),
            "win_rate": round(w/t, 4) if t else 0,
        }

    # ---- Predictions (dollar-weighted) ----
    bcs_sums = defaultdict(lambda: {"q":0.0,"a":0.0,"n":0})
    bc_sums  = defaultdict(lambda: {"q":0.0,"a":0.0,"n":0})
    cat_sums = defaultdict(lambda: {"q":0.0,"a":0.0,"n":0})
    bidder_sums = defaultdict(lambda: {"q":0.0,"a":0.0,"n":0})
    for q in quote_records:
        if q["quote_total"] <= 0: continue
        for d in [bcs_sums[(q["bidder"], q["category"], size_bucket(q["quote_total"]))],
                  bc_sums[(q["bidder"], q["category"])],
                  cat_sums[q["category"]],
                  bidder_sums[q["bidder"]]]:
            d["q"] += q["quote_total"]; d["a"] += q["actual_total"]; d["n"] += 1

    def predict(bidder, cat, qt):
        for key, d, conf, basis in [
            ((bidder,cat,size_bucket(qt)), bcs_sums.get((bidder,cat,size_bucket(qt))), "high", "bidder x category x size"),
            ((bidder,cat), bc_sums.get((bidder,cat)), "medium", "bidder x category"),
            (cat, cat_sums.get(cat), "low", "category only"),
            (bidder, bidder_sums.get(bidder), "low", "bidder only"),
        ]:
            if d and d["n"] >= 3 and d["q"] > 0:
                return d["a"]/d["q"], conf, basis, d["n"]
        return 1.0, "no data", "no prior", 0

    def make_recommendation(loss, cat, visit_count, confidence):
        actions = []
        risk = "low"
        if loss > 1000: risk = "high"; actions.append("Re-quote with David before sending crew")
        elif loss > 500: risk = "medium"; actions.append("Add extra crew capacity that day")
        elif loss > 250: risk = "medium"; actions.append("Schedule buffer time, likely to run long")
        if cat == "Crane work": actions.append("Crane jobs run wide of bid; David should sanity-check")
        if cat == "Removals" and loss > 100: actions.append("Removals run -12% across all bidders; build buffer into schedule")
        vc = visit_count or 0
        if vc > 2: actions.append(f"{vc} visits planned - confirm scope has not crept")
        if confidence in ("low","no data"): actions.append("Limited history for this bidder/category - bid with caution")
        if not actions: actions.append("On track based on history")
        return risk, actions

    predictions = []
    for j in upcoming_jobs:
        quote = j.get("quote") or {}
        qt = (quote.get("amounts") or {}).get("total") or j.get("total") or 0
        if not qt: continue
        bidder = norm_bidder((j.get("salesperson") or {}).get("name",{}).get("full"))
        cat = categorize(j.get("title"))
        visits = (j.get("visits") or {}).get("totalCount", 0)
        ratio, conf, basis, n = predict(bidder, cat, qt)
        pred = qt * ratio; loss = qt - pred
        risk, actions = make_recommendation(loss, cat, visits, conf)
        predictions.append({
            "type":"scheduled_job","reference":f"Job #{j.get('jobNumber')}",
            "job_number":j.get("jobNumber"),"quote_number":quote.get("quoteNumber"),
            "client":(j.get("client") or {}).get("name",""),"bidder":bidder,"category":cat,
            "title":j.get("title"),"quote_total":round(qt,2),
            "predicted_error_pct":round(ratio-1,4),"predicted_actual":round(pred,2),
            "expected_loss":round(loss,2),"visits_planned":visits,"start_at":j.get("startAt"),
            "confidence":conf,"basis":basis,"history_n":n,"risk":risk,"recommendations":actions,
        })
    for q in awaiting_quotes:
        qt = (q.get("amounts") or {}).get("total") or 0
        if not qt: continue
        bidder = norm_bidder((q.get("salesperson") or {}).get("name",{}).get("full"))
        cat = categorize(q.get("title"))
        ratio, conf, basis, n = predict(bidder, cat, qt)
        pred = qt * ratio; loss = qt - pred
        risk, actions = make_recommendation(loss, cat, None, conf)
        predictions.append({
            "type":"awaiting_response","reference":f"Quote #{q.get('quoteNumber')}",
            "quote_number":q.get("quoteNumber"),"client":(q.get("client") or {}).get("name",""),
            "bidder":bidder,"category":cat,"title":q.get("title"),"quote_total":round(qt,2),
            "predicted_error_pct":round(ratio-1,4),"predicted_actual":round(pred,2),
            "expected_loss":round(loss,2),"visits_planned":None,"sent_at":q.get("sentAt"),
            "created_at":q.get("createdAt"),"confidence":conf,"basis":basis,
            "history_n":n,"risk":risk,"recommendations":actions,
        })
    predictions.sort(key=lambda p: -p["expected_loss"])

    tq = sum(p["quote_total"] for p in predictions)
    tp = sum(p["predicted_actual"] for p in predictions)
    tl = sum(p["expected_loss"] for p in predictions)

    # ---- Final output ----
    output = {
        "meta": {
            "source": "Jobber GraphQL API (live)",
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "jobs_pulled": len(jobs),
            "quote_records_count": len(quote_records),
            "lost_quotes_count": len(lost_records),
            "predictions_count": len(predictions),
            "bidders": sorted(bidder_summary.keys()),
            "categories": sorted({j["category"] for j in jobs}),
            "months": months_seen,
            "target_rate_per_hour": None,
        },
        "jobs": jobs,
        "quote_records": quote_records,
        "lost_quotes": lost_records,
        "bidder_summary": bidder_summary,
        "month_summary": month_summary,
        "win_loss_summary": win_loss_summary,
        "win_loss_by_category": win_loss_by_category,
        "predictions": predictions,
        "predictions_summary": {
            "total_count": len(predictions),
            "total_quoted": round(tq, 2),
            "total_predicted": round(tp, 2),
            "total_expected_loss": round(tl, 2),
        },
    }

    DATA_OUT.parent.mkdir(parents=True, exist_ok=True)
    DATA_OUT.write_text(json.dumps(output, indent=2))
    print(f"\nWrote {DATA_OUT}")
    print(f"  {len(jobs)} jobs, {len(quote_records)} quote records, {len(lost_records)} lost quotes, {len(predictions)} predictions")

    # Re-embed data into the dashboard HTML so the page is self-contained
    html_path = DOCS / "index.html"
    if html_path.exists():
        html = html_path.read_text()
        compact = json.dumps(output, separators=(",",":"))
        new_html = re.sub(
            r'const DATA = \{.*?\};\nconst ALLOWED_BIDDERS',
            lambda m: 'const DATA = ' + compact + ';\nconst ALLOWED_BIDDERS',
            html, count=1, flags=re.DOTALL,
        )
        html_path.write_text(new_html)
        print(f"  Updated embedded DATA in {html_path}")

if __name__ == "__main__":
    main()
