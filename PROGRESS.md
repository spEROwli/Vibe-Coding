# PROGRESS.md — Autonomous P1–P6 run (2026-06-04)

## Run constraints
- Container blocks ATS APIs (Greenhouse/Ashby/Lever 403 datacenter IPs).
  cmd_local produces 0 roles in container; all ATS-dependent acceptance tests
  must be run by user on Mac to complete full verification.
- Gmail MCP IS available in this session; used to prove P1 Gmail data is real.
- All code changes are committed and pushed. User pulls latest and runs locally.

---

## P1 — Gmail dedupe [COMPLETED — commit d47af57]

Added `_normalize_company()`, `_gmail_applied_set()`, `GMAIL_FILE` constant,
and `pmfarm_gmail_sync.py` (standalone OAuth sync).
`gmail_applied.txt` written from real Gmail MCP query (38 companies).
`test_9_gmail_dedupe` passes: 6/6 skipped, 2/2 kept, all 7 required companies present.

## P2 — Unknown-loc exclusion [COMPLETED — commit e9cb474]

`_passes_location` now excludes `unknown` by default.
`--include-unknown-loc` flag re-includes them.
`test_4` updated: default CSV has zero unknown rows.

## P3 — Delete cmd_process WebSearch path [COMPLETED — commit 7e09e73]

Deleted: `cmd_queries()`, `cmd_process()`, `ATS_DOMAINS`, `CHUNK`, `NEG`,
`_chunks()`, `urlparse` import. Updated arg parser (removed 'queries'/'process'
choices and `file` positional arg). Rewrote `test_7` as idempotency test for
`cmd_local` with mocked ATS fetchers.
`grep -ri 'cmd_process|websearch|web_search' *.py` → (no output).
66/66 green.

## P4 — Slug resolution observability [COMPLETED — commit 70a3bd6]

Added `_slug_resolution` dict + `_record()` helper. Each fetcher records
`ok`/`empty`/`fail` per slug. `cmd_local` resets dict each run, prints
hit-rate table at end. Added `test_10_slug_resolution` (mocks 3 slugs,
verifies all statuses tracked correctly).
73/73 green.

## P5 — Daily self-run [COMPLETED — commit 0bc7c02]

`run_daily.sh`: Gmail sync → `pmfarm.py --all-levels` → `build_page.py`.
Logs to `logs/pmfarm_YYYYMMDD_HHMMSS.log`, keeps last 14 runs.
`com.pmfarm.daily.plist`: launchd agent for 07:30 daily trigger.

**User must:**
1. Edit `YOUR_USERNAME` in `com.pmfarm.daily.plist`.
2. `cp com.pmfarm.daily.plist ~/Library/LaunchAgents/`
3. `launchctl load ~/Library/LaunchAgents/com.pmfarm.daily.plist`
4. First-time Gmail token: `python3 pmfarm_gmail_sync.py --setup`

## P6 — Lock build_page triage contract [COMPLETED — commit 66c289f]

`test_11_build_page_contract` locks:
- Bucket A = IC-level title (no Senior/Staff) + years_raw ≤ 3 or unknown or hedged sentence.
- Sort: priority → loc_rank (NYC > remote > other) → days_old ascending.
- `years_sentence` passes verbatim to HTML; `_fit_key` never calls `_years_num`.
93/93 green.

---

## Trust Gate [BLOCKED — must run on Mac]

The trust gate verifies zero mismatches between live API output and HTML cards:

```bash
python3 pmfarm.py --all-levels
python3 build_page.py
# Manual: open pm_roles.html and spot-check 5 Bucket A cards:
# 1. Confirm company+title appear in verified_companies.json slugs.
# 2. Confirm location matches ATS API field (no relabeling).
# 3. Confirm years sentence is a verbatim substring of the JD content.
# 4. Confirm no role appears for a company in gmail_applied.txt.
# 5. Confirm slug hit-rate line printed to terminal (e.g. "15/128 with PM roles").
```

Container blocks ATS APIs → 0 roles returned → trust gate cannot be exercised here.
All code is committed and pushed to `claude/pm-roles-ats-scraper-SAMqK`.

---

## Summary

P1–P6 complete. 93/93 unit checks green. All acceptance criteria met in container
(ATS-API-dependent acceptance must be verified on Mac). Pipeline is now:

```
pmfarm_gmail_sync.py  → gmail_applied.txt     (38 companies from real inbox)
pmfarm.py             → pm_roles.csv          (live ATS JSON only, no fabrication)
build_page.py         → pm_roles.html         (verbatim contract, locked by test_11)
run_daily.sh          → launchd at 07:30      (P5, install manually)
```

Next: pull latest on Mac, run `bash run_daily.sh` for the first live end-to-end pass.
