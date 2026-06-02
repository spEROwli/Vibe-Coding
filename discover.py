#!/usr/bin/env python3
"""
discover.py — Grow verified_companies.json by seeding from the YC company directory.

Two modes:

  python3 discover.py           # fetch YC NYC companies, probe ATS slugs, update cache
  python3 discover.py --dry-run # same, but only print findings, don't write

Requires local/non-container execution — the container blocks outbound HTTP to
ATS boards. Run this on your laptop once a week to expand the slug cache, then
commit verified_companies.json and it's available in every future Claude Code run.

Discovery pipeline:
  1. Fetch all YC companies from the public yc-oss API (~6k companies)
  2. Filter: NYC-adjacent AND active AND has open jobs
  3. For each company, probe Greenhouse → Ashby → Lever with a slug guess
     (slug guess = company name lowercased, punctuation stripped, spaces → hyphens)
  4. If a board resolves AND has >= 1 role matching TITLE_MUST_INCLUDE → add to cache
  5. Persist verified_companies.json

The yc-oss API is a GitHub Pages-hosted mirror of YC's Algolia index:
  https://yc-oss.github.io/api/companies/all.json
"""

import json, re, sys, time, urllib.request
from pathlib import Path

CACHE_FILE   = Path("verified_companies.json")
YC_API_URL   = "https://yc-oss.github.io/api/companies/all.json"
NYC_STRINGS  = ["new york", "nyc", "brooklyn", "manhattan"]
TITLE_MUST   = ["product manager", "associate product", "technical product"]
MAX_WORKERS  = 12
PROBE_DELAY  = 0.15   # seconds between probes to avoid rate-limit

# ── slug guessing ─────────────────────────────────────────────────────────────

def _guess_slug(name: str) -> list[str]:
    """Return ordered list of slug candidates for a company name."""
    base = name.lower().strip()
    variants = [
        re.sub(r"[^a-z0-9]+", "-", base).strip("-"),   # hyphens
        re.sub(r"[^a-z0-9]+", "",  base),               # no separator
        re.sub(r"\s+",         "-", base.split(",")[0]).strip("-"),  # first word group
    ]
    # strip common suffixes
    for suffix in (" inc", " corp", " llc", " co", " ai", " health", " labs"):
        stripped = re.sub(r"[^a-z0-9]+", "-", base.replace(suffix, "")).strip("-")
        if stripped not in variants:
            variants.append(stripped)
    return list(dict.fromkeys(v for v in variants if v))  # dedupe, keep order


# ── ATS probers ───────────────────────────────────────────────────────────────

def _get(url: str) -> dict | list | None:
    req = urllib.request.Request(url, headers={"User-Agent": "pmfarm-discover/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=8) as r:
            return json.loads(r.read())
    except Exception:
        return None


def _has_pm_role(jobs: list[dict], title_key: str) -> bool:
    for j in jobs:
        t = (j.get(title_key) or "").lower()
        if any(kw in t for kw in TITLE_MUST):
            return True
    return False


def _probe_greenhouse(slug: str) -> bool:
    data = _get(f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs")
    if not data:
        return False
    return _has_pm_role(data.get("jobs", []), "title")


def _probe_ashby(slug: str) -> bool:
    data = _get(f"https://api.ashbyhq.com/posting-api/job-board/{slug}")
    if not data:
        return False
    jobs = data if isinstance(data, list) else data.get("jobPostings", [])
    return _has_pm_role(jobs, "title")


def _probe_lever(slug: str) -> bool:
    data = _get(f"https://api.lever.co/v0/postings/{slug}?mode=json&limit=50")
    if not data:
        return False
    jobs = data if isinstance(data, list) else data.get("postings", [])
    return _has_pm_role(jobs, "text")


PROBERS = [
    ("greenhouse", _probe_greenhouse),
    ("ashby",      _probe_ashby),
    ("lever",      _probe_lever),
]


# ── cache helpers ─────────────────────────────────────────────────────────────

def _load_cache() -> dict:
    if CACHE_FILE.exists():
        return json.loads(CACHE_FILE.read_text())
    return {"greenhouse": [], "ashby": [], "lever": [], "yc": []}


def _known_slugs(cache: dict) -> set[str]:
    out = set()
    for ats_list in cache.values():
        if isinstance(ats_list, list):
            out.update(e["slug"] for e in ats_list if isinstance(e, dict))
    return out


def _save_cache(cache: dict):
    for ats in ["greenhouse", "ashby", "lever", "yc"]:
        cache.setdefault(ats, [])
    cache.get("_meta", {}) and cache.update({
        "_meta": {**cache["_meta"], "last_updated": __import__("datetime").date.today().isoformat()}
    })
    CACHE_FILE.write_text(json.dumps(cache, indent=2))
    print(f"  Saved {CACHE_FILE}")


# ── main ──────────────────────────────────────────────────────────────────────

def discover(dry_run: bool = False):
    print("Fetching YC company directory…")
    all_cos = _get(YC_API_URL)
    if not all_cos:
        print("ERROR: Could not fetch YC API. Are you running locally (not in container)?")
        sys.exit(1)
    print(f"  {len(all_cos)} companies total")

    # Filter to NYC-adjacent companies with open jobs
    candidates = []
    for co in all_cos:
        if not co.get("hiring"):
            continue
        loc = (co.get("city") or co.get("location") or "").lower()
        if not any(n in loc for n in NYC_STRINGS):
            continue
        candidates.append(co)

    print(f"  {len(candidates)} NYC companies currently hiring")

    cache    = _load_cache()
    known    = _known_slugs(cache)
    found    = 0

    for co in candidates:
        name = co.get("name") or co.get("company_name") or ""
        if not name:
            continue

        for slug in _guess_slug(name):
            if slug in known:
                break  # already cached

            for ats_name, prober in PROBERS:
                time.sleep(PROBE_DELAY)
                try:
                    if prober(slug):
                        entry = {
                            "slug": slug,
                            "name": name,
                            "tags": ["nyc"],
                            "yc_batch": co.get("batch", ""),
                        }
                        if not dry_run:
                            cache[ats_name].append(entry)
                            known.add(slug)
                        print(f"  + [{ats_name:12s}] {slug:30s}  ({name})")
                        found += 1
                        break  # found ATS, skip other probers
                except Exception as e:
                    print(f"    probe error {ats_name}/{slug}: {e}", file=sys.stderr)

    if not dry_run and found:
        _save_cache(cache)

    print(f"\nDone. {found} new {'(dry-run, not saved)' if dry_run else 'companies added to cache'}.")


if __name__ == "__main__":
    dry_run = "--dry-run" in sys.argv
    discover(dry_run)
