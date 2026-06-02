#!/usr/bin/env python3
"""
pmfarm.py — PM role scraper: Greenhouse · Ashby · Lever

Local use (direct API, works outside Claude Code cloud):
  python3 pmfarm.py [--remote-only]

Claude Code iOS (WebSearch pipeline):
  python3 pmfarm.py queries
  python3 pmfarm.py process FILE [--remote-only]

Dedupe: put an applied.csv (columns: company, url) next to this script.
        Any role whose URL — or company+title pair — matches is silently skipped.
        Fill the `applied` column in pm_roles.csv as you go; that file can serve
        as next run's applied.csv.
"""

import argparse, csv, datetime, html as H, json, re, sys, urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import urlparse

# ── FILTERS ──────────────────────────────────────────────────────────────────
TITLE_MUST_INCLUDE = [
    "product manager", "associate product", "technical product",
    "hardware product", "apm program", "rotational product",
]
TITLE_EXCLUDE      = ["marketing", "program manager", "product marketing"]
SENIORITY_EXCLUDE  = ["senior", "sr.", "staff", "principal", "lead", "director",
                      "head of", "vp ", "vice president", " ii", " iii", "group product"]

NYC_LOCS    = ["new york", "nyc", "brooklyn", "manhattan"]
REMOTE_LOCS = ["remote", "united states", "anywhere", "us", "nationwide",
               "distributed", "work from anywhere", "work from home"]

# Keywords in description that signal the role values engineering background.
# Surfaced in terminal output as a "signal" flag — not used for filtering.
HARDWARE_SIGNAL = [
    "mechanical engineer", "hardware", "medical device", "med device",
    "regulated", "fda", "iso 13485", "physical product", "manufacturing",
    "embedded", "firmware", "iot", "wearable", "sensor",
]

# ── COMPANIES ─────────────────────────────────────────────────────────────────
# Pure-software / fintech — competitive without PM experience
GREENHOUSE = [
    "betterment", "robinhood", "justworks", "mongodb", "datadog", "figma",
    "stripe", "brex", "plaid", "affirm", "sofi", "gusto", "rippling",
    "doubleverify", "pinterest", "carta", "hubspot", "webflow", "etsy",
    "duolingo", "airtable", "benchling", "cockroachlabs", "coda",
    "gemini", "navan", "whatnot", "modal", "replit",
    # Healthtech / med-adjacent — your background is a differentiator here
    "hingehealth", "springhealth", "headway", "cerebral", "lyra",
    "ro", "twentyeight-health", "tempus", "color", "flatiron",
    # Hardware-adjacent — engineering background valued
    "peloton", "whoop", "oura", "brilliant", "verkada",
]
ASHBY = [
    "notion", "harvey", "ramp", "cohere", "linear", "supabase", "mercury",
    "vanta", "clay", "deel", "retool", "scale", "perplexity", "vercel",
    "anyscale", "cursor", "glean", "watershed", "alchemy", "dbt-labs",
    "openai", "anthropic", "arc", "prefect", "runway",
    # Healthtech
    "nuvation", "sword-health", "kry", "nirahealth", "zealthy",
    "turquoise-health", "ribbon-health", "available",
]
LEVER = [
    "airbnb", "shopify", "canva", "asana", "zendesk", "squarespace",
    "intercom", "netlify", "sendbird", "postman", "contentful",
    "amplitude", "mixpanel", "segment", "heap", "fullstory",
    "pagerduty", "fastly", "cloudflare", "hashicorp",
    # APM programs and hardware companies that use Lever
    "nuro", "astrazeneca", "veracyte",
]

ATS_DOMAINS = {
    "boards.greenhouse.io": "Greenhouse",
    "jobs.lever.co":        "Lever",
    "jobs.ashbyhq.com":     "Ashby",
}

YEARS_RE = re.compile(
    r'(\d+)\+?\s*(?:–|-|to)\s*(\d+)\s*years?|(\d+)\+\s*years?|(\d+)\s*years?\s*of\s*experience',
    re.IGNORECASE,
)

CHUNK        = 8
NEG          = ' -"senior" -"staff" -"principal" -"director" -"lead"'
APPLIED_FILE = "applied.csv"
OUTPUT_FILE  = "pm_roles.csv"


# ── helpers ───────────────────────────────────────────────────────────────────

def _strip_html(text: str) -> str:
    return H.unescape(re.sub(r"<[^>]+>", " ", text or "")).strip()


def _chunks(lst, n):
    for i in range(0, len(lst), n):
        yield lst[i:i+n]


def _parse_years(text: str) -> tuple[str, str]:
    """Return (years_raw, years_context). Surfaces the max year count found."""
    best_n, best_ctx = None, ""
    for m in YEARS_RE.finditer(text):
        nums = [int(g) for g in m.groups() if g]
        if not nums:
            continue
        n = max(nums)
        if best_n is None or n > best_n:
            best_n   = n
            s        = max(0, m.start() - 35)
            e        = min(len(text), m.end() + 35)
            best_ctx = "…" + text[s:e].replace("\n", " ").strip() + "…"
    return (str(best_n) if best_n is not None else "unknown", best_ctx)


def _loc_class(location: str, snippet: str) -> str:
    combined   = (location + " " + snippet).lower()
    has_remote = any(r in combined for r in REMOTE_LOCS)
    has_nyc    = any(n in combined for n in NYC_LOCS)
    if has_remote and has_nyc:
        return "remote+nyc"
    if has_remote:
        return "remote"
    if has_nyc:
        return "nyc"
    return "unknown"


def _days_old(date_str: str | None) -> str:
    if not date_str:
        return "unknown"
    try:
        dt    = datetime.datetime.fromisoformat(date_str.replace("Z", "+00:00"))
        delta = datetime.datetime.now(datetime.timezone.utc) - dt
        return str(delta.days)
    except Exception:
        return "unknown"


def _passes_title(title: str) -> bool:
    t = title.lower()
    return (any(kw in t for kw in TITLE_MUST_INCLUDE)
            and not any(kw in t for kw in TITLE_EXCLUDE)
            and not any(kw in t for kw in SENIORITY_EXCLUDE))


def _passes_location(lc: str, remote_only: bool) -> bool:
    if remote_only:
        return lc in ("remote", "remote+nyc", "unknown")
    return lc in ("remote", "remote+nyc", "nyc", "unknown")


def _load_applied() -> set[str]:
    seen: set[str] = set()
    try:
        with open(APPLIED_FILE, newline="", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                if row.get("url"):
                    seen.add(row["url"].strip().lower())
                if row.get("company") and row.get("title"):
                    seen.add(f"{row['company'].strip().lower()}|{row['title'].strip().lower()}")
    except FileNotFoundError:
        pass
    return seen


def _deduped(jobs: list[dict], applied: set[str]) -> list[dict]:
    out = []
    for j in jobs:
        if (j["url"].strip().lower() in applied or
                f"{j['company'].strip().lower()}|{j['title'].strip().lower()}" in applied):
            continue
        out.append(j)
    return out


def _make_job(source, company, title, location, url, snippet, date_str) -> dict:
    lc            = _loc_class(location, snippet)
    years_raw, yc = _parse_years(snippet)
    return {
        "source":        source,
        "company":       company,
        "title":         title,
        "location":      location,
        "loc_class":     lc,
        "url":           url,
        "days_old":      _days_old(date_str),
        "years_raw":     years_raw,
        "years_context": yc,
        "hw_signal":     "YES" if any(kw in (title + " " + snippet).lower() for kw in HARDWARE_SIGNAL) else "",
        "applied":       "",
    }


# ── ATS fetchers ──────────────────────────────────────────────────────────────

def _fetch(url: str) -> dict | list | None:
    req = urllib.request.Request(url, headers={"User-Agent": "pmfarm/3.0"})
    try:
        with urllib.request.urlopen(req, timeout=12) as r:
            return json.loads(r.read())
    except Exception:
        return None


def fetch_greenhouse(slug: str) -> list[dict]:
    data = _fetch(f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs?content=true")
    if not data:
        return []
    out = []
    for j in data.get("jobs", []):
        title = j.get("title", "")
        if not _passes_title(title):
            continue
        loc      = j.get("location", {})
        location = loc.get("name", "") if isinstance(loc, dict) else ""
        content  = _strip_html(j.get("content", ""))
        out.append(_make_job(
            "Greenhouse", slug, title, location,
            j.get("absolute_url", ""), content[:500],
            j.get("updated_at"),
        ))
    return out


def fetch_ashby(slug: str) -> list[dict]:
    data = _fetch(f"https://api.ashbyhq.com/posting-api/job-board/{slug}")
    if not data:
        return []
    jobs = data if isinstance(data, list) else data.get("jobPostings", [])
    out  = []
    for j in jobs:
        title = j.get("title", "") or j.get("jobTitle", "")
        if not _passes_title(title):
            continue
        location = j.get("location", "") or j.get("locationName", "")
        content  = _strip_html(j.get("descriptionHtml", "") or j.get("description", ""))
        out.append(_make_job(
            "Ashby", slug, title, location,
            j.get("jobUrl", "") or j.get("applyUrl", ""), content[:500],
            None,   # Ashby does not reliably expose a post date
        ))
    return out


def fetch_lever(slug: str) -> list[dict]:
    data = _fetch(f"https://api.lever.co/v0/postings/{slug}?mode=json&limit=250")
    if not data:
        return []
    jobs = data if isinstance(data, list) else data.get("postings", [])
    out  = []
    for j in jobs:
        title = j.get("text", "")
        if not _passes_title(title):
            continue
        cats     = j.get("categories", {})
        location = cats.get("location", "")
        content  = " ".join(_strip_html(li.get("content", "")) for li in j.get("lists", []))
        ts       = j.get("createdAt")
        date_str = (datetime.datetime.utcfromtimestamp(ts / 1000).isoformat() + "Z") if ts else None
        out.append(_make_job(
            "Lever", slug, title, location,
            j.get("hostedUrl", ""), content[:500],
            date_str,
        ))
    return out


# ── output ─────────────────────────────────────────────────────────────────────

def _sort_key(j: dict) -> tuple:
    loc_order = {"remote": 0, "remote+nyc": 1, "nyc": 2, "unknown": 3}
    order = loc_order.get(j["loc_class"], 4)
    try:
        age = int(j["days_old"])
    except (ValueError, TypeError):
        age = 9999
    return (order, age)


def _output(jobs: list[dict], skipped: int = 0):
    jobs.sort(key=_sort_key)

    print(f"\n{'─' * 72}")
    for j in jobs:
        age = f"{j['days_old']}d" if j["days_old"] != "unknown" else "?d"
        yrs = j["years_raw"] if j["years_raw"] != "unknown" else "?"
        hw = "  *** HW/MEDTECH SIGNAL ***" if j.get("hw_signal") else ""
        print(f"[{j['source']:11s}] {j['company']:18s}  {j['title']}{hw}")
        print(f"  {(j['location'] or j['loc_class']):32s}  age={age:<6s}  yrs_req={yrs}")
        if j["years_context"]:
            print(f"  \"{j['years_context'][:90]}\"")
        print(f"  {j['url']}\n")

    if skipped:
        print(f"(skipped {skipped} already-applied role(s) from {APPLIED_FILE})\n")

    fields = ["source", "company", "title", "location", "loc_class",
              "url", "days_old", "years_raw", "years_context", "hw_signal", "applied"]
    with open(OUTPUT_FILE, "w", newline="", encoding="utf-8") as f:
        csv.DictWriter(f, fieldnames=fields).writeheader()
        csv.DictWriter(f, fieldnames=fields).writerows(jobs)
    print(f"Saved {len(jobs)} role(s) → {OUTPUT_FILE}")
    print(f"Fill the 'applied' column as you go. Save as {APPLIED_FILE} to dedupe next run.")


# ── phase 1: query generation (Claude Code iOS) ───────────────────────────────

def cmd_queries():
    specs = [
        ("boards.greenhouse.io", GREENHOUSE),
        ("jobs.ashbyhq.com",     ASHBY),
        ("jobs.lever.co",        LEVER),
    ]
    out = []
    for domain, companies in specs:
        for chunk in _chunks(companies, CHUNK):
            sites = " OR ".join(f"site:{domain}/{c}" for c in chunk)
            out.append({"query": f'({sites}) "product manager"{NEG}', "domain": domain})
    print(json.dumps(out, indent=2))


# ── phase 2: process WebSearch results (Claude Code iOS) ─────────────────────

def cmd_process(path: str, remote_only: bool):
    with open(path) as f:
        raw = json.load(f)

    applied = _load_applied()
    jobs    = []
    for item in raw:
        url     = item.get("url", "")
        title   = item.get("title", "")
        snippet = item.get("snippet", "") or item.get("description", "")
        domain  = urlparse(url).netloc.lstrip("www.")
        if domain not in ATS_DOMAINS or not _passes_title(title):
            continue
        lc = _loc_class("", snippet)
        if not _passes_location(lc, remote_only):
            continue
        company       = urlparse(url).path.lstrip("/").split("/")[0]
        years_raw, yc = _parse_years(snippet)
        hw = "YES" if any(kw in (title + " " + snippet).lower() for kw in HARDWARE_SIGNAL) else ""
        jobs.append({
            "source":        ATS_DOMAINS[domain],
            "company":       company,
            "title":         title,
            "location":      "",
            "loc_class":     lc,
            "url":           url,
            "days_old":      "unknown",
            "years_raw":     years_raw,
            "years_context": yc,
            "hw_signal":     hw,
            "applied":       "",
        })

    pre     = len(jobs)
    jobs    = _deduped(jobs, applied)
    skipped = pre - len(jobs)
    _output(jobs, skipped)


# ── local mode ────────────────────────────────────────────────────────────────

def cmd_local(remote_only: bool):
    tasks = ([(fetch_greenhouse, s) for s in GREENHOUSE] +
             [(fetch_ashby,      s) for s in ASHBY] +
             [(fetch_lever,      s) for s in LEVER])

    raw: list[dict] = []
    with ThreadPoolExecutor(max_workers=12) as pool:
        futs = {pool.submit(fn, slug): (fn.__name__, slug) for fn, slug in tasks}
        for fut in as_completed(futs):
            fn_name, slug = futs[fut]
            try:
                rows = fut.result()
                if rows:
                    print(f"  {fn_name}({slug}): {len(rows)} matched title filter")
                raw.extend(rows)
            except Exception as e:
                print(f"  {fn_name}({slug}) error: {e}", file=sys.stderr)

    filtered = [j for j in raw if _passes_location(j["loc_class"], remote_only)]
    applied  = _load_applied()
    pre      = len(filtered)
    jobs     = _deduped(filtered, applied)
    skipped  = pre - len(jobs)
    print(f"\n{len(raw)} title-matched → {len(filtered)} after location → {len(jobs)} after dedupe")
    _output(jobs, skipped)


# ── entry point ───────────────────────────────────────────────────────────────

def main():
    p = argparse.ArgumentParser(
        description="PM role scraper — Greenhouse, Ashby, Lever",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    p.add_argument("cmd", nargs="?", default="local",
                   choices=["local", "queries", "process"],
                   help="local (default), queries, or process FILE")
    p.add_argument("file", nargs="?", help="WebSearch results JSON (process mode only)")
    p.add_argument("--remote-only", action="store_true",
                   help="Remote/US-wide roles only; drop NYC-specific listings")
    args = p.parse_args()

    if args.cmd == "queries":
        cmd_queries()
    elif args.cmd == "process":
        if not args.file:
            p.error("'process' requires a FILE argument")
        cmd_process(args.file, args.remote_only)
    else:
        cmd_local(args.remote_only)


if __name__ == "__main__":
    main()
