#!/usr/bin/env python3
"""
pmfarm.py — Pull live PM roles straight from public ATS APIs.
Sources: Greenhouse, Ashby, Lever.
Edit COMPANIES and FILTERS, then: python3 pmfarm.py
"""
import urllib.request, json, re, html as H, csv, datetime, sys, time
from concurrent.futures import ThreadPoolExecutor, as_completed

# ---------- FILTERS (edit these) ----------
TITLE_MUST_INCLUDE = ["product manager", "associate product"]
TITLE_EXCLUDE      = ["marketing", "program manager"]
SENIORITY_EXCLUDE  = ["senior", "sr.", "staff", "principal", "lead", "director",
                      "head of", "vp ", "vice president", " ii", " iii"]
MAX_PM_YEARS       = 3          # drop roles asking more than this; None = keep all
LOCATIONS          = ["new york", "nyc", "brooklyn", "manhattan", "remote",
                      "united states", "anywhere", "us"]   # substring match, lowercase
# ------------------------------------------

# ---------- COMPANIES (edit freely) ----------
GREENHOUSE = [
    "betterment", "robinhood", "justworks", "mongodb", "datadog", "figma",
    "stripe", "brex", "plaid", "affirm", "sofi", "gusto", "rippling",
    "doubleverify", "pinterest", "carta", "hubspot", "webflow", "etsy",
    "duolingo", "airtable", "benchling", "cockroachlabs", "coda",
    "gemini", "navan", "whatnot", "modal", "replit",
]

ASHBY = [
    "notion", "harvey", "ramp", "cohere", "linear", "supabase", "mercury",
    "vanta", "clay", "deel", "retool", "scale", "perplexity", "vercel",
    "anyscale", "cursor", "glean", "watershed", "alchemy", "dbt-labs",
    "openai", "anthropic", "arc", "prefect", "runway",
]

LEVER = [
    "airbnb", "shopify", "canva", "asana", "zendesk", "squarespace",
    "intercom", "netlify", "sendbird", "postman", "contentful",
    "amplitude", "mixpanel", "segment", "heap", "fullstory",
    "pagerduty", "fastly", "cloudflare", "hashicorp",
]
# ------------------------------------------

YEARS_RE = re.compile(
    r'(\d+)\+?\s*(?:–|-|to)\s*(\d+)\s*years?|(\d+)\+\s*years?|(\d+)\s*years?\s*of\s*experience',
    re.IGNORECASE,
)


def _fetch(url: str, payload: bytes | None = None, headers: dict | None = None) -> dict | list | None:
    req = urllib.request.Request(url, data=payload, headers=headers or {})
    req.add_header("User-Agent", "pmfarm/1.0 job-research-bot")
    try:
        with urllib.request.urlopen(req, timeout=12) as r:
            return json.loads(r.read())
    except Exception:
        return None


def _strip_html(text: str) -> str:
    text = re.sub(r"<[^>]+>", " ", text or "")
    return H.unescape(text).strip()


def _max_years_required(text: str) -> int | None:
    """Return the highest years-of-experience number found in text, or None."""
    nums = []
    for m in YEARS_RE.finditer(text):
        for g in m.groups():
            if g is not None:
                nums.append(int(g))
    return max(nums) if nums else None


# ── Greenhouse ──────────────────────────────────────────────────────────────

def fetch_greenhouse(slug: str) -> list[dict]:
    url = f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs?content=true"
    data = _fetch(url)
    if not data or "jobs" not in data:
        return []
    results = []
    for job in data["jobs"]:
        title = job.get("title", "")
        location = ""
        locs = job.get("offices") or job.get("location", {})
        if isinstance(locs, list):
            location = ", ".join(o.get("name", "") for o in locs)
        elif isinstance(locs, dict):
            location = locs.get("name", "")
        content = _strip_html(job.get("content", ""))
        results.append({
            "source":    "Greenhouse",
            "company":   slug,
            "title":     title,
            "location":  location,
            "url":       job.get("absolute_url", ""),
            "posted":    job.get("updated_at", "")[:10],
            "content":   content,
        })
    return results


# ── Ashby ────────────────────────────────────────────────────────────────────

def fetch_ashby(slug: str) -> list[dict]:
    url = f"https://api.ashbyhq.com/posting-api/job-board/{slug}"
    data = _fetch(url)
    if not data:
        return []
    jobs = data if isinstance(data, list) else data.get("jobPostings", [])
    results = []
    for job in jobs:
        title = job.get("title", "") or job.get("jobTitle", "")
        location = job.get("location", "") or job.get("locationName", "")
        job_url = job.get("jobUrl", "") or job.get("applyUrl", "")
        posted = (job.get("publishedDate") or job.get("updatedAt") or "")[:10]
        content = _strip_html(job.get("descriptionHtml", "") or job.get("description", ""))
        results.append({
            "source":   "Ashby",
            "company":  slug,
            "title":    title,
            "location": location,
            "url":      job_url,
            "posted":   posted,
            "content":  content,
        })
    return results


# ── Lever ────────────────────────────────────────────────────────────────────

def fetch_lever(slug: str) -> list[dict]:
    url = f"https://api.lever.co/v0/postings/{slug}?mode=json&limit=250"
    data = _fetch(url)
    if not data:
        return []
    jobs = data if isinstance(data, list) else data.get("postings", [])
    results = []
    for job in jobs:
        title = job.get("text", "")
        cats  = job.get("categories", {})
        location = cats.get("location", "") or cats.get("allLocations", [""])[0] \
            if isinstance(cats.get("allLocations"), list) else cats.get("location", "")
        job_url = job.get("hostedUrl", "") or job.get("applyUrl", "")
        posted  = datetime.datetime.fromtimestamp(
            job["createdAt"] / 1000
        ).date().isoformat() if job.get("createdAt") else ""
        lists = job.get("lists", [])
        content = " ".join(_strip_html(li.get("content", "")) for li in lists)
        content += " " + _strip_html(job.get("descriptionPlain", "") or job.get("description", ""))
        results.append({
            "source":   "Lever",
            "company":  slug,
            "title":    title,
            "location": location,
            "url":      job_url,
            "posted":   posted,
            "content":  content.strip(),
        })
    return results


# ── Filtering ────────────────────────────────────────────────────────────────

def passes_filters(job: dict) -> bool:
    title_lo = job["title"].lower()
    loc_lo   = job["location"].lower()
    content  = (job["title"] + " " + job["content"]).lower()

    if not any(kw in title_lo for kw in TITLE_MUST_INCLUDE):
        return False

    if any(kw in title_lo for kw in TITLE_EXCLUDE):
        return False

    if any(kw in title_lo for kw in SENIORITY_EXCLUDE):
        return False

    if LOCATIONS and not any(loc in loc_lo for loc in LOCATIONS):
        return False

    if MAX_PM_YEARS is not None:
        yrs = _max_years_required(content)
        if yrs is not None and yrs > MAX_PM_YEARS:
            return False

    return True


# ── Main ─────────────────────────────────────────────────────────────────────

def scrape_all() -> list[dict]:
    tasks = (
        [(fetch_greenhouse, s) for s in GREENHOUSE] +
        [(fetch_ashby,      s) for s in ASHBY] +
        [(fetch_lever,      s) for s in LEVER]
    )

    all_jobs: list[dict] = []
    with ThreadPoolExecutor(max_workers=12) as pool:
        futures = {pool.submit(fn, slug): (fn.__name__, slug) for fn, slug in tasks}
        for fut in as_completed(futures):
            fn_name, slug = futures[fut]
            try:
                jobs = fut.result()
                all_jobs.extend(jobs)
                if jobs:
                    print(f"  {fn_name}({slug}): {len(jobs)} jobs fetched")
            except Exception as exc:
                print(f"  {fn_name}({slug}) error: {exc}", file=sys.stderr)

    return all_jobs


def main():
    print("Fetching jobs …")
    raw = scrape_all()
    print(f"\nTotal fetched : {len(raw)}")

    filtered = [j for j in raw if passes_filters(j)]
    print(f"After filters : {len(filtered)}")

    filtered.sort(key=lambda j: (j["company"], j["title"]))

    # ── terminal preview ──────────────────────────────────────────────────
    print("\n" + "─" * 72)
    for j in filtered:
        print(f"[{j['source']}] {j['company']:20s}  {j['title']}")
        print(f"  {j['location']:30s}  {j['posted']}")
        print(f"  {j['url']}")
        print()

    # ── CSV export ────────────────────────────────────────────────────────
    ts       = datetime.datetime.now().strftime("%Y%m%d_%H%M")
    filename = f"pm_roles_{ts}.csv"
    fields   = ["source", "company", "title", "location", "posted", "url"]
    with open(filename, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(filtered)

    print(f"Saved {len(filtered)} roles → {filename}")


if __name__ == "__main__":
    main()
