#!/usr/bin/env python3
"""
pmfarm.py — PM role scraper: Greenhouse · Ashby · Lever

  python3 pmfarm.py [--remote-only] [--all-levels] [--include-unknown-loc]

All data comes from live ATS JSON APIs. See SCRAPER_RULES.md.
Dedupe: gmail_applied.txt (primary) + applied.csv (fallback).
"""

import argparse, csv, datetime, html as H, json, re, sys, urllib.request
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
    r'(?<!\w)(?:senior|sr\.|staff|principal|lead|director|vp|vice\s+president)(?!\w)'
    r'|(?<!\w)head\s+of\b'
    r'|(?<!\w)group\s+product\b'
    r'|\b(?:ii|iii)(?:\s|$)',
    re.IGNORECASE,
)

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
# Loaded from verified_companies.json if present; falls back to these defaults.
# Run discover.py locally to grow the cache with YC-seeded companies.
_CACHE_FILE = "verified_companies.json"

def _load_companies() -> tuple[list, list, list]:
    try:
        import os
        if os.path.exists(_CACHE_FILE):
            data = json.loads(open(_CACHE_FILE).read())
            cand = data.get("candidates", {})
            # Merge verified slugs with the wider candidate pool. Candidates are
            # unconfirmed company guesses; a bad slug simply returns no jobs from
            # the API (harmless), while good ones widen coverage for free.
            def _merge(key):
                seen, out = set(), []
                for src in (data.get(key, []), cand.get(key, [])):
                    for e in src:
                        s = e["slug"] if isinstance(e, dict) else e
                        if s and s not in seen:
                            seen.add(s); out.append(s)
                return out
            gh, ash, lev = _merge("greenhouse"), _merge("ashby"), _merge("lever")
            if gh or ash or lev:
                return gh, ash, lev
    except Exception:
        pass
    # Hardcoded fallback — used when cache file is absent
    return (
        [   # Greenhouse
            "betterment", "robinhood", "justworks", "mongodb", "datadog", "figma",
            "stripe", "brex", "plaid", "affirm", "sofi", "gusto", "rippling",
            "doubleverify", "pinterest", "carta", "hubspot", "webflow", "etsy",
            "duolingo", "airtable", "benchling", "cockroachlabs", "coda",
            "gemini", "navan", "whatnot", "modal", "replit",
            "hingehealth", "springhealth", "headway", "cerebral", "lyra",
            "ro", "twentyeight-health", "tempus", "color", "flatiron",
            "peloton", "whoop", "oura", "brilliant", "verkada",
            "mantrahealth", "alteradigitalhealth", "mavenclinic", "airbnb",
        ],
        [   # Ashby
            "notion", "harvey", "ramp", "cohere", "linear", "supabase", "mercury",
            "vanta", "clay", "deel", "retool", "scale-ai", "perplexity", "vercel",
            "anyscale", "cursor", "glean", "watershed", "alchemy", "dbt-labs",
            "openai", "anthropic", "arc", "prefect", "runway",
            "nuvation", "sword-health", "nirahealth", "turquoise-health", "ribbon-health",
            "oneapp", "airwallex", "pivotal-health", "plaid", "brigit",
        ],
        [   # Lever
            "airbnb", "shopify", "canva", "asana", "zendesk", "squarespace",
            "intercom", "netlify", "sendbird", "postman", "contentful",
            "amplitude", "mixpanel", "pagerduty", "cloudflare", "hashicorp",
            "nuro", "veracyte",
            "tenna", "cents", "salvohealth", "mistral", "luni", "Flex",
        ],
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

APPLIED_FILE = "applied.csv"
GMAIL_FILE   = "gmail_applied.txt"  # synced by pmfarm_gmail_sync.py; one company per line
OUTPUT_FILE  = "pm_roles.csv"


# ── helpers ───────────────────────────────────────────────────────────────────

def _strip_html(text: str) -> str:
    return H.unescape(re.sub(r"<[^>]+>", " ", text or "")).strip()


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
    t = re.sub(r"\s+", " ", text or "").strip()
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
    out = []
    for j in jobs:
        if (_norm_url(j["url"]) in applied or
                _ct_key(j["company"], j["title"]) in applied):
            continue
        out.append(j)
    return out


def _make_job(source, company, title, location, url, snippet, date_str,
              full_content=None) -> dict:
    lc            = _loc_class(location, snippet)
    years_raw, yc = _parse_years(snippet)
    # Verbatim sentence comes from FULL content so it isn't lost to truncation.
    years_sentence = _years_sentence(full_content if full_content is not None else snippet)
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
        "hw_signal":      "YES" if any(kw in (title + " " + snippet).lower() for kw in HARDWARE_SIGNAL) else "",
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
            j.get("updated_at"), full_content=content,
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
            full_content=content,
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
            date_str, full_content=content,
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
              "url", "days_old", "years_raw", "years_context", "years_sentence",
              "hw_signal", "applied"]
    with open(OUTPUT_FILE, "w", newline="", encoding="utf-8") as f:
        csv.DictWriter(f, fieldnames=fields).writeheader()
        csv.DictWriter(f, fieldnames=fields).writerows(jobs)
    print(f"Saved {len(jobs)} role(s) → {OUTPUT_FILE}")
    print(f"Fill the 'applied' column as you go. Save as {APPLIED_FILE} to dedupe next run.")


# ── local mode ────────────────────────────────────────────────────────────────

def cmd_local(remote_only: bool, include_unknown_loc: bool = False):
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
