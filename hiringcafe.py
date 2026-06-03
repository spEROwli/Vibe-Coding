#!/usr/bin/env python3
"""
hiringcafe.py — Second pipeline: pull PM roles from Hiring Café's aggregated index.

Hiring Café (https://hiring.cafe) aggregates jobs across 46+ ATS platforms, so it
sees companies the fixed verified_companies.json list never will. This is a
*complement* to pmfarm.py, not a replacement — run both, dedupe the union.

  python3 hiringcafe.py                      # PM roles, NYC + remote-US
  python3 hiringcafe.py --query "product manager" --pages 5
  python3 hiringcafe.py --remote-only
  python3 hiringcafe.py --schema             # first-run: dump field paths of a
                                             # sample job so the extractor mapping
                                             # can be verified against real data

SAME HARD RULES AS SCRAPER_RULES.md:
  - Only emit what the live API returned this run. Raw response is saved to
    hiringcafe_raw.json every run so every emitted row is auditable.
  - Fields are copied verbatim. If a field isn't in the API object, the cell is
    left blank — never guessed, inferred, or completed.
  - years: the exact sentence containing "year" from the description, else
    "not stated". Never a number derived from title or seniority.

Must run locally (not in the Claude Code container) — hiring.cafe sits behind
Cloudflare and 403s any datacenter IP.
"""

import argparse, csv, json, sys, urllib.request
from urllib.error import HTTPError, URLError


class _PostPreservingRedirect(urllib.request.HTTPRedirectHandler):
    """urllib's default handler downgrades POST→GET on 301/302/303 redirects.
    hiring.cafe redirects /api/search-jobs (e.g. to add a trailing slash); the
    downgraded GET then hits a POST-only route and returns 405. This re-issues
    the redirect as POST with the original body intact."""
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return urllib.request.Request(
            newurl, data=req.data,
            headers=dict(req.header_items()),
            method="POST", origin_req_host=req.origin_req_host,
            unverifiable=True)


_OPENER = urllib.request.build_opener(_PostPreservingRedirect)

# Reuse the exact filtering/dedupe logic from the primary pipeline so both
# pipelines agree on what counts as an IC-level PM role in a viable location.
import pmfarm
from pmfarm import (
    _passes_title, _passes_location, _loc_class, _norm_url,
    _ct_key, _load_applied, _strip_html,
)

# Title variants to widen the net in --broad mode. Each is a separate API query;
# results are merged and deduped. Covers IC through senior/leadership PM titles.
BROAD_QUERIES = [
    "product manager", "product owner", "technical product manager",
    "senior product manager", "group product manager", "principal product manager",
    "director of product", "platform product manager", "growth product manager",
    "ai product manager",
]

API_JOBS  = "https://hiring.cafe/api/search-jobs"
RAW_FILE  = "hiringcafe_raw.json"
OUT_FILE  = "hiringcafe_roles.csv"

HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                   "AppleWebKit/537.36 (KHTML, like Gecko) "
                   "Chrome/130.0.0.0 Safari/537.36"),
    "Accept":       "application/json, text/plain, */*",
    "Content-Type": "application/json",
    "Referer":      "https://hiring.cafe/",
    "Origin":       "https://hiring.cafe",
}

# Candidate key names for each field, in priority order. hiring.cafe nests most
# data under job_information / v5_processed_job_data, so extraction does a
# recursive first-match search rather than assuming a fixed path. If none match,
# the field stays blank (rule: never guess).
FIELD_KEYS = {
    "title":    ["core_job_title", "job_title", "title", "position_name", "role_title"],
    "company":  ["company_name", "employer_name", "company", "organization", "org_name"],
    "location": ["formatted_workplace_location", "workplace_physical_location",
                 "workplace_location", "formatted_address", "location", "city"],
    "url":      ["apply_url", "apply_link", "job_url", "source_url", "url", "link"],
    "job_id":   ["id", "job_id", "_id", "uuid"],
    "desc":     ["requirements_summary", "job_description", "description",
                 "description_text", "jd"],
    "date":     ["estimated_publish_date", "posted_date", "date_posted",
                 "created_at", "publish_date"],
}


# ── extraction ────────────────────────────────────────────────────────────────

def _dig(obj, keys):
    """Recursive first-match: return the first non-empty value whose key is in
    `keys`, searching nested dicts/lists. Verbatim — no transformation."""
    if isinstance(obj, dict):
        for k in keys:
            v = obj.get(k)
            if isinstance(v, str) and v.strip():
                return v.strip()
            if isinstance(v, (int, float)):
                return str(v)
        for v in obj.values():
            found = _dig(v, keys)
            if found:
                return found
    elif isinstance(obj, list):
        for item in obj:
            found = _dig(item, keys)
            if found:
                return found
    return ""


def _years_sentence(desc: str) -> str:
    """Exact sentence containing 'year' from the description, else 'not stated'.
    Verbatim substring — no number is ever derived (SCRAPER_RULES)."""
    text = _strip_html(desc)
    for sentence in [s.strip() for s in text.replace("\n", " ").split(".")]:
        if "year" in sentence.lower():
            return sentence
    return "not stated"


def extract(job: dict) -> dict:
    title    = _dig(job, FIELD_KEYS["title"])
    company  = _dig(job, FIELD_KEYS["company"])
    location = _dig(job, FIELD_KEYS["location"])
    url      = _dig(job, FIELD_KEYS["url"])
    job_id   = _dig(job, FIELD_KEYS["job_id"])
    desc     = _dig(job, FIELD_KEYS["desc"])
    date     = _dig(job, FIELD_KEYS["date"])
    return {
        "source":    "HiringCafe",
        "company":   company,
        "title":     title,
        "location":  location,
        "loc_class": _loc_class(location, desc[:500]),
        "url":       url,
        "job_id":    job_id,
        "years":     _years_sentence(desc),
        "date":      date,
    }


# ── fetch ─────────────────────────────────────────────────────────────────────

def _search_state(query: str) -> dict:
    """Minimal valid searchState. Location is filtered client-side (the API's geo
    filter needs a full Google-Places object); we query broadly then filter."""
    return {
        "searchQuery":     query,
        "locations":       [],
        "workplaceTypes":  ["Remote", "Hybrid", "Onsite"],
        "commitmentTypes": ["Full Time"],
        "seniorityLevel":  [],
        "sortBy":          "default",
    }


def _post(url: str, body: bytes):
    """POST that survives redirects without method downgrade. Falls back to the
    trailing-slash variant on a 405 (the classic redirect-downgrade signature)."""
    req = urllib.request.Request(url, data=body, headers=HEADERS, method="POST")
    try:
        return _OPENER.open(req, timeout=20)
    except HTTPError as e:
        if e.code == 405 and not url.endswith("/"):
            req2 = urllib.request.Request(url + "/", data=body, headers=HEADERS, method="POST")
            return _OPENER.open(req2, timeout=20)
        raise


def fetch_page(query: str, page: int, size: int = 100) -> list:
    body = json.dumps({
        "size": size, "page": page, "searchState": _search_state(query),
    }).encode()
    with _post(API_JOBS, body) as r:
        data = json.loads(r.read())
    # Response shape isn't documented; jobs live under one of these keys.
    for key in ("results", "jobs", "data", "items", "content"):
        if isinstance(data, dict) and isinstance(data.get(key), list):
            return data[key]
    if isinstance(data, dict) and isinstance(data.get("hits"), dict):
        return data["hits"].get("hits", [])
    return data if isinstance(data, list) else []


def fetch_all(query: str, pages: int, save_raw: bool = True) -> list:
    all_jobs, raw = [], []
    for p in range(pages):
        try:
            batch = fetch_page(query, p)
        except HTTPError as e:
            print(f"  page {p}: HTTP {e.code} {e.reason}", file=sys.stderr)
            if e.code in (403, 401):
                print("  → hiring.cafe blocked this IP. Run locally, not in a "
                      "datacenter/container.", file=sys.stderr)
            break
        except URLError as e:
            print(f"  page {p}: {e.reason}", file=sys.stderr)
            break
        if not batch:
            break
        raw.extend(batch)
        all_jobs.extend(batch)
        print(f"  page {p}: {len(batch)} jobs")
        if len(batch) < 100:
            break
    # Save raw so every emitted row is auditable against this run's response.
    if save_raw:
        with open(RAW_FILE, "w", encoding="utf-8") as f:
            json.dump(raw, f, indent=2)
    return all_jobs


# ── schema probe (verification aid) ───────────────────────────────────────────

def probe(query: str):
    """Try endpoint/method variants and report each result + the server's `Allow`
    header (which on a 405 names exactly which methods the route accepts). One
    local run tells us definitively what hiring.cafe wants today."""
    body = json.dumps({"size": 1, "page": 0, "searchState": _search_state(query)}).encode()
    endpoints = [
        "https://hiring.cafe/api/search-jobs",
        "https://hiring.cafe/api/search-jobs/",
        "https://hiring.cafe/api/search-jobs/get-total-count",
        "https://hiring.cafe/api/jobs",
        "https://hiring.cafe/api/search",
    ]
    methods = ["POST", "GET"]
    for url in endpoints:
        for method in methods:
            data = body if method == "POST" else None
            req  = urllib.request.Request(url, data=data, headers=HEADERS, method=method)
            tag  = f"{method:4s} {url}"
            try:
                with _OPENER.open(req, timeout=20) as r:
                    payload = r.read(300)
                    print(f"  OK   {r.status}  {tag}")
                    print(f"        body: {payload[:200]!r}")
            except HTTPError as e:
                allow = e.headers.get("Allow") or e.headers.get("allow") or "—"
                server = e.headers.get("Server") or "—"
                print(f"  FAIL {e.code}  {tag}   Allow={allow}  Server={server}")
            except URLError as e:
                print(f"  ERR        {tag}  ({e.reason})")
    print("\nRead the 'Allow=' on any 405 — those are the methods that route accepts.")
    print("Use the first 'OK 200' line: its method + URL is what to wire in.")


def dump_schema(query: str):
    jobs = fetch_all(query, 1)
    if not jobs:
        print("No jobs returned — cannot probe schema.")
        return
    sample = jobs[0]
    print(f"\nSample job object (raw, first result):\n{json.dumps(sample, indent=2)[:3000]}")
    print("\nExtractor would read:")
    for k, v in extract(sample).items():
        print(f"  {k:10s} = {v!r}")
    print(f"\nFull raw saved to {RAW_FILE} ({len(jobs)} jobs).")


# ── main ──────────────────────────────────────────────────────────────────────

def run(queries: list, pages: int, remote_only: bool):
    jobs = []
    for q in queries:
        print(f"Querying hiring.cafe for '{q}' ({pages} page(s) max)…")
        jobs.extend(fetch_all(q, pages, save_raw=False))
    # One combined raw file across all queries = complete audit trail.
    with open(RAW_FILE, "w", encoding="utf-8") as f:
        json.dump(jobs, f, indent=2)
    total = len(jobs)
    print(f"\n{total} jobs returned by API across {len(queries)} query(ies)")

    rows = [extract(j) for j in jobs]

    # Filter: must have a usable title + url, pass the IC-PM title gate, and land
    # in a viable location. Drop anything missing a url (can't apply, can't audit).
    kept, dropped_title, dropped_loc, dropped_blank = [], 0, 0, 0
    for r in rows:
        if not r["title"] or not r["url"]:
            dropped_blank += 1
            continue
        if not _passes_title(r["title"]):
            dropped_title += 1
            continue
        if not _passes_location(r["loc_class"], remote_only):
            dropped_loc += 1
            continue
        kept.append(r)

    # Dedupe within this run (aggregators repeat the same job from multiple ATSs)
    # and against applied.csv.
    applied = _load_applied()
    seen, deduped, skipped_applied = set(), [], 0
    for r in kept:
        nu = _norm_url(r["url"])
        ck = _ct_key(r["company"], r["title"])
        if nu in applied or ck in applied:
            skipped_applied += 1
            continue
        if nu in seen:
            continue
        seen.add(nu)
        deduped.append(r)

    # ── manifest (SCRAPER_RULES) ──
    import datetime
    print("\n" + "─" * 74)
    print("HIRING CAFÉ PIPELINE — MANIFEST")
    print(f"  run:            {datetime.datetime.now().isoformat(timespec='seconds')}")
    print(f"  queries:        {queries}   remote_only={remote_only}")
    print(f"  API returned:   {total}")
    print(f"  dropped blank:  {dropped_blank} (no title or no apply url)")
    print(f"  dropped title:  {dropped_title} (not IC-level PM)")
    print(f"  dropped loc:    {dropped_loc} (not NYC/remote-US)")
    print(f"  skipped applied:{skipped_applied}")
    print(f"  EMITTED:        {len(deduped)}")
    print("  Every role below was returned by a live API call in this run. No")
    print("  titles, IDs, locations, or years were inferred or invented.")
    print("─" * 74)

    for r in deduped:
        hw = ""  # parity with pmfarm's signal flag is left to the merge step
        print(f"[{r['loc_class']:10s}] {r['company']:24s} {r['title']}")
        print(f"             {r['location'] or '(no location in API)'}")
        print(f"             years: {r['years']}")
        print(f"             {r['url']}")

    with open(OUT_FILE, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(deduped[0].keys()) if deduped else
                           ["source","company","title","location","loc_class","url","job_id","years","date"],
                           quoting=csv.QUOTE_ALL)
        w.writeheader()
        w.writerows(deduped)
    print(f"\nSaved {len(deduped)} role(s) → {OUT_FILE}")
    print(f"Raw API response → {RAW_FILE} (audit trail)")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--query", default="product manager")
    ap.add_argument("--pages", type=int, default=5)
    ap.add_argument("--remote-only", action="store_true")
    ap.add_argument("--all-levels", action="store_true",
                    help="Keep Senior/Staff/Principal/Lead PM roles too")
    ap.add_argument("--broad", action="store_true",
                    help="Run all PM title variants (widest net); implies --all-levels")
    ap.add_argument("--schema", action="store_true",
                    help="dump a sample job's raw fields and exit (verification)")
    ap.add_argument("--probe", action="store_true",
                    help="test endpoint/method variants and report which works")
    args = ap.parse_args()

    # --broad casts the widest net, so it includes all seniority levels.
    pmfarm.INCLUDE_SENIOR = args.all_levels or args.broad

    if args.probe:
        probe(args.query)
    elif args.schema:
        dump_schema(args.query)
    else:
        queries = BROAD_QUERIES if args.broad else [args.query]
        run(queries, args.pages, args.remote_only)
