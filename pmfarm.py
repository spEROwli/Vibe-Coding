#!/usr/bin/env python3
"""
pmfarm.py — role scraper: Greenhouse · Ashby · Lever (· hiring.cafe via Bright Data)

  python3 pmfarm.py [--remote-only] [--include-unknown-loc]

Targets: Product Manager, APM, Technical Program Manager, Forward Deployed Engineer,
         Solutions Engineer, Solutions Architect, Business Operations, Strategy & Ops.
NYC hard constraint + US remote. Experience bar: 0-3 yrs stated or unstated.
All data comes from live ATS JSON APIs. See SCRAPER_RULES.md.
Dedupe: gmail_applied.txt (primary) + applied.csv (fallback).
"""

import argparse, csv, datetime, html as H, json, os, re, shutil, subprocess, sys, urllib.request, urllib.parse
from concurrent.futures import ThreadPoolExecutor, as_completed

# ── SOURCES (parametric toggles) ──────────────────────────────────────────────
# Master switch per source. Adding or tuning a source is a config edit here, not
# surgery in cmd_local. The ATS sources (greenhouse/ashby/lever) are live; the
# disabled aggregators (themuse/remotive/adzuna) are not toggled here because they
# are intentionally not called at all. "brightdata" (hiring.cafe) ships OFF and
# stays inert — zero spend — until a key + collector exist. See BRIGHTDATA_SETUP.md.
SOURCES = {
    "greenhouse": True,
    "ashby":      True,
    "lever":      True,
    "brightdata": False,
}

# ── Bright Data / hiring.cafe credentials (gitignored, same pattern as Adzuna) ─
# brightdata_key.txt holds three lines:
#   line1 = BRIGHTDATA_API_KEY · line2 = BRIGHTDATA_COLLECTOR_ID · line3 = HIRINGCAFE_SEARCH_URL
# Absent or blank → fetch_brightdata() is inert (returns []). Pay-as-you-go cost is
# ~$1.50 / 1k page loads; the guardrails live in fetch_brightdata().
_BRIGHTDATA_KEY_FILE  = os.path.join(os.path.dirname(os.path.abspath(__file__)), "brightdata_key.txt")
_BRIGHTDATA_RUN_STAMP = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".brightdata_last_run")
BRIGHTDATA_CLI        = os.environ.get("BRIGHTDATA_CLI", "bdata")  # CLI name or absolute path
MAX_PAGE_LOADS        = 50   # page-load budget per run (documented cap; see fetch_brightdata note)
BRIGHTDATA_MIN_HOURS  = 20   # once-per-day spend lock: skip if a paid run ran < this many hours ago


def _load_brightdata_creds() -> tuple[str, str, str] | None:
    """Return (api_key, collector_id, search_url) from brightdata_key.txt, or None
    when the file is absent or any line is blank. Blank/whitespace-only lines count
    as missing so a CI step that echoes an *unset* secret into the file stays inert
    rather than erroring (mirrors _load_adzuna_creds tolerance)."""
    try:
        lines = [ln.strip() for ln in
                 open(_BRIGHTDATA_KEY_FILE, encoding="utf-8").read().splitlines()]
        api_key      = lines[0] if len(lines) > 0 else ""
        collector_id = lines[1] if len(lines) > 1 else ""
        search_url   = lines[2] if len(lines) > 2 else ""
        if not (api_key and collector_id and search_url):
            return None
        return (api_key, collector_id, search_url)
    except Exception:
        return None


# ── FILTERS ──────────────────────────────────────────────────────────────────
TITLE_MUST_INCLUDE = [
    # Product Management
    "product manager", "associate product", "technical product",
    "hardware product", "apm program", "rotational product",
    "product owner",
    # Deployment (Palantir-style)
    "deployment strategist",
    # Technical Program Management
    "technical program manager",
    # Forward Deployed Engineering
    "forward deployed engineer", "forward deployed",
    # Solutions
    "solutions engineer", "solutions architect",
    # Operations
    "business operations", "strategy and operations",
    "strategy & operations", "biz ops", "bizops", "strat ops",
]
TITLE_EXCLUDE = ["marketing", "product marketing"]

# Unambiguous executive titles — categorically above the IC bar, dropped on the
# title alone. NOTE: "senior associate" is deliberately NOT here — at many firms
# (e.g. Capital One) it is an early-career, 1-2-years-out title and must pass.
# Seniority is judged by the stated YEARS requirement (EXPERIENCE_CAP), not by
# soft title words like "Senior"/"Lead", which are noisy and produce false drops.
_HARD_SENIOR_RE = re.compile(
    r'(?<!\w)(?:vice\s+president|vp|svp|evp|head\s+of|'
    r'principal|staff|director|managing\s+director|md)(?!\w)',
    re.IGNORECASE,
)

# Experience bar (parametric knob). Keep roles requiring <= this many years, plus
# any role with no stated requirement. Roles explicitly requiring MORE are dropped
# at scrape time. The parsed years value is the ground truth — never the title.
EXPERIENCE_CAP = 3

NYC_LOCS    = ["new york", "nyc", "brooklyn", "manhattan"]
SF_LOCS     = [
    "san francisco", "bay area", "silicon valley", "south bay", "east bay",
    "palo alto", "mountain view", "menlo park", "redwood city",
    "san jose", "cupertino", "santa clara", "sunnyvale",
    "san mateo", "foster city", "burlingame", "pleasanton", "san carlos",
    "oakland",
]
REMOTE_LOCS = ["remote", "united states", "anywhere", "nationwide",
               "distributed", "work from anywhere", "work from home"]

# Non-target US metros. A location naming one of these (without "remote") gets
# loc_class="unknown" and is excluded by default. SF metro cities were removed —
# they are now a target geography detected by SF_LOCS above.
_US_NON_NYC = [
    "seattle", "chicago", "austin", "boston",
    "los angeles", "denver", "atlanta", "portland", "miami",
    "dallas", "houston", "phoenix",
    "bellevue", "kirkland", "cambridge", "pittsburgh",
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

# Phrases that frame a year bar as a preferred / nice-to-have qualifier rather
# than a hard requirement. Used by _requirement_kind so a soft LOW bar
# ("Strongly Preferred: 2+ years") can't sink the gate below a hard HIGH bar
# ("Required: 5+ years") — while a plain "5+ … 2+ …" with no qualifier words
# still reads optimistically as 2 (TEST 3). Hard markers let an explicit
# "Required"/"Minimum" clause out-vote a nearby preferred header.
YEARS_SOFT_MARKERS = ("preferred", "preferably", "nice to have", "nice-to-have",
                      "bonus", "ideally", "a plus", "good to have", "desired",
                      "would be a plus", "pluses")
YEARS_HARD_MARKERS = ("required", "requirement", "must have", "must-have",
                      "minimum", "at least", "you must", "we require")

# Sentence/clause separator. _strip_html turns every <li>/<p>/<h*> into ". ",
# so each requirement bullet ends up as its own clause splittable on this.
_SENT_SEP = re.compile(r"[.!?;]\s")

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


def _clause_bounds(text: str, start: int, end: int) -> tuple[int, int]:
    """[left, right) of the sentence/clause containing the match at [start, end).
    Because _strip_html makes every requirement bullet its own ". "-delimited
    clause, this isolates the one phrase the year number actually belongs to."""
    left = 0
    for m in _SENT_SEP.finditer(text, 0, start):
        left = m.end()
    nxt = _SENT_SEP.search(text, end)
    return left, (nxt.start() if nxt else len(text))


def _preceding_sentence(text: str, clause_left: int) -> str:
    """The sentence immediately before clause_left — catches header-style
    qualifiers like a standalone 'Strongly Preferred.' line above the bullet."""
    if clause_left <= 0:
        return ""
    head = text[:clause_left]
    seps = list(_SENT_SEP.finditer(head))
    prev = seps[-2].end() if len(seps) >= 2 else 0
    return head[prev:]


def _nearest_marker_kind(s: str, pos: int) -> str | None:
    """'soft' / 'hard' / None for the qualifier marker nearest offset `pos` in
    `s`, so the closest framing word governs the number."""
    s = s.lower()
    best_kind, best_dist = None, None
    for markers, kind in ((YEARS_SOFT_MARKERS, "soft"), (YEARS_HARD_MARKERS, "hard")):
        for mk in markers:
            i = s.find(mk)
            while i != -1:
                d = abs(i - pos)
                if best_dist is None or d < best_dist:
                    best_kind, best_dist = kind, d
                i = s.find(mk, i + 1)
    return best_kind


def _requirement_kind(text: str, start: int, end: int) -> str:
    """'soft' if the year bar at [start, end) is framed as preferred/nice-to-have,
    else 'hard'. The number's OWN clause wins outright; only a clause with no
    marker falls back to its preceding header sentence. This keeps a trailing
    qualifier in one clause ('5+ years preferred. 2+ years required.') from
    leaking onto the next number. Unmarked → 'hard'."""
    left, right = _clause_bounds(text, start, end)
    kind = _nearest_marker_kind(text[left:right], start - left)
    if kind is None:
        head = _preceding_sentence(text, left)
        kind = _nearest_marker_kind(head, len(head))
    return kind or "hard"


def _parse_years(text: str) -> tuple[str, str]:
    """Return (years_raw, years_context).

    Honest-by-design: collects every requirement-style year mention, drops
    team/collective-tenure phrasing, and reports the MINIMUM HARD bar found (the
    most optimistic read, so you never self-reject). A bar framed as preferred /
    nice-to-have is set aside so it can't pull the gate below a real requirement
    — but if a posting states ONLY soft bars, those still count (a sole
    "preferred 8+ years" drops as before). The context column shows every matched
    phrase so you can catch the number if it's lying. No match → unknown.
    """
    hard:     list[int] = []
    soft:     list[int] = []
    contexts: list[str] = []
    for pattern, mode in YEARS_PATTERNS:
        for m in pattern.finditer(text):
            span = m.group(0).lower()
            if any(bad in span for bad in YEARS_DISQUALIFY):
                continue
            nums = [int(g) for g in m.groups() if g]
            if not nums:
                continue
            value  = nums[0] if mode == "low" else min(nums)
            bucket = soft if _requirement_kind(text, m.start(), m.end()) == "soft" else hard
            bucket.append(value)
            s   = max(0, m.start() - 30)
            e   = min(len(text), m.end() + 30)
            ctx = "…" + text[s:e].replace("\n", " ").strip() + "…"
            if ctx not in contexts:
                contexts.append(ctx)

    # Gate on hard requirements; soft bars only count when there are no hard
    # ones, so the optimistic low-end never falls onto a "preferred" qualifier.
    pool = hard or soft
    if not pool:
        return ("unknown", "")
    return (str(min(pool)), " | ".join(contexts[:3]))


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
    has_nyc = any(n in loc_lower for n in NYC_LOCS)
    has_sf  = any(s in loc_lower for s in SF_LOCS)

    # A named foreign country/region makes this international unless it also names
    # a target city — "London + San Francisco" is still a SF role.
    has_intl = any(m in loc_lower for m in _INTL_MARKERS)
    if has_intl and not has_nyc and not has_sf:
        return "international"

    # Explicit "remote" in the location field always wins.
    has_explicit_remote = "remote" in loc_lower

    # Generic US terms only count when no specific non-target city is present,
    # preventing "Seattle, United States" from matching as remote.
    has_non_target_us = any(c in loc_lower for c in _US_NON_NYC)
    has_us_generic    = (not has_non_target_us) and any(r in loc_lower for r in REMOTE_LOCS)

    has_remote = has_explicit_remote or has_us_generic

    if has_remote and has_nyc and has_sf: return "remote+nyc+sf"
    if has_remote and has_nyc:            return "remote+nyc"
    if has_remote and has_sf:             return "remote+sf"
    if has_remote:                        return "remote"
    if has_nyc    and has_sf:             return "nyc+sf"
    if has_nyc:                           return "nyc"
    if has_sf:                            return "sf"
    if has_non_target_us:                 return "unknown"
    if location.strip():                  return "international"
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
    if not any(kw in t for kw in TITLE_MUST_INCLUDE):
        return False
    if any(kw in t for kw in TITLE_EXCLUDE):
        return False
    # Only unambiguous executive titles disqualify on the title alone. Seniority
    # otherwise is enforced by the stated-years gate (EXPERIENCE_CAP) downstream,
    # so a "Senior Associate" or a "Senior PM" with a <=3yr bar is NOT dropped here.
    if _HARD_SENIOR_RE.search(t):
        return False
    return True


def _passes_location(lc: str, remote_only: bool, include_unknown: bool = False) -> bool:
    if remote_only:
        allowed = {"remote", "remote+nyc", "remote+sf", "remote+nyc+sf"}
    else:
        allowed = {"remote", "remote+nyc", "nyc",
                   "sf", "remote+sf", "nyc+sf", "remote+nyc+sf"}
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


# ── The Muse (public API; cross-company, NYC + multiple categories) ────────────
# Filters server-side by category + location. We query several categories so that
# SE/SA/FDE/Ops roles appear alongside PM roles. _passes_title() handles precision.
# Docs: https://www.themuse.com/developers/api/v2
def fetch_themuse(pages: int = 3) -> list[dict]:
    base = "https://www.themuse.com/api/public/jobs"
    out: list[dict] = []
    seen_urls: set[str] = set()
    total = 0

    location_passes = [
        ("New York, NY",       "NYC"),
        ("San Francisco, CA",  "SF"),
        ("Flexible / Remote",  "remote"),
    ]
    # Categories that cover all target role types.
    categories = [
        "Product Management",   # PM / APM / TPM
        "Engineering",          # TPM / FDE / SE / SA
        "Operations",           # BizOps / StratOps
        "Sales",                # SE / SA often appear here
    ]

    for location_filter, loc_label in location_passes:
        for category in categories:
            # Skip Engineering/Sales remote pass — thin results there;
            # Adzuna/Remotive cover remote SE/SA better.
            if loc_label == "remote" and category in ("Engineering", "Sales"):
                continue
            for page in range(pages):
                params = {
                    "category":   category,
                    "location":   location_filter,
                    "page":       page,
                    "descending": "true",
                }
                url  = base + "?" + urllib.parse.urlencode(params)
                data = _fetch(url)
                if not isinstance(data, dict):
                    print(f"  [themuse] no/blocked response ({category}/{loc_label} p{page})",
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

    print(f"  fetch_themuse: {total} returned → {len(out)} matched title filter")
    return out


# ── Remotive (public API; remote-first job board, no auth, no Cloudflare) ──────
# Remote-only source. We run multiple searches to cover all target role types.
# Every role is remote; we keep only those a US-based candidate can take.
# Docs: https://github.com/remotive-com/remote-jobs-api
_REMOTIVE_US_OK = [
    "usa", "u.s.", "united states", "north america", "americas",
    "worldwide", "anywhere", "global", "remote",
]

_REMOTIVE_SEARCHES = [
    "product+manager",
    "solutions+engineer",
    "solutions+architect",
    "technical+program+manager",
    "forward+deployed",
    "business+operations",
    "strategy+operations",
]


def fetch_remotive(limit: int = 50) -> list[dict]:
    out: list[dict] = []
    seen_urls: set[str] = set()
    grand_total = 0

    for term in _REMOTIVE_SEARCHES:
        url  = f"https://remotive.com/api/remote-jobs?search={term}&limit={limit}"
        data = _fetch(url)
        if not isinstance(data, dict):
            print(f"  [remotive] no/blocked response for {term!r}", file=sys.stderr)
            continue
        jobs = data.get("jobs", [])
        grand_total += len(jobs)
        for j in jobs:
            title = j.get("title", "")
            if not _passes_title(title):
                continue
            apply_url = j.get("url", "")
            if not apply_url:
                continue
            nu = _norm_url(apply_url)
            if nu in seen_urls:
                continue
            seen_urls.add(nu)
            region = (j.get("candidate_required_location", "") or "").strip()
            rlow = region.lower()
            if region and not any(ok in rlow for ok in _REMOTIVE_US_OK):
                continue
            company  = j.get("company_name", "") or "(unknown)"
            location = f"Remote — {region}" if region else "Remote"
            content  = _strip_html(j.get("description", ""))
            date     = j.get("publication_date")
            out.append(_make_job(
                "Remotive", company, title, location,
                apply_url, content[:500], date, full_content=content,
            ))

    print(f"  fetch_remotive: {grand_total} returned → {len(out)} matched title + US-remote")
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


# NYC-targeted search terms (all target role types).
_ADZUNA_NYC = [
    "product manager",
    "associate product manager",
    "technical program manager",
    "solutions engineer",
    "solutions architect",
    "forward deployed engineer",
    "business operations",
    "strategy and operations",
]

# Nationwide search terms (remote discovery — subset most likely to be remote).
_ADZUNA_NATIONAL = [
    "product manager",
    "technical program manager",
    "solutions engineer",
    "solutions architect",
]


def fetch_adzuna(pages_per_search: int = 2) -> list[dict]:
    creds = _load_adzuna_creds()
    if not creds:
        print("  [adzuna] skipped: adzuna_key.txt missing or malformed", file=sys.stderr)
        return []
    app_id, app_key = creds

    base = "https://api.adzuna.com/v1/api/jobs/us/search"
    out: list[dict] = []
    seen_urls: set[str] = set()
    total = 0

    searches = (
        [{"what_phrase": t, "where": "New York City, NY"} for t in _ADZUNA_NYC] +
        [{"what_phrase": t, "where": "San Francisco, CA"} for t in _ADZUNA_NYC] +
        [{"what_phrase": t}                               for t in _ADZUNA_NATIONAL]
    )

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
                date_str = j.get("created")
                out.append(_make_job(
                    "Adzuna", company, title, location,
                    apply_url, content[:500], date_str, full_content=content,
                ))

    print(f"  fetch_adzuna: {total} returned → {len(out)} matched title filter")
    return out


# ── Bright Data / hiring.cafe (gated source, reached via the `bdata` CLI) ──────
# hiring.cafe is JS-gated and unreachable by the urllib fetchers above; a Bright
# Data Scraper Studio collector renders + extracts it. This fetcher is INERT by
# default — it returns [] unless SOURCES["brightdata"] is True AND a complete
# brightdata_key.txt exists. Rows are mapped into the standard _make_job() shape
# and then flow through the EXACT same experience gate + dedupe as the ATS rows in
# cmd_local; no filtering logic is duplicated here.
#
# Cost (pay-as-you-go, ~$1.50 / 1k page loads) is guarded three ways:
#   1. inert-by-default toggle SOURCES["brightdata"] — the master spend switch;
#   2. a once-per-day lock (BRIGHTDATA_MIN_HOURS) so a manual re-run cannot re-bill;
#   3. a tight, pre-filtered search URL (fewer results = fewer page loads).
# NOTE: bdata v0.3.1 `scraper run` exposes no --max-pages flag, so MAX_PAGE_LOADS
# is enforced client-side as a hard cap on the rows we accept (a backstop, not a
# server-side billing cap). The real spend protections are the toggle + daily lock
# + a narrow search URL. Always confirm spend with `bdata budget balance`.

# The AI-built collector decides the exact JSON field names, so each logical field
# is looked up across the likely variants. The names to request are spelled out in
# BRIGHTDATA_SETUP.md so the collector emits keys this map already covers.
_BD_FIELDS = {
    "title":    ["title", "job_title", "role", "position", "name"],
    "company":  ["company", "company_name", "employer", "organization", "org"],
    "location": ["location", "locations", "city", "job_location", "place"],
    "url":      ["apply_url", "apply_link", "external_apply_url", "external_url",
                 "direct_url", "job_url", "url", "link"],
    "years":    ["years_required", "years_required_text", "experience_required",
                 "experience", "years", "yoe"],
    "content":  ["description", "job_description", "content", "summary", "details"],
    "date":     ["post_date", "posted_date", "date_posted", "published_at",
                 "created_at", "date", "posted"],
}


def _bd_get(row: dict, field: str) -> str:
    """First non-empty value for a logical field across its candidate keys.
    Lists are joined (e.g. multiple locations) so downstream string logic works."""
    for key in _BD_FIELDS[field]:
        v = row.get(key)
        if isinstance(v, list):
            v = ", ".join(str(x) for x in v if x)
        if v:
            return str(v).strip()
    return ""


def _bd_recent_run() -> bool:
    """True if a paid brightdata run completed < BRIGHTDATA_MIN_HOURS ago. Backs the
    once-per-day spend lock so a manual re-run cannot trigger repeated paid scrapes."""
    try:
        ts = float(open(_BRIGHTDATA_RUN_STAMP).read().strip())
        return (datetime.datetime.now().timestamp() - ts) < BRIGHTDATA_MIN_HOURS * 3600
    except Exception:
        return False


def fetch_brightdata() -> list[dict]:
    # 1. Inert unless explicitly enabled AND fully configured. Both checks run
    #    BEFORE any subprocess, so the disabled path costs nothing and never errors.
    if not SOURCES.get("brightdata"):
        return []
    creds = _load_brightdata_creds()
    if not creds:
        print("  [brightdata] inert: brightdata_key.txt missing or incomplete", file=sys.stderr)
        return []
    api_key, collector_id, search_url = creds

    # 2. Once-per-day spend lock — a manual re-run within the window is free.
    if _bd_recent_run():
        print(f"  [brightdata] skipped: a paid run completed < {BRIGHTDATA_MIN_HOURS}h ago "
              f"(spend lock). Delete {os.path.basename(_BRIGHTDATA_RUN_STAMP)} to force a re-run.",
              file=sys.stderr)
        return []

    # 3. CLI must be on PATH; if absent, stay inert rather than break the ATS run.
    if shutil.which(BRIGHTDATA_CLI) is None:
        print(f"  [brightdata] inert: '{BRIGHTDATA_CLI}' CLI not found on PATH "
              "(npm install -g @brightdata/cli)", file=sys.stderr)
        return []

    # 4. Run the collector. API key passed via env (skips `bdata login`); never in
    #    argv (would leak in `ps`). --json for parsing, --timeout bounds a hung job.
    # A hiring.cafe *search* URL is paginated, so a real run usually hits Scraper
    # Studio's batch auto-fallback (longer poll). Give the CLI a generous --timeout
    # and the subprocess a slightly longer wall-clock cap so a legit big batch isn't
    # killed mid-poll. If it still times out, the CLI returns empty → inert, not error.
    env = dict(os.environ, BRIGHTDATA_API_KEY=api_key)
    cmd = [BRIGHTDATA_CLI, "scraper", "run", collector_id, search_url, "--json", "--timeout", "1800"]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, env=env, timeout=2000)
    except Exception as e:
        print(f"  [brightdata] run failed: {e}", file=sys.stderr)
        return []
    if proc.returncode != 0:
        print(f"  [brightdata] non-zero exit ({proc.returncode}): "
              f"{(proc.stderr or '').strip()[:200]}", file=sys.stderr)
        return []

    # 5. The run was billed the moment it returned 0 — stamp the lock now, before
    #    any parsing/filtering, so a re-run cannot re-bill even if mapping fails.
    try:
        open(_BRIGHTDATA_RUN_STAMP, "w").write(str(datetime.datetime.now().timestamp()))
    except Exception:
        pass

    # 6. Parse rows: accept a bare array or a {data|results|...: [...]} wrapper.
    try:
        payload = json.loads(proc.stdout or "[]")
    except Exception as e:
        print(f"  [brightdata] could not parse JSON output: {e}", file=sys.stderr)
        return []
    if isinstance(payload, dict):
        for k in ("data", "results", "rows", "items", "output"):
            if isinstance(payload.get(k), list):
                payload = payload[k]
                break
        else:
            payload = [payload]
    if not isinstance(payload, list):
        return []

    # 7. Map → standard _make_job() shape. Fold the years-required text AND the
    #    description into `content` so the SAME _parse_years experience gate
    #    downstream actually sees the requirement (otherwise hiring.cafe rows would
    #    sail past the one rule that matters). MAX_PAGE_LOADS caps rows as a backstop.
    out: list[dict] = []
    for r in payload[:MAX_PAGE_LOADS]:
        if not isinstance(r, dict):
            continue
        title = _bd_get(r, "title")
        url   = _bd_get(r, "url")
        if not _passes_title(title) or not url:
            continue
        company  = _bd_get(r, "company") or "(unknown)"
        location = _bd_get(r, "location")
        years_t  = _bd_get(r, "years")
        desc     = _bd_get(r, "content")
        content  = _strip_html(" . ".join(p for p in (years_t, desc) if p)) or title
        date_str = _bd_get(r, "date") or None
        out.append(_make_job(
            "hiring.cafe", company, title, location,
            url, content[:500], date_str, full_content=content,
        ))

    print(f"  fetch_brightdata: {len(payload)} row(s) → {len(out)} matched title filter")
    return out


# ── output ─────────────────────────────────────────────────────────────────────

def _sort_key(j: dict) -> tuple:
    loc_order = {
        "remote": 0, "remote+nyc": 1, "remote+sf": 1, "remote+nyc+sf": 1,
        "nyc": 2, "sf": 2, "nyc+sf": 2,
        "unknown": 3,
    }
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

    # ── ATS fetch (each provider gated by its SOURCES toggle) ─────────────────
    tasks: list[tuple] = []
    if SOURCES.get("greenhouse"):
        tasks += [(fetch_greenhouse, s) for s in GREENHOUSE]
    if SOURCES.get("ashby"):
        tasks += [(fetch_ashby, s) for s in ASHBY]
    if SOURCES.get("lever"):
        tasks += [(fetch_lever, s) for s in LEVER]

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

    # ── Bright Data / hiring.cafe (gated source) ──────────────────────────────
    # Inert unless SOURCES["brightdata"] is True AND brightdata_key.txt is set up
    # (see BRIGHTDATA_SETUP.md). Its rows join `raw` and pass through the SAME
    # freshness + experience gate + location filter + dedupe below as the ATS rows.
    if SOURCES.get("brightdata"):
        raw.extend(fetch_brightdata())

    # ── ATS-DIRECT ONLY ──────────────────────────────────────────────────────
    # Aggregator sources (The Muse, Remotive, Adzuna) are intentionally NOT called.
    # Adzuna redirects through its own domain instead of the company apply page;
    # The Muse links to its own landing pages; Remotive returns ~0 matches. Every
    # role here comes from Greenhouse/Ashby/Lever (and, once enabled, hiring.cafe
    # direct apply links), whose URLs are the company's own application page — a
    # clean direct link, every time. (fetch_themuse / fetch_remotive / fetch_adzuna
    # remain defined but unused.)

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

    # ── experience-bar gate (hard requirement): drop ONLY roles that explicitly
    #    state more than EXPERIENCE_CAP years. Unstated years and 0-EXPERIENCE_CAP
    #    both pass. years_raw is the optimistic low-end from _parse_years, so a
    #    "3-5 years" role reads as 3 and is kept. Title seniority never gates here.
    def _over_bar(j: dict) -> bool:
        try:
            return int(j["years_raw"]) > EXPERIENCE_CAP
        except (ValueError, TypeError):
            return False   # "unknown" / unstated → keep
    over = [j for j in raw if _over_bar(j)]
    if over:
        print(f"Dropped {len(over)} role(s) explicitly requiring >{EXPERIENCE_CAP}yrs "
              f"(e.g. {', '.join(sorted({j['company'] for j in over}))[:80]})")
    raw = [j for j in raw if not _over_bar(j)]

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

    _NYC_CLASSES = {"nyc", "remote+nyc", "nyc+sf", "remote+nyc+sf"}
    _SF_CLASSES  = {"sf",  "remote+sf",  "nyc+sf", "remote+nyc+sf"}
    nyc_count    = sum(1 for j in filtered if j["loc_class"] in _NYC_CLASSES)
    sf_count     = sum(1 for j in filtered if j["loc_class"] in _SF_CLASSES)
    remote_count = sum(1 for j in filtered if "remote" in j["loc_class"])
    print(f"\n{len(raw)} title-matched → {len(filtered)} after location "
          f"(NYC={nyc_count} SF={sf_count} remote={remote_count} excluded-unknown={excluded_unknown}) "
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
        description="Role scraper — PM · TPM · FDE · SE · SA · BizOps · StratOps",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    p.add_argument("--remote-only", action="store_true",
                   help="Remote/US-wide roles only; drop NYC-specific listings")
    p.add_argument("--include-unknown-loc", action="store_true",
                   help="Include roles whose location could not be classified")
    args = p.parse_args()

    cmd_local(args.remote_only, args.include_unknown_loc)


if __name__ == "__main__":
    main()
