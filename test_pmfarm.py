#!/usr/bin/env python3
"""
test_pmfarm.py — adversarial tests for the scraper's trust-critical logic.
Run: python3 test_pmfarm.py
"""
import csv, os, tempfile
import pmfarm

PASS, FAIL = "PASS", "FAIL"
results = []


def check(name, got, want):
    ok = got == want
    results.append((PASS if ok else FAIL, name, repr(got), repr(want)))


# ── TEST 3: years parsing on adversarial phrasing ────────────────────────────

def test_years():
    cases = [
        # (description, expected years_raw)
        ("3-5 years preferred, but equivalent experience valued", "3"),
        ("5+ years in marketing, 2+ in product",                  "2"),
        ("No specific experience required",                       "unknown"),
        ("10 years of combined team experience",                  "unknown"),
        # real-world sanity (not in the brief, but must not regress)
        ("3+ years of product management experience",             "3"),
        ("Minimum of 4 years in product",                         "4"),
        ("at least 2 years building software",                    "2"),
    ]
    print("\n── TEST 3: years parsing ─────────────────────────────────────────")
    for desc, want in cases:
        raw, ctx = pmfarm._parse_years(desc)
        check(f"years({desc[:38]!r})", raw, want)
        flag = "ok  " if raw == want else "XX  "
        print(f"  {flag}years_raw={raw:<8s} ← {desc}")
        if ctx:
            print(f"        context: {ctx}")


# ── TEST 1: dedupe fires across URL-normalization variants ───────────────────

def test_dedupe():
    applied_rows = [
        # company+title path (no URL on file)
        {"company": "harvey",     "title": "Innovation Product Manager",  "url": ""},
        # URL paths — applied titles deliberately differ from live titles,
        # so ONLY url normalization can catch these
        {"company": "notion",     "title": "applied-A", "url": "https://jobs.ashbyhq.com/notion/abc123"},
        {"company": "betterment", "title": "applied-B", "url": "https://boards.greenhouse.io/betterment/jobs/111/"},      # trailing slash
        {"company": "oscar",      "title": "applied-C", "url": "https://boards.greenhouse.io/oscar/jobs/222?gh_jid=999"}, # query param
        {"company": "robinhood",  "title": "applied-D", "url": "http://boards.greenhouse.io/robinhood/jobs/333"},        # http scheme
    ]
    jobs = [
        {"company": "harvey",     "title": "Innovation Product Manager", "url": "https://jobs.ashbyhq.com/harvey/live"},          # via company+title
        {"company": "notion",     "title": "live-A", "url": "https://jobs.ashbyhq.com/notion/abc123"},                            # exact URL
        {"company": "betterment", "title": "live-B", "url": "https://boards.greenhouse.io/betterment/jobs/111"},                  # no trailing slash
        {"company": "oscar",      "title": "live-C", "url": "https://boards.greenhouse.io/oscar/jobs/222?gh_src=other"},          # different query
        {"company": "robinhood",  "title": "live-D", "url": "https://boards.greenhouse.io/robinhood/jobs/333"},                   # https vs http
    ]
    survivor = {"company": "stripe", "title": "Product Manager", "url": "https://boards.greenhouse.io/stripe/jobs/777"}

    # write applied fixture and point pmfarm at it
    fd, path = tempfile.mkstemp(suffix=".csv")
    with os.fdopen(fd, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=["company", "title", "url"], quoting=csv.QUOTE_ALL)
        w.writeheader()
        w.writerows(applied_rows)

    orig = pmfarm.APPLIED_FILE
    pmfarm.APPLIED_FILE = path
    try:
        applied = pmfarm._load_applied()
        deduped = pmfarm._deduped(jobs, applied)
        kept    = pmfarm._deduped(jobs + [survivor], applied)
    finally:
        pmfarm.APPLIED_FILE = orig
        os.remove(path)

    print("\n── TEST 1: dedupe across URL variants ────────────────────────────")
    labels = ["company+title", "exact URL", "trailing slash", "?query param", "http→https"]
    for job, label in zip(jobs, labels):
        removed = job not in deduped
        flag = "ok  " if removed else "XX  "
        print(f"  {flag}{label:16s} {job['company']}/{job['title']}  removed={removed}")

    check("all 5 applied roles removed", len(deduped), 0)
    check("non-applied survivor kept",    kept, [survivor])
    print(f"  → {len(jobs)} applied roles in, {len(deduped)} out (want 0)")
    print(f"  → control survivor kept: {kept == [survivor]}")


# ── TEST 2: missing applied.csv doesn't crash ────────────────────────────────

def test_missing_file():
    orig = pmfarm.APPLIED_FILE
    pmfarm.APPLIED_FILE = "/tmp/does_not_exist_pmfarm.csv"
    try:
        applied = pmfarm._load_applied()
        jobs = [{"company": "x", "title": "Product Manager", "url": "https://e/x"}]
        kept = pmfarm._deduped(jobs, applied)
    finally:
        pmfarm.APPLIED_FILE = orig
    print("\n── TEST 2: missing applied.csv ───────────────────────────────────")
    print(f"  ok  no crash; returned all {len(kept)} role(s)")
    check("missing file → empty applied set", applied, set())
    check("missing file → all roles kept",    kept, jobs)


if __name__ == "__main__":
    test_years()
    test_dedupe()
    test_missing_file()

    print("\n" + "═" * 68)
    n_fail = sum(1 for r in results if r[0] == FAIL)
    for status, name, got, want in results:
        if status == FAIL:
            print(f"  FAIL  {name}\n        got={got}  want={want}")
    print(f"  {len(results) - n_fail}/{len(results)} checks passed"
          + ("" if n_fail == 0 else f"  ({n_fail} FAILED)"))
    print("═" * 68)
    raise SystemExit(1 if n_fail else 0)
