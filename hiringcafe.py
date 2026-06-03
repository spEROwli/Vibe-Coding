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

# Reuse the exact filtering/dedupe logic from the primary pipeline so both
# pipelines agree on what counts as an IC-level PM role in a viable location.
from pmfarm import (
    _passes_title, _passes_location, _loc_class, _norm_url,
    _ct_key, _load_applied, _strip_html,
)

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


def fetch_page(query: str, page: int, size: int = 100) -> list:
    body = json.dumps({
        "size": size, "page": page, "searchState": _search_state(query),
    }).encode()
    req = urllib.request.Request(API_JOBS, data=body, headers=HEADERS, method="POST")
    with urllib.request.urlopen(req, timeout=20) as r:
        data = json.loads(r.read())
    # Response shape isn't documented; jobs live under one of these keys.
    for key in ("results", "jobs", "data", "items", "content"):
        if isinstance(data, dict) and isinstance(data.get(key), list):
            return data[key]
    if isinstance(data, dict) and isinstance(data.get("hits"), dict):
        return data["hits"].get("hits", [])
    return data if isinstance(data, list) else []


def fetch_all(query: str, pages: int) -> list:
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
    with open(RAW_FILE, "w", encoding="utf-8") as f:
        json.dump(raw, f, indent=2)
    return all_jobs


# ── schema probe (verification aid) ───────────────────────────────────────────

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

def run(query: str, pages: int, remote_only: bool):
    print(f"Querying hiring.cafe for '{query}' ({pages} page(s) max)…")
    jobs = fetch_all(query, pages)
    total = len(jobs)
    print(f"\n{total} jobs returned by API")

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
    print(f"  query:          {query!r}   remote_only={remote_only}")
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
    ap.add_argument("--schema", action="store_true",
                    help="dump a sample job's raw fields and exit (verification)")
    args = ap.parse_args()
    if args.schema:
        dump_schema(args.query)
    else:
        run(args.query, args.pages, args.remote_only)
