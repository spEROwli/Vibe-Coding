#!/usr/bin/env python3
"""
pmfarm.py — PM role scraper: Greenhouse · Ashby · Lever

  python3 pmfarm.py [--remote-only] [--all-levels] [--include-unknown-loc]

All data comes from live ATS JSON APIs. See SCRAPER_RULES.md.
Dedupe: gmail_applied.txt (primary) + applied.csv (fallback).
"""

import argparse, csv, datetime, html as H, json, os, re, sys, urllib.request, urllib.parse
from concurrent.futures import ThreadPoolExecutor, as_completed

# ── FILTERS ──────────────────────────────────────────────────────────────────
TITLE_MUST_INCLUDE = [
    "product manager", "associate product", "technical product",
    "hardware product", "apm program", "rotational product",
]
TITLE_EXCLUDE      = ["marketing", "program manager", "product marketing"]

# Word-boundary regex — "lead" must not match "leadership", "staff" not "staffing", etc.
# (?<!\w) / (?!\w) are lookaround equivalents of \b that work around "sr." having a
# non-word char at the end.
_SENIORITY_RE = re.compile(
    r'(?<!\w)(?:senior|sr\.?|staff|principal|lead|director|vp|vice\s+president)(?!\w)'
    r'|(?<!\w)head\s+of\b'
    r'|(?<!\w)group\s+product\b'
    r'|\b(?:ii|iii)(?:\s|$)',
    re.IGNORECASE,
)

# Executive/level markers that are disqualifying NO MATTER where they appear in
# the title — after a comma, a dash, or a slash ("Product Manager, VP",
# "PM - Vice President", "… Senior Associate / Vice President"). These roles are
# dropped entirely (never written to the CSV), even under --all-levels, because
# they are categorically above the IC bar this tool targets.
_HARD_SENIOR_RE = re.compile(
    r'(?<!\w)(?:vice\s+president|vp|svp|evp|senior\s+associate|head\s+of|'
    r'principal|staff|director|managing\s+director|md)(?!\w)',
    re.IGNORECASE,
)

NYC_LOCS    = ["new york", "nyc", "brooklyn", "manhattan"]
REMOTE_LOCS = ["remote", "united states", "anywhere", "nationwide",
               "distributed", "work from anywhere", "work from home"]

# Specific US metros that are NOT NYC. If a location names one of these
# cities without also saying "remote", the role is in-office outside NYC
# and gets loc_class="unknown" (excluded by default).
_US_NON_NYC = [
    "san francisco", "seattle", "chicago", "austin", "boston",
    "los angeles", "denver", "atlanta", "portland", "miami",
    "dallas", "houston", "phoenix", "san jose", "pleasanton",
    "palo alto", "mountain view", "menlo park", "redwood city",
    "san carlos", "bellevue", "kirkland", "cambridge", "pittsburgh",
    "philadelphia", "minneapolis", "nashville", "charlotte", "raleigh",
    "salt lake city", "san diego", "las vegas", "tampa", "orlando",
    "detroit", "cleveland", "cincinnati", "indianapolis", "st. louis",
    "kansas city", "new orleans", "richmond", "baltimore", "washington dc",
    "washington, dc", "washington, d.c", "washington",
]

# Country/region markers that make a role non-US even if it says "remote"
# ("Remote - Canada", "Remote, EMEA"). Used to stop foreign-remote roles from
# being classified as US-remote and slipping past the geo filter.
_INTL_MARKERS = [
    "canada", "toronto", "vancouver", "montreal", "ontario",
    "united kingdom", "london", "england", "ireland", "dublin",
    "france", "paris", "germany", "berlin", "munich", "munzstrasse",
    "netherlands", "amsterdam", "spain", "madrid", "barcelona", "aveiro",
    "portugal", "lisbon", "italy", "rome", "milan", "poland", "warsaw",
    "sweden", "stockholm", "switzerland", "zurich", "emea", "apac",
    "japan", "tokyo", "singapore", "india", "bangalore", "bengaluru",
    "australia", "sydney", "melbourne", "brazil", "sao paulo", "mexico",
    "hungary", "budapest", "israel", "tel aviv", "philippines", "manila",
]

# Keywords in description that signal the role values engineering background.
# Surfaced in terminal output as a "signal" flag — not used for filtering.
HARDWARE_SIGNAL = [
    "mechanical engineer", "hardware", "medical device", "med device", "medtech",
    "regulated", "fda", "iso 13485", "physical product", "manufacturing",
    "embedded", "firmware", "iot", "wearable", "sensor",
    "diagnostics", "clinical", "life sciences", "life-science", "biomedical",
    "surgical", "medical imaging", "point-of-care", "in vitro", "pharmaceutical",
]

LANGUAGE_SIGNAL = [
    "fluent in spanish", "fluent in french", "spanish speaker", "french speaker",
    "bilingual", "multilingual", "latam", "latin america", "francophone",
    "spanish language", "french language", "español", "français",
]

# ── COMPANIES ─────────────────────────────────────────────────────────────────
# Loaded from verified_companies.json if present; falls back to these defaults
# per-ATS (so a cache with only greenhouse slugs still gets Ashby/Lever coverage).
# Run discover.py locally to grow the cache with YC-seeded companies.
_CACHE_FILE = "verified_companies.json"

_FALLBACK_GH = [
    "betterment", "robinhood", "justworks", "mongodb", "datadog", "figma",
    "stripe", "brex", "plaid", "affirm", "sofi", "gusto", "rippling",
    "doubleverify", "pinterest", "carta", "hubspot", "webflow", "etsy",
    "duolingo", "airtable", "benchling", "cockroachlabs", "coda",
    "gemini", "navan", "whatnot", "modal", "replit",
    "hingehealth", "springhealth", "headway", "cerebral", "lyra",
    "ro", "twentyeight-health", "tempus", "color", "flatiron",
    "peloton", "whoop", "oura", "brilliant", "verkada",
    "mantrahealth", "alteradigitalhealth", "mavenclinic", "airbnb",
]
_FALLBACK_ASH = [
    "notion", "harvey", "ramp", "cohere", "linear", "supabase", "mercury",
    "vanta", "clay", "deel", "retool", "scale-ai", "perplexity", "vercel",
    "anyscale", "cursor", "glean", "watershed", "alchemy", "dbt-labs",
    "openai", "anthropic", "arc", "prefect", "runway",
    "nuvation", "sword-health", "nirahealth", "turquoise-health", "ribbon-health",
    "oneapp", "airwallex", "pivotal-health", "plaid", "brigit",
]
_FALLBACK_LEV = [
    "airbnb", "shopify", "canva", "asana", "zendesk", "squarespace",
    "intercom", "netlify", "sendbird", "postman", "contentful",
    "amplitude", "mixpanel", "pagerduty", "cloudflare", "hashicorp",
    "nuro", "veracyte",
    "tenna", "cents", "salvohealth", "mistral", "luni", "Flex",
]


def _load_companies() -> tuple[list, list, list]:
    # Preferred source: companies.db (self-cleaning, learns which slugs are live).
    # Build it with `python3 build_companydb.py`. Falls back to the JSON cache and
    # then the hardcoded lists if the DB is absent or empty.
    try:
        import companydb, os
        if os.path.exists(companydb.DB_FILE):
            gh  = companydb.load_active("greenhouse")
            ash = companydb.load_active("ashby")
            lev = companydb.load_active("lever")
            if gh or ash or lev:
                return (gh or _FALLBACK_GH, ash or _FALLBACK_ASH, lev or _FALLBACK_LEV)
    except Exception as e:
        print(f"  [warn] companies.db load failed ({e}); using JSON cache",
              file=sys.stderr)

    gh = ash = lev = None
    try:
        import os
        if os.path.exists(_CACHE_FILE):
            data = json.loads(open(_CACHE_FILE, encoding="utf-8").read())
            cand = data.get("candidates", {})
            # Merge verified slugs with the wider candidate pool. Candidates are
            # unconfirmed company guesses; a bad slug simply returns no jobs from
            # the API (harmless), while good ones widen coverage for free.
            def _merge(key):
                seen, out = set(), []
                for src in (data.get(key) or [], cand.get(key) or []):
                    for e in src:
                        s = e["slug"] if isinstance(e, dict) else e
                        if s and s not in seen:
                            seen.add(s); out.append(s)
                return out or None   # None → fall through to hardcoded fallback
            gh  = _merge("greenhouse")
            ash = _merge("ashby")
            lev = _merge("lever")
    except Exception as e:
        print(f"  [warn] could not load {_CACHE_FILE}: {e}", file=sys.stderr)
    # Fall back per-ATS so a partial cache never silently drops a whole provider.
    return (
        gh  if gh  is not None else _FALLBACK_GH,
        ash if ash is not None else _FALLBACK_ASH,
        lev if lev is not None else _FALLBACK_LEV,
    )

GREENHOUSE, ASHBY, LEVER = _load_companies()

# Requirement-style year patterns only. Deliberately NO bare "N years" pattern,
# so phrasing like "10 years of combined team experience" is NOT mistaken for an
# individual requirement. Each pattern's group(s) yield candidate year value(s);
# for ranges we keep the LOW end (the minimum bar to clear).
YEARS_PATTERNS = [
    # range: "3-5 years", "3 to 5 years"  → low end
    (re.compile(r'(\d+)\s*(?:–|—|-|to)\s*(\d+)\s*\+?\s*years?', re.I), "low"),
    # plus: "5+ years"
    (re.compile(r'(\d+)\s*\+\s*years?', re.I), "all"),
    # "N+ years of [≤3 words] experience" — allows "of product management experience"
    (re.compile(r'(\d+)\s*\+?\s*years?\s+of\s+(?:[\w-]+\s+){0,3}experience', re.I), "all"),
    # in-domain: "2+ in product", "5+ years in marketing"
    (re.compile(r'(\d+)\s*\+?\s*(?:years?\s+)?in\s+'
                r'(?:product|marketing|software|tech|engineering|design|'
                r'operations|consulting|business|industry)', re.I), "all"),
    # "at least N years", "minimum of N years"
    (re.compile(r'(?:at\s+least|minimum(?:\s+of)?|min\.?)\s+(\d+)\s*\+?\s*years?', re.I), "all"),
]

# A matched span containing any of these describes team/collective tenure,
# not an individual PM requirement — so it is dropped.
YEARS_DISQUALIFY = ["combined", "collective", "total", "company-wide", "across our"]

# Drop roles whose posting is older than this many days (when a date is known).
# Aggregators like The Muse keep evergreen listings open for a year+; those are
# stale by any job-seeker's standard. Roles with no date are kept (can't judge).
MAX_AGE_DAYS = 60

APPLIED_FILE = "applied.csv"
GMAIL_FILE   = "gmail_applied.txt"  # synced by pmfarm_gmail_sync.py; one company per line
OUTPUT_FILE  = "pm_roles.csv"

# Populated by fetch_* functions; reset at the start of each cmd_local run.
# key = "source:slug", value = "ok" | "empty" | "fail"
_slug_resolution: dict[str, str] = {}


def _record(source: str, slug: str, status: str) -> None:
    _slug_resolution[f"{source}:{slug}"] = status


# ── helpers ───────────────────────────────────────────────────────────────────

def _strip_html(text: str) -> str:
    # Unescape first so &lt;p&gt; → <p> before the tag regex runs.
    t = H.unescape(text or "")
    # Convert closing block-level tags to ". " so list items become distinct
    # pseudo-sentences that _years_sentence can split on.
    t = re.sub(r"</?(li|p|br|div|tr|h[1-6]|ul|ol|table|tbody|thead|th|td)\b[^>]*>",
               ". ", t, flags=re.I)
    t = re.sub(r"<[^>]+>", " ", t)
    t = re.sub(r"(\.\s*){2,}", ". ", t)   # collapse ". . . " → ". "
    return re.sub(r"\s+", " ", t).strip(" .")


def _parse_years(text: str) -> tuple[str, str]:
    """Return (years_raw, years_context).

    Honest-by-design: collects every requirement-style year mention, drops
    team/collective-tenure phrasing, and reports the MINIMUM bar found (the most
    optimistic read, so you never self-reject). The context column shows every
    matched phrase so you can catch the number if it's lying. No match → unknown.
    """
    candidates: list[int] = []
    contexts:   list[str] = []
    for pattern, mode in YEARS_PATTERNS:
        for m in pattern.finditer(text):
            span = m.group(0).lower()
            if any(bad in span for bad in YEARS_DISQUALIFY):
                continue
            nums = [int(g) for g in m.groups() if g]
            if not nums:
                continue
            value = nums[0] if mode == "low" else min(nums)
            candidates.append(value)
            s   = max(0, m.start() - 30)
            e   = min(len(text), m.end() + 30)
            ctx = "…" + text[s:e].replace("\n", " ").strip() + "…"
            if ctx not in contexts:
                contexts.append(ctx)

    if not candidates:
        return ("unknown", "")
    return (str(min(candidates)), " | ".join(contexts[:3]))


def _years_sentence(text: str) -> str:
    """Return the first verbatim sentence containing 'year(s)' from the JD content,
    else 'not stated'. SCRAPER_RULES: the years field must be the exact JD sentence,
    never a bucket or a derived number. Run on FULL content, not a truncation."""
    t = re.sub(r"\s+", " ", _strip_html(text or "")).strip()
    for sentence in re.split(r"(?<=[.!?])\s+", t):
        if re.search(r"\byears?\b", sentence, re.I):
            return sentence.strip()[:300]
    return "not stated"


def _norm_url(url: str) -> str:
    """Normalize a job URL for reliable dedupe: lowercase, force https, strip
    query string / fragment / trailing slash. Defeats false misses from
    ?gh_jid=, ?gh_src=, http vs https, and trailing-slash variants."""
    u = (url or "").strip().lower()
    if u.startswith("http://"):
        u = "https://" + u[len("http://"):]
    u = u.split("?", 1)[0].split("#", 1)[0]
    return u.rstrip("/")


def _loc_class(location: str, snippet: str = "") -> str:
    # Classification uses only the location field, not the JD snippet, to prevent
    # body text like "remote work options" from misclassifying in-office roles.
    loc_lower = location.lower()
    has_nyc             = any(n in loc_lower for n in NYC_LOCS)

    # A named foreign country/region makes this international regardless of
    # "remote" — "Remote - Canada" is not a US-remote role.
    has_intl = any(m in loc_lower for m in _INTL_MARKERS)
    if has_intl and not has_nyc:
        return "international"

    # Explicit "remote" in the location field always wins.
    has_explicit_remote = "remote" in loc_lower

    # Generic US location terms only count when no specific non-NYC city is present,
    # preventing "San Francisco, United States" from matching as remote.
    has_non_nyc_us = any(c in loc_lower for c in _US_NON_NYC)
    has_us_generic = (not has_non_nyc_us) and any(r in loc_lower for r in REMOTE_LOCS)

    has_remote = has_explicit_remote or has_us_generic

    if has_remote and has_nyc: return "remote+nyc"
    if has_remote:             return "remote"
    if has_nyc:                return "nyc"
    if has_non_nyc_us:         return "unknown"
    if location.strip():       return "international"
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


# When True, the seniority gate is disabled so Senior/Staff/Principal/Lead PM
# roles are kept too. Set via --all-levels for candidates with the experience to
# clear higher bars who want maximum coverage.
INCLUDE_SENIOR = False


def _passes_title(title: str) -> bool:
    t = title.lower()
    if not any(kw in t for kw in TITLE_MUST_INCLUDE):
        return False
    if any(kw in t for kw in TITLE_EXCLUDE):
        return False
    # Hard executive markers (VP/Director/Principal/Staff/Senior Associate/MD)
    # disqualify anywhere in the title and are NOT overridable by --all-levels.
    if _HARD_SENIOR_RE.search(t):
        return False
    if INCLUDE_SENIOR:
        return True
    # Check seniority only in the pre-comma segment: "Product Manager, Senior Care"
    # has "Senior" in the specialty area, not the level.  Real seniority markers
    # ("Senior Product Manager", "Lead PM") always precede the role noun.
    # Exception: roman-numeral suffixes like "II"/"III" appear after the role noun
    # with no comma, so parts[0] still catches them ("Product Manager II").
    main = t.split(",", 1)[0]
    return not _SENIORITY_RE.search(main)


def _passes_location(lc: str, remote_only: bool, include_unknown: bool = False) -> bool:
    """Determine whether a role's loc_class passes the geo filter.

    Default (no flags): only remote, remote+nyc, nyc pass. unknown is excluded
    because an unclassified location is not a confirmed NYC/remote match — it is
    a data gap that can silently include SF-only or international roles.
    Use --include-unknown-loc to opt in to unknown-location roles.
    """
    if remote_only:
        allowed = {"remote", "remote+nyc"}
    else:
        allowed = {"remote", "remote+nyc", "nyc"}
    if include_unknown:
        allowed.add("unknown")
    return lc in allowed


def _ct_key(company: str, title: str) -> str:
    return f"{company.strip().lower()}|{title.strip().lower()}"


def _normalize_company(name: str) -> str:
    """Lowercase + strip all non-alphanumeric chars for Gmail company matching.
    'FanDuel' → 'fanduel'; 'Scale AI' → 'scaleai'; 'scale-ai' (slug) → 'scaleai'.
    Known false negative: 'CLEAR' → 'clear' ≠ slug 'clearme' (add to applied.csv manually)."""
    return re.sub(r"[^a-z0-9]", "", name.lower())


def _gmail_applied_set() -> tuple[set[str], list[tuple]]:
    """Read gmail_applied.txt. Returns (normalized_company_set, evidence_list).
    Each evidence entry: (normalized_name, raw_name, subject_line, date_str).
    Silently returns empty set if the file is absent (Gmail dedupe disabled)."""
    companies: set[str] = set()
    evidence:  list[tuple] = []
    try:
        with open(GMAIL_FILE, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                parts = line.split("\t", 2)
                raw  = parts[0].strip()
                subj = parts[1].strip() if len(parts) > 1 else ""
                date = parts[2].strip() if len(parts) > 2 else ""
                norm = _normalize_company(raw)
                if norm:
                    companies.add(norm)
                    evidence.append((norm, raw, subj, date))
    except FileNotFoundError:
        print(f"  ℹ  {GMAIL_FILE} not found — Gmail dedupe disabled. "
              "Run: python3 pmfarm_gmail_sync.py", file=sys.stderr)
    return companies, evidence


def _load_applied() -> set[str]:
    seen: set[str] = set()
    try:
        with open(APPLIED_FILE, newline="", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                if row.get("url"):
                    seen.add(_norm_url(row["url"]))
                if row.get("company") and row.get("title"):
                    seen.add(_ct_key(row["company"], row["title"]))
    except FileNotFoundError:
        pass
    return seen


def _deduped(jobs: list[dict], applied: set[str]) -> list[dict]:
    out, seen = [], set()
    for j in jobs:
        nu = _norm_url(j["url"])
        ck = _ct_key(j["company"], j["title"])
        if nu in applied or ck in applied:
            continue
        # Within-run dupe: aggregators (The Muse) repeat one role under several
        # location groupings — same company+title is the same job.
        if nu in seen or ck in seen:
            continue
        seen.add(nu)
        seen.add(ck)
        out.append(j)
    return out


def _make_job(source, company, title, location, url, snippet, date_str,
              full_content=None) -> dict:
    content        = full_content if full_content is not None else snippet
    lc             = _loc_class(location)
    years_raw, yc  = _parse_years(content)
    years_sentence = _years_sentence(content)
    body_lower     = (title + " " + content).lower()
    return {
        "source":         source,
        "company":        company,
        "title":          title,
        "location":       location,
        "loc_class":      lc,
        "url":            url,
        "days_old":       _days_old(date_str),
        "years_raw":      years_raw,
        "years_context":  yc,
        "years_sentence": years_sentence,
        "hw_signal":      "YES" if any(kw in body_lower for kw in HARDWARE_SIGNAL) else "",
        "lang_signal":    "YES" if any(kw in body_lower for kw in LANGUAGE_SIGNAL) else "",
        "applied":        "",
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
    if not isinstance(data, dict):
        _record("GH", slug, "fail")
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
            j.get("updated_at"), full_content=content,
        ))
    _record("GH", slug, "ok" if out else "empty")
    return out


def fetch_ashby(slug: str) -> list[dict]:
    data = _fetch(f"https://api.ashbyhq.com/posting-api/job-board/{slug}")
    if not data:
        _record("Ashby", slug, "fail")
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
            full_content=content,
        ))
    _record("Ashby", slug, "ok" if out else "empty")
    return out


def fetch_lever(slug: str) -> list[dict]:
    data = _fetch(f"https://api.lever.co/v0/postings/{slug}?mode=json&limit=250")
    if not data:
        _record("Lever", slug, "fail")
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
        date_str = (datetime.datetime.fromtimestamp(ts / 1000, datetime.timezone.utc).isoformat().replace("+00:00", "Z")) if ts is not None else None
        out.append(_make_job(
            "Lever", slug, title, location,
            j.get("hostedUrl", ""), content[:500],
            date_str, full_content=content,
        ))
    _record("Lever", slug, "ok" if out else "empty")
    return out


# ── The Muse (public API; cross-company, NYC + Product Management filtered) ────
# Unlike the fixed Greenhouse/Lever slug list, The Muse indexes thousands of
# companies and filters server-side by category + location + date. No auth, no
# Cloudflare — works from a plain script. This is the source that delivers FRESH,
# NYC, on-target roles instead of the same stale big-company pool.
# Docs: https://www.themuse.com/developers/api/v2
def fetch_themuse(pages: int = 4) -> list[dict]:
    base = "https://www.themuse.com/api/public/jobs"
    out: list[dict] = []
    seen_urls: set[str] = set()
    total = 0

    # Two passes: NYC and Flexible/Remote — The Muse filters server-side by category,
    # giving high precision. Deduped within this fetch by apply URL.
    searches = [
        ("New York, NY",       "NYC"),
        ("Flexible / Remote",  "remote"),
    ]

    for location_filter, label in searches:
        for page in range(pages):
            params = {
                "category":   "Product Management",
                "location":   location_filter,
                "page":       page,
                "descending": "true",
            }
            url  = base + "?" + urllib.parse.urlencode(params)
            data = _fetch(url)
            if not isinstance(data, dict):
                print(f"  [themuse] no/blocked response on page {page} ({label})",
                      file=sys.stderr)
                break
            results = data.get("results", [])
            if not results:
                break
            total += len(results)
            for j in results:
                title = j.get("name", "")
                if not _passes_title(title):
                    continue
                apply_url = (j.get("refs") or {}).get("landing_page", "")
                if not apply_url:
                    continue
                nu = _norm_url(apply_url)
                if nu in seen_urls:
                    continue
                seen_urls.add(nu)
                company  = (j.get("company") or {}).get("name", "") or "(unknown)"
                location = ", ".join(l.get("name", "") for l in j.get("locations", []))
                content  = _strip_html(j.get("contents", ""))
                date     = j.get("publication_date")
                out.append(_make_job(
                    "TheMuse", company, title, location,
                    apply_url, content[:500], date, full_content=content,
                ))
            if page + 1 >= data.get("page_count", pages + 1):
                break

    print(f"  fetch_themuse: {total} returned → {len(out)} matched IC-PM title")
    return out


# ── Remotive (public API; remote-first job board, no auth, no Cloudflare) ──────
# Second live source, complementing The Muse. Remotive is remote-only, so this
# deepens the US-REMOTE half of the goal (The Muse covers NYC). Every role is
# remote; we keep only those a US-based candidate can take (USA / North America /
# Worldwide / Anywhere) and drop region-locked foreign listings. No API key.
# Docs: https://github.com/remotive-com/remote-jobs-api
_REMOTIVE_US_OK = [
    "usa", "u.s.", "united states", "north america", "americas",
    "worldwide", "anywhere", "global", "remote",
]


def fetch_remotive(limit: int = 100) -> list[dict]:
    # Query by search term, not category: the "product" category is a small,
    # senior-heavy feed (Product Designer / Owner / Senior PM) that filters to
    # nothing. A "product manager" search returns actual PM-titled roles across
    # every category, giving the IC title filter something to keep.
    url  = ("https://remotive.com/api/remote-jobs"
            f"?search=product+manager&limit={limit}")
    data = _fetch(url)
    if not isinstance(data, dict):
        print("  [remotive] no/blocked response (check network)", file=sys.stderr)
        return []
    jobs = data.get("jobs", [])
    out, total = [], len(jobs)
    for j in jobs:
        title = j.get("title", "")
        if not _passes_title(title):
            continue
        apply_url = j.get("url", "")
        if not apply_url:
            continue
        region = (j.get("candidate_required_location", "") or "").strip()
        # US-eligibility gate: keep only regions a US candidate can work from.
        rlow = region.lower()
        if region and not any(ok in rlow for ok in _REMOTIVE_US_OK):
            continue
        company  = j.get("company_name", "") or "(unknown)"
        # All Remotive roles are remote — prefix so _loc_class reads "remote".
        location = f"Remote — {region}" if region else "Remote"
        content  = _strip_html(j.get("description", ""))
        date     = j.get("publication_date")   # ISO-8601, e.g. 2026-06-01T12:00:00
        out.append(_make_job(
            "Remotive", company, title, location,
            apply_url, content[:500], date, full_content=content,
        ))
    print(f"  fetch_remotive: {total} returned → {len(out)} matched IC-PM + US-remote")
    if total and not out:
        # Diagnostic: show why the first few were dropped so we can tune the filter.
        print("  [remotive-debug] filter breakdown on first 20 jobs:")
        counts: dict[str, int] = {}
        for j in (jobs[:20]):
            title  = j.get("title", "")
            region = (j.get("candidate_required_location", "") or "").strip()
            t      = title.lower()
            rlow   = region.lower()
            if not title:
                reason = "no-title"
            elif not any(kw in t for kw in TITLE_MUST_INCLUDE):
                reason = f"must-include-miss"
            elif any(kw in t for kw in TITLE_EXCLUDE):
                reason = "excluded-kw"
            elif _HARD_SENIOR_RE.search(t):
                reason = "hard-senior"
            elif _SENIORITY_RE.search(t.split(",", 1)[0]):
                reason = "soft-senior"
            elif region and not any(ok in rlow for ok in _REMOTIVE_US_OK):
                reason = "non-us-region"
            else:
                reason = "passed-all?"
            counts[reason] = counts.get(reason, 0) + 1
            print(f"    {reason:20s}  {title!r}  [{region}]")
        print(f"  [remotive-debug] summary: {counts}")
    return out


# ── Adzuna (paid API; broad US job index, keyword-search — no slug list needed) ─
# Unlike the ATS fetchers, Adzuna is a search API: we query "product manager"
# across all US employers and let the existing title/location filters do the work.
# Credentials are read at runtime from adzuna_key.txt (line 1: app_id, line 2: app_key).
# Docs: https://developer.adzuna.com/docs/search
_ADZUNA_KEY_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "adzuna_key.txt")


def _load_adzuna_creds() -> tuple[str, str] | None:
    try:
        lines = open(_ADZUNA_KEY_FILE).read().strip().splitlines()
        return lines[0].strip(), lines[1].strip()
    except Exception:
        return None


def fetch_adzuna(pages_per_search: int = 4) -> list[dict]:
    creds = _load_adzuna_creds()
    if not creds:
        print("  [adzuna] skipped: adzuna_key.txt missing or malformed", file=sys.stderr)
        return []
    app_id, app_key = creds

    base = "https://api.adzuna.com/v1/api/jobs/us/search"
    out: list[dict] = []
    seen_urls: set[str] = set()
    total = 0

    # Two targeted searches: NYC (location-anchored) + nationwide (remote discovery).
    # The nationwide pass uses no `where` so Adzuna returns all US results; our
    # existing _loc_class() then keeps only remote/remote+nyc classified roles.
    searches = [
        {"what_phrase": "product manager", "where": "New York City, NY"},
        {"what_phrase": "product manager"},
    ]

    for search_params in searches:
        for page in range(1, pages_per_search + 1):
            params = {
                "app_id":           app_id,
                "app_key":          app_key,
                "results_per_page": 50,
                "sort_by":          "date",
                "max_days_old":     MAX_AGE_DAYS,
                "content-type":     "application/json",
                **search_params,
            }
            url  = f"{base}/{page}?" + urllib.parse.urlencode(params)
            data = _fetch(url)
            if not isinstance(data, dict):
                print(f"  [adzuna] no response on page {page} ({search_params})", file=sys.stderr)
                break
            results = data.get("results", [])
            if not results:
                break
            total += len(results)
            for j in results:
                title = j.get("title", "")
                if not _passes_title(title):
                    continue
                apply_url = j.get("redirect_url", "")
                if not apply_url:
                    continue
                nu = _norm_url(apply_url)
                if nu in seen_urls:
                    continue
                seen_urls.add(nu)
                company  = (j.get("company") or {}).get("display_name", "") or "(unknown)"
                location = (j.get("location") or {}).get("display_name", "") or ""
                content  = _strip_html(j.get("description", ""))
                date_str = j.get("created")   # ISO-8601 e.g. "2026-06-01T12:00:00Z"
                out.append(_make_job(
                    "Adzuna", company, title, location,
                    apply_url, content[:500], date_str, full_content=content,
                ))

    print(f"  fetch_adzuna: {total} returned → {len(out)} matched IC-PM title")
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
        hw   = "  *** HW/MEDTECH SIGNAL ***" if j.get("hw_signal") else ""
        lang = "  🌐 LANG" if j.get("lang_signal") else ""
        print(f"[{j['source']:11s}] {j['company']:18s}  {j['title']}{hw}{lang}")
        print(f"  {(j['location'] or j['loc_class']):32s}  age={age:<6s}  yrs_req={yrs}")
        if j["years_context"]:
            print(f"  \"{j['years_context'][:90]}\"")
        print(f"  {j['url']}\n")

    if skipped:
        print(f"(skipped {skipped} already-applied role(s) from {APPLIED_FILE})\n")

    fields = ["source", "company", "title", "location", "loc_class",
              "url", "days_old", "years_raw", "years_context", "years_sentence",
              "hw_signal", "lang_signal", "applied"]
    with open(OUTPUT_FILE, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        w.writerows(jobs)
    print(f"Saved {len(jobs)} role(s) → {OUTPUT_FILE}")
    print(f"Fill the 'applied' column as you go. Save as {APPLIED_FILE} to dedupe next run.")


# ── local mode ────────────────────────────────────────────────────────────────

def cmd_local(remote_only: bool, include_unknown_loc: bool = False):
    _slug_resolution.clear()

    # ── Gmail company-level dedupe (primary source of truth) ──────────────────
    gmail_set, gmail_evidence = _gmail_applied_set()
    print(f"\nGmail applied set: {len(gmail_set)} companies")
    if gmail_set:
        for norm, raw, subj, date in sorted(gmail_evidence, key=lambda x: x[0]):
            print(f"  {raw:<28s} ← \"{subj[:55]}\"  ({date[:10]})")
    else:
        print("  (none — run python3 pmfarm_gmail_sync.py to enable)")

    # ── ATS fetch ─────────────────────────────────────────────────────────────
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

    # ── The Muse: fresh NYC Product-Management roles across all companies
    #    (primary source of relevance — the fixed ATS list above is supplementary;
    #    hiring.cafe is unusable: fully behind Cloudflare bot protection) ────────
    print("\nQuerying The Muse for fresh NYC Product Management roles…")
    try:
        raw.extend(fetch_themuse())
    except Exception as e:
        print(f"  [themuse] skipped: {e}", file=sys.stderr)

    # ── Remotive: fresh US-remote Product roles ───────────────────────────────
    print("Querying Remotive for fresh US-remote Product roles…")
    try:
        raw.extend(fetch_remotive())
    except Exception as e:
        print(f"  [remotive] skipped: {e}", file=sys.stderr)

    # ── Adzuna: broad US keyword search — no slug list, indexes all employers ──
    print("Querying Adzuna for US Product Manager roles…")
    try:
        raw.extend(fetch_adzuna())
    except Exception as e:
        print(f"  [adzuna] skipped: {e}", file=sys.stderr)

    # ── freshness filter: drop stale postings with a known age > MAX_AGE_DAYS ──
    def _too_old(j: dict) -> bool:
        try:
            return int(j["days_old"]) > MAX_AGE_DAYS
        except (ValueError, TypeError):
            return False   # unknown age → keep (can't judge)
    stale = [j for j in raw if _too_old(j)]
    if stale:
        print(f"\nDropped {len(stale)} stale role(s) older than {MAX_AGE_DAYS}d "
              f"(e.g. {', '.join(sorted({j['company'] for j in stale}))[:80]})")
    raw = [j for j in raw if not _too_old(j)]

    # ── location filter (unknown excluded by default — P2) ───────────────────
    unknown_count = sum(1 for j in raw if j["loc_class"] == "unknown")
    filtered = [j for j in raw if _passes_location(j["loc_class"], remote_only, include_unknown_loc)]
    excluded_unknown = sum(1 for j in raw if j["loc_class"] == "unknown"
                          and not _passes_location("unknown", remote_only, include_unknown_loc))

    # ── applied.csv URL/name dedupe (fallback, manual) ────────────────────────
    applied = _load_applied()
    pre_url = len(filtered)
    url_deduped = _deduped(filtered, applied)
    skipped_url = pre_url - len(url_deduped)

    # ── Gmail company-level dedupe ────────────────────────────────────────────
    gmail_skipped: list[tuple] = []
    jobs: list[dict] = []
    if gmail_set:
        for j in url_deduped:
            norm = _normalize_company(j["company"])
            if norm in gmail_set:
                ev = next((e for e in gmail_evidence if e[0] == norm), None)
                gmail_skipped.append((j, ev))
            else:
                jobs.append(j)
    else:
        jobs = url_deduped

    if gmail_skipped:
        print(f"\nRoles skipped by Gmail dedupe: {len(gmail_skipped)}")
        for j, ev in gmail_skipped[:10]:
            ev_str = (f"\"{ev[2][:50]}\" ({ev[3][:10]})" if ev
                      else f"\"{j['company']}\" matched")
            print(f"  {j['company']:<24s} {j['title'][:45]}")
            print(f"    ← {ev_str}")
        if len(gmail_skipped) > 10:
            print(f"  … and {len(gmail_skipped) - 10} more")

    nyc_count     = sum(1 for j in filtered if j["loc_class"] == "nyc")
    remote_count  = sum(1 for j in filtered if j["loc_class"] in ("remote", "remote+nyc"))
    print(f"\n{len(raw)} title-matched → {len(filtered)} after location "
          f"(NYC={nyc_count} remote={remote_count} excluded-unknown={excluded_unknown}) "
          f"→ {len(url_deduped)} after URL-dedupe → {len(jobs)} after Gmail-dedupe")
    if skipped_url:
        print(f"(also skipped {skipped_url} by applied.csv URL/name match)")
    if excluded_unknown and not include_unknown_loc:
        print(f"(tip: --include-unknown-loc to see the {excluded_unknown} unclassified roles)")

    # ── slug resolution table (P4 observability) ──────────────────────────────
    if _slug_resolution:
        ok_n    = sum(1 for v in _slug_resolution.values() if v == "ok")
        empty_n = sum(1 for v in _slug_resolution.values() if v == "empty")
        fail_n  = sum(1 for v in _slug_resolution.values() if v == "fail")
        total_n = len(_slug_resolution)
        print(f"\nSlug hit-rate: {ok_n}/{total_n} with PM roles | "
              f"{empty_n} resolved-empty | {fail_n} failed (404/timeout)")
        if fail_n:
            failed = [k for k, v in sorted(_slug_resolution.items()) if v == "fail"]
            print("  Failed slugs: " + ", ".join(failed[:20])
                  + (f" … +{fail_n - 20} more" if fail_n > 20 else ""))

        # Persist this run's outcomes so companies.db self-cleans: live slugs
        # rise, repeatedly-failing ones get pruned from future runs.
        try:
            import companydb, os
            if os.path.exists(companydb.DB_FILE):
                companydb.record_results(_slug_resolution)
                print(f"  (recorded results to {companydb.DB_FILE})")
        except Exception as e:
            print(f"  [warn] could not update companies.db: {e}", file=sys.stderr)

    _output(jobs, 0)


# ── entry point ───────────────────────────────────────────────────────────────

def main():
    p = argparse.ArgumentParser(
        description="PM role scraper — Greenhouse, Ashby, Lever",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    p.add_argument("--remote-only", action="store_true",
                   help="Remote/US-wide roles only; drop NYC-specific listings")
    p.add_argument("--all-levels", action="store_true",
                   help="Keep Senior/Staff/Principal/Lead PM roles too (no seniority filter)")
    p.add_argument("--include-unknown-loc", action="store_true",
                   help="Include roles whose location could not be classified (may include non-NYC/non-remote)")
    args = p.parse_args()

    global INCLUDE_SENIOR
    INCLUDE_SENIOR = args.all_levels

    cmd_local(args.remote_only, args.include_unknown_loc)


if __name__ == "__main__":
    main()
