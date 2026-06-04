#!/usr/bin/env python3
"""
test_pmfarm.py — all 8 adversarial test cases.
Run: python3 test_pmfarm.py
"""
import csv, io, json, os, sys, tempfile, shutil
import pmfarm

PASS, FAIL = "PASS", "FAIL"
results: list[tuple] = []


def check(name: str, got, want):
    ok = got == want
    results.append((PASS if ok else FAIL, name, repr(got), repr(want)))
    return ok


def header(n: int, title: str):
    print(f"\n── TEST {n}: {title} {'─' * (55 - len(title))}")


# ── TEST 1: dedupe fires across URL-normalization variants ────────────────────

def test_1_dedupe():
    header(1, "dedupe fires")
    applied_rows = [
        # company+title — no URL on file, so must match by name pair
        {"company": "harvey",     "title": "Innovation Product Manager",  "url": ""},
        # URL variants — titles intentionally differ, so only URL can catch them
        {"company": "notion",     "title": "applied-A", "url": "https://jobs.ashbyhq.com/notion/abc123"},
        {"company": "betterment", "title": "applied-B", "url": "https://boards.greenhouse.io/betterment/jobs/111/"},        # trailing slash
        {"company": "oscar",      "title": "applied-C", "url": "https://boards.greenhouse.io/oscar/jobs/222?gh_jid=999"},  # query param
        {"company": "robinhood",  "title": "applied-D", "url": "http://boards.greenhouse.io/robinhood/jobs/333"},          # http scheme
    ]
    jobs = [
        {"company": "harvey",     "title": "Innovation Product Manager", "url": "https://jobs.ashbyhq.com/harvey/live"},
        {"company": "notion",     "title": "live-A",                    "url": "https://jobs.ashbyhq.com/notion/abc123"},
        {"company": "betterment", "title": "live-B",                    "url": "https://boards.greenhouse.io/betterment/jobs/111"},   # no slash
        {"company": "oscar",      "title": "live-C",                    "url": "https://boards.greenhouse.io/oscar/jobs/222?gh_src=x"}, # diff param
        {"company": "robinhood",  "title": "live-D",                    "url": "https://boards.greenhouse.io/robinhood/jobs/333"},     # https
    ]
    survivor = {"company": "stripe", "title": "Product Manager", "url": "https://boards.greenhouse.io/stripe/jobs/777"}

    fd, path = tempfile.mkstemp(suffix=".csv")
    with os.fdopen(fd, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=["company","title","url"], quoting=csv.QUOTE_ALL)
        w.writeheader(); w.writerows(applied_rows)

    orig = pmfarm.APPLIED_FILE
    pmfarm.APPLIED_FILE = path
    try:
        applied = pmfarm._load_applied()
        deduped = pmfarm._deduped(jobs, applied)
        kept    = pmfarm._deduped(jobs + [survivor], applied)
    finally:
        pmfarm.APPLIED_FILE = orig
        os.remove(path)

    labels = ["company+title", "exact URL", "trailing slash", "?query param", "http→https"]
    for job, label in zip(jobs, labels):
        removed = job not in deduped
        flag = "ok  " if removed else "XX  "
        print(f"  {flag}{label:16s} removed={removed}")
        check(f"dedupe/{label}", removed, True)

    check("non-applied survivor kept", kept, [survivor])
    print(f"  → {len(jobs)} applied in, {len(deduped)} out (want 0)")
    print(f"  → survivor kept: {kept == [survivor]}")


# ── TEST 2: missing applied.csv doesn't crash ────────────────────────────────

def test_2_missing_file():
    header(2, "missing applied.csv")
    orig = pmfarm.APPLIED_FILE
    pmfarm.APPLIED_FILE = "/tmp/does_not_exist_pmfarm_test.csv"
    try:
        applied = pmfarm._load_applied()
        jobs    = [{"company": "x", "title": "Product Manager", "url": "https://e/x"}]
        kept    = pmfarm._deduped(jobs, applied)
        print(f"  ok  no crash — returned {len(kept)} role(s), applied set size={len(applied)}")
        check("missing file → empty set",    applied, set())
        check("missing file → all kept",     kept,    jobs)
    finally:
        pmfarm.APPLIED_FILE = orig


# ── TEST 3: years parsing on adversarial phrasing ────────────────────────────

def test_3_years():
    header(3, "years parsing")
    cases = [
        ("3-5 years preferred, but equivalent experience valued", "3"),
        ("5+ years in marketing, 2+ in product",                  "2"),
        ("No specific experience required",                       "unknown"),
        ("10 years of combined team experience",                  "unknown"),
        # sanity regressions
        ("3+ years of product management experience",             "3"),
        ("Minimum of 4 years in product",                         "4"),
        ("at least 2 years building software",                    "2"),
        ("1-3 years in a product role",                           "1"),
    ]
    for desc, want in cases:
        raw, ctx = pmfarm._parse_years(desc)
        ok = raw == want
        flag = "ok  " if ok else "XX  "
        print(f"  {flag}years_raw={raw:<8s} ← {desc}")
        if ctx:
            print(f"        context: {ctx[:80]}")
        check(f"years/{desc[:40]}", raw, want)


# ── TEST 4: remote toggle is a real strict subset ────────────────────────────

def test_4_remote_toggle():
    header(4, "remote toggle")
    jobs = [
        {"company": "a", "title": "Product Manager", "url": "u/a", "loc_class": "remote"},
        {"company": "b", "title": "Product Manager", "url": "u/b", "loc_class": "remote+nyc"},
        {"company": "c", "title": "Product Manager", "url": "u/c", "loc_class": "nyc"},
        {"company": "d", "title": "Product Manager", "url": "u/d", "loc_class": "unknown"},
    ]
    without = [j for j in jobs if pmfarm._passes_location(j["loc_class"], False)]
    with_ro = [j for j in jobs if pmfarm._passes_location(j["loc_class"], True)]

    companies_without = {j["company"] for j in without}
    companies_with    = {j["company"] for j in with_ro}
    nyc_present_without = any(j["loc_class"] == "nyc" for j in without)
    nyc_present_with    = any(j["loc_class"] == "nyc" for j in with_ro)

    print(f"  without --remote-only : {sorted(companies_without)}  (want a,b,c,d)")
    print(f"  with    --remote-only : {sorted(companies_with)}     (want a,b,d — no nyc)")
    print(f"  nyc in without={nyc_present_without}, nyc in with={nyc_present_with}")

    check("without: all 4 loc classes pass", len(without), 4)
    check("with: nyc dropped",               len(with_ro), 3)
    check("with: nyc company absent",        "c" not in companies_with, True)
    check("with: strict subset of without",  companies_with.issubset(companies_without), True)


# ── TEST 5: seniority filter — no false positives or false negatives ─────────

def test_5_seniority():
    header(5, "seniority filter")
    should_drop = [
        "Senior Product Manager",
        "Staff Product Manager",
        "Principal Product Manager",
        "Lead Product Manager",
        "Director, Product",
        "Product Manager II",
        "Product Manager III",
        "Sr. Product Manager",
        "Group Product Manager",
        "VP of Product",
        "Vice President of Product",
        "Head of Product",
    ]
    should_pass = [
        "Product Manager",
        "Associate Product Manager",
        "Technical Product Manager",
        "Product Manager, Leadership Tools",   # 'lead' substring — must NOT drop
        "Product Manager, Leads Management",   # 'lead' substring — must NOT drop
        "Staffing Product Manager",            # 'staff' substring — must NOT drop
        "Product Manager, Senior Care",        # 'senior' substring — must NOT drop
        "Product Manager, Directorial Support", # 'director' substring — must NOT drop
    ]
    for title in should_drop:
        result = pmfarm._passes_title(title)
        flag = "ok  " if not result else "XX  "
        print(f"  {flag}DROPPED {title!r}")
        check(f"seniority/drop/{title}", result, False)
    for title in should_pass:
        result = pmfarm._passes_title(title)
        flag = "ok  " if result else "XX  "
        print(f"  {flag}PASSES  {title!r}")
        check(f"seniority/pass/{title}", result, True)


# ── TEST 6: dead/missing slug returns [] and doesn't halt ────────────────────

def test_6_dead_slug():
    header(6, "dead slug handling")
    orig_fetch = pmfarm._fetch

    call_log: list[str] = []

    def mock_fetch(url: str):
        call_log.append(url)
        if "asdfqwer" in url:
            return None           # dead slug → network / 404
        if "greenhouse" in url:
            return {"jobs": []}   # live slug, no PM roles today
        return []                 # lever / ashby live slug, empty

    pmfarm._fetch = mock_fetch
    try:
        dead   = pmfarm.fetch_greenhouse("asdfqwer")
        live   = pmfarm.fetch_greenhouse("stripe")
        # mix in thread pool — dead slug should not block others
        import concurrent.futures
        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
            futs = {
                pool.submit(pmfarm.fetch_greenhouse, "asdfqwer"): "dead-greenhouse",
                pool.submit(pmfarm.fetch_ashby,      "asdfqwer"): "dead-ashby",
                pool.submit(pmfarm.fetch_lever,      "asdfqwer"): "dead-lever",
                pool.submit(pmfarm.fetch_greenhouse, "stripe"):   "live-stripe",
            }
            pool_results = {label: fut.result() for fut, label in futs.items()}
    finally:
        pmfarm._fetch = orig_fetch

    print(f"  dead slug (greenhouse) → {dead!r}")
    print(f"  live slug (stripe)     → {live!r}")
    for label, res in pool_results.items():
        print(f"  threaded {label:18s} → {res!r}")

    check("dead slug → []",          dead,  [])
    check("live slug no PMs → []",   live,  [])
    check("threaded dead GH → []",   pool_results["dead-greenhouse"], [])
    check("threaded dead Ashby → []",pool_results["dead-ashby"],      [])
    check("threaded dead Lever → []",pool_results["dead-lever"],      [])
    check("threaded live continues", pool_results["live-stripe"],      [])


# ── TEST 7: idempotency — two back-to-back runs produce identical CSV ────────

def test_7_idempotency():
    header(7, "idempotency")
    src = "search_results.json"
    if not os.path.exists(src):
        print("  skip  search_results.json not present — run the scraper first")
        return

    orig_out = pmfarm.OUTPUT_FILE
    fd1, tmp1 = tempfile.mkstemp(suffix=".csv")
    fd2, tmp2 = tempfile.mkstemp(suffix=".csv")
    os.close(fd1); os.close(fd2)

    try:
        pmfarm.OUTPUT_FILE = tmp1
        pmfarm.cmd_process(src, remote_only=False)
        pmfarm.OUTPUT_FILE = tmp2
        pmfarm.cmd_process(src, remote_only=False)

        with open(tmp1, newline="", encoding="utf-8") as f1, \
             open(tmp2, newline="", encoding="utf-8") as f2:
            rows1 = list(csv.DictReader(f1))
            rows2 = list(csv.DictReader(f2))
    finally:
        pmfarm.OUTPUT_FILE = orig_out
        os.remove(tmp1); os.remove(tmp2)

    same_count = len(rows1) == len(rows2)
    same_urls  = [r["url"] for r in rows1] == [r["url"] for r in rows2]
    same_years = [r["years_raw"] for r in rows1] == [r["years_raw"] for r in rows2]

    print(f"  run1={len(rows1)} roles, run2={len(rows2)} roles")
    print(f"  URL order identical:   {same_urls}")
    print(f"  years_raw identical:   {same_years}")

    check("idempotent count",     same_count, True)
    check("idempotent url order", same_urls,  True)
    check("idempotent years",     same_years, True)


# ── TEST 8: honest limits ────────────────────────────────────────────────────

def test_8_honest_limits():
    header(8, "honest limits")

    # 8a: Ashby days_old is always unknown (no date from API)
    job = pmfarm._make_job("Ashby", "notion", "Product Manager", "Remote",
                           "https://jobs.ashbyhq.com/notion/x", "snippet", None)
    print(f"  Ashby days_old={job['days_old']!r}  (want 'unknown')")
    check("Ashby days_old=unknown", job["days_old"], "unknown")

    # 8b: Greenhouse date parses correctly into days_old
    gh_job = pmfarm._make_job("Greenhouse", "stripe", "Product Manager", "Remote",
                              "https://boards.greenhouse.io/stripe/jobs/1",
                              "3+ years of product management experience",
                              "2026-05-29T00:00:00Z")
    days = gh_job["days_old"]
    try:
        days_int = int(days)
        days_ok  = 0 <= days_int <= 365
    except ValueError:
        days_ok = False
    print(f"  Greenhouse days_old={days!r}  (want int 0–365)")
    check("Greenhouse days_old is numeric", days_ok, True)

    # 8c: single thin-result company returns exactly that many roles, no padding
    orig_fetch = pmfarm._fetch

    def mock_one_role(url: str):
        if "greenhouse" in url and "tinyco" in url:
            return {"jobs": [{"title": "Product Manager", "location": {"name": "Remote"},
                              "absolute_url": "https://boards.greenhouse.io/tinyco/jobs/1",
                              "content": "3+ years experience", "updated_at": "2026-06-01T00:00:00Z"}]}
        return None

    pmfarm._fetch = mock_one_role
    try:
        roles = pmfarm.fetch_greenhouse("tinyco")
    finally:
        pmfarm._fetch = orig_fetch

    print(f"  thin fetch → {len(roles)} role(s)  (want exactly 1, no padding)")
    check("thin result = exact count, no padding", len(roles), 1)

    # 8d: years_raw=unknown does NOT appear as "0" or empty in output
    _, ctx = pmfarm._parse_years("No experience required")
    raw, _ = pmfarm._parse_years("No experience required")
    print(f"  'No experience required' → years_raw={raw!r}  (want 'unknown', not '0')")
    check("no-experience → unknown not zero", raw, "unknown")


# ── TEST 9: Gmail company-level dedupe ───────────────────────────────────────

def test_9_gmail_dedupe():
    header(9, "Gmail company-level dedupe")

    gmail_content = "\n".join([
        "# test fixture",
        "Harvey\tThank You for Applying to Harvey\t2026-06-01",
        "Betterment\tThank you for applying to Betterment!\t2026-06-01",
        "Oscar\tThank you for applying to Oscar!\t2026-06-01",
        "Robinhood\tThank you for applying to Robinhood\t2026-06-01",
        "21Shares\tThank you for applying to 21Shares\t2026-06-01",
        "DoorDash\tThank you for applying to DoorDash\t2026-06-01",
        "Stripe\tThanks for applying to Stripe!\t2026-06-01",
        "Scale AI\tThank you for applying to Scale AI\t2026-05-02",
        "FanDuel\tThank you for applying to FanDuel\t2026-06-03",
    ])

    fd, path = tempfile.mkstemp(suffix=".txt")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(gmail_content)

    orig_gf = pmfarm.GMAIL_FILE
    pmfarm.GMAIL_FILE = path
    try:
        gmail_set, evidence = pmfarm._gmail_applied_set()

        # --- normalization correctness ---
        print(f"  gmail_set ({len(gmail_set)}): {sorted(gmail_set)}")
        check("normalize FanDuel",  pmfarm._normalize_company("FanDuel"),  "fanduel")
        check("normalize Scale AI", pmfarm._normalize_company("Scale AI"), "scaleai")
        check("normalize scale-ai", pmfarm._normalize_company("scale-ai"), "scaleai")  # slug → same
        check("normalize 21Shares", pmfarm._normalize_company("21Shares"), "21shares")
        check("set size", len(gmail_set), 9)

        # --- roles that SHOULD be dropped ---
        should_skip = [
            {"company": "harvey",     "title": "Innovation PM",     "url": "https://jobs.ashbyhq.com/harvey/1"},
            {"company": "betterment", "title": "PM User Trust",     "url": "https://boards.greenhouse.io/betterment/1"},
            {"company": "stripe",     "title": "PM Startup",        "url": "https://boards.greenhouse.io/stripe/2"},
            {"company": "oscar",      "title": "PM Network",        "url": "https://boards.greenhouse.io/oscar/3"},
            {"company": "scale-ai",   "title": "PM Core",           "url": "https://jobs.ashbyhq.com/scale-ai/4"},
            {"company": "fanduel",    "title": "PM Sportsbook",     "url": "https://boards.greenhouse.io/fanduel/5"},
        ]
        # --- roles that SHOULD survive ---
        should_keep = [
            {"company": "anthropic",  "title": "PM Consumer",       "url": "https://jobs.ashbyhq.com/anthropic/6"},
            {"company": "cursor",     "title": "PM Core",           "url": "https://jobs.ashbyhq.com/cursor/7"},
        ]

        skipped, kept = [], []
        for j in should_skip + should_keep:
            if pmfarm._normalize_company(j["company"]) in gmail_set:
                skipped.append(j)
            else:
                kept.append(j)

        print(f"\n  Roles skipped by Gmail dedupe: {len(skipped)}")
        for j in skipped:
            print(f"    {j['company']:<20s} {j['title']}")
        print(f"  Roles kept: {len(kept)}")
        for j in kept:
            print(f"    {j['company']:<20s} {j['title']}")

        check("should_skip: all 6 dropped",  len(skipped), 6)
        check("should_keep: all 2 kept",     len(kept),    2)
        check("anthropic not dropped",       should_keep[0] in kept, True)
        check("cursor not dropped",          should_keep[1] in kept, True)

        # --- required-companies assertion (from CODE_DIRECTIVES) ---
        required_raw = ["Harvey", "Betterment", "Oscar", "Robinhood",
                        "21Shares", "DoorDash", "Stripe"]
        required_norm = {pmfarm._normalize_company(c) for c in required_raw}
        missing = required_norm - gmail_set
        print(f"\n  Required companies assertion: missing={missing or 'none'}")
        check("all CODE_DIRECTIVES required companies in set", len(missing), 0)

    finally:
        pmfarm.GMAIL_FILE = orig_gf
        os.remove(path)

    # --- missing gmail_applied.txt → empty set, no crash ---
    orig_gf = pmfarm.GMAIL_FILE
    pmfarm.GMAIL_FILE = "/tmp/no_such_gmail_applied.txt"
    try:
        gset, _ = pmfarm._gmail_applied_set()
        check("missing gmail file → empty set", gset, set())
    finally:
        pmfarm.GMAIL_FILE = orig_gf


# ── run all ───────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    test_1_dedupe()
    test_2_missing_file()
    test_3_years()
    test_4_remote_toggle()
    test_5_seniority()
    test_6_dead_slug()
    test_7_idempotency()
    test_8_honest_limits()
    test_9_gmail_dedupe()

    n_fail = sum(1 for r in results if r[0] == FAIL)
    print(f"\n{'═'*68}")
    for status, name, got, want in results:
        if status == FAIL:
            print(f"  FAIL  {name}\n        got={got}\n        want={want}")
    print(f"  {len(results) - n_fail}/{len(results)} checks passed"
          + (f"  ← {n_fail} FAILED" if n_fail else "  ✓ all green"))
    print(f"{'═'*68}")
    raise SystemExit(1 if n_fail else 0)
