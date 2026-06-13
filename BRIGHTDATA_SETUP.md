# Bright Data → hiring.cafe — Post-Shower Setup

This turns on the **hiring.cafe** source. The code is already built and **inert** (it
returns nothing and cannot spend money) until you finish the steps below. The ATS
pipeline (Greenhouse/Ashby/Lever) keeps working the whole time, untouched.

---

## 💸 COST — read first

- **Pricing: ~$1.50 per 1,000 page loads (Scraper Studio), pay-as-you-go.**
- **USE PAY-AS-YOU-GO. NEVER buy a monthly plan** ($499+/mo). You are scraping one
  search URL once a day — that is a few cents to a few dollars a month, not hundreds.
  A monthly subscription is wasted money for this use.
- A one-off `bdata scraper create` (building the collector) also costs a little.
- There is no hard server-side page cap on a run, so spend is bounded by:
  1. **The master toggle** — `SOURCES["brightdata"]` in `pmfarm.py`. Inert = **$0**.
  2. **The once-per-day lock** — a successful run writes `.brightdata_last_run`; any
     re-run within 20h is skipped for free. A manual re-run cannot re-bill you.
  3. **A tight search URL** — the more you pre-filter on hiring.cafe (NYC/SF, your
     role types, entry/mid), the fewer results, the fewer page loads, the less it costs.
  4. **`MAX_PAGE_LOADS = 50`** in `pmfarm.py` — client-side backstop on rows accepted.
- **The cost gate (do this in order):** run it manually **once**, then check
  `bdata budget balance`, and **only after** you have seen a real (small) number do you
  flip the toggle on and commit for the daily automation.

---

## Steps

### 1. Create the account + get the API key  *(your action — payment boundary)*
1. Sign up at <https://brightdata.com>.
2. **Choose pay-as-you-go. Do NOT pick a monthly plan.**
3. Get your API key: **Account settings → API keys** (or ask the in-app assistant
   "where can I find my API key"). Copy it.

### 2. Log the CLI in
The `bdata` CLI is already installed (`bdata --version` → `0.3.1`). Authenticate once:
```bash
bdata login                       # opens a browser for OAuth
# or, non-interactive:
bdata login --api-key "<YOUR_API_KEY>"
```
Verify:
```bash
bdata zones        # non-zero exit means not logged in
bdata budget balance
```

### 3. Build the filtered hiring.cafe search URL
On <https://hiring.cafe>, apply your filters in the UI, then **copy the URL from the
address bar** once the results match what you want:
- Locations: **New York** (primary) + **San Francisco**, plus **Remote (US)**.
- Roles: Product Manager, APM, Technical Program Manager, Forward Deployed Engineer,
  Solutions Engineer, Solutions Architect, Business Operations, Strategy & Operations,
  Product Owner, Deployment Strategist.
- Experience: **entry / mid** (the code still hard-drops anything explicitly 4+ yrs).

Tighter filters = fewer page loads = lower cost. This URL is line 3 of the key file.

### 4. Build the collector — `bdata scraper create`
Run this with **your** search URL in place of `<SEARCH_URL>`. Generation takes ~5–10 min.
The description below is ready to use — it names every field the scraper in `pmfarm.py`
expects (`title`, `company`, `location`, `years_required`, `apply_url`, `post_date`):

```bash
bdata scraper create "<SEARCH_URL>" \
  "Extract every job listing on this hiring.cafe results page. For each listing return: \
title (the exact job title); company (the hiring company name); location (city/state or \
Remote); years_required (the verbatim text stating required years of experience, e.g. \
'2+ years', or empty if none is stated); apply_url (the DIRECT external apply link to the \
company's own application page / ATS — NOT the hiring.cafe listing URL); and post_date \
(when the job was posted). Follow each listing through to capture the real external apply \
URL, not the hiring.cafe redirect. Return one object per job." \
  --name hiringcafe-pm --pretty -o create.json
```

> **Why the apply_url wording matters:** the page rule is ATS-direct links only. If the
> scraper returns `hiring.cafe/...` URLs, they break the no-redirect rule and won't dedupe
> against roles already found on Greenhouse/Ashby/Lever. The description forces the real
> external link.

Grab the collector id:
```bash
COLLECTOR_ID=$(jq -r '.collector_id // .id' create.json)
echo "$COLLECTOR_ID"
```
If `create` times out, the collector id is still printed — finish/inspect it at
`https://brightdata.com/cp/scrapers/<collector_id>`. Do **not** re-run `create` (that
builds a second collector and bills again).

### 5. Save the three values into `brightdata_key.txt`  (gitignored)
Exactly three lines, in this order — same loader pattern as `adzuna_key.txt`:
```
<BRIGHTDATA_API_KEY>
<COLLECTOR_ID>
<HIRINGCAFE_SEARCH_URL>
```
One-liner:
```bash
printf '%s\n%s\n%s\n' "<API_KEY>" "$COLLECTOR_ID" "<SEARCH_URL>" > brightdata_key.txt
```

### 6. Test ONCE, then check the bill
Flip the toggle on **temporarily** (or test before committing — your call):
```bash
# In pmfarm.py:  SOURCES = { ... "brightdata": True }
~/.venv/pmfarm/bin/python3 pmfarm.py
bdata budget balance        # confirm the spend is small/expected
```
You should see `fetch_brightdata: N row(s) → M matched title filter`, and hiring.cafe
roles in the output with **direct** apply links, correctly experience-gated and deduped.

> **If the first run comes back empty:** a hiring.cafe *search* URL is paginated, so
> the run usually hits Scraper Studio's batch auto-fallback, which can take several
> minutes. An empty result is most likely the run still finishing / timing out — it
> is **not** a broken collector. Just re-run (the spend lock makes the immediate
> re-run free if the prior one billed), or widen the timeout. Only `heal` if real
> rows come back with the *wrong fields*.
If fields come back wrong/empty:
```bash
bdata scraper heal "$COLLECTOR_ID" "<what is wrong, e.g. apply_url is the hiring.cafe page, not the company link>"
```

### 7. Go daily
Once the manual run looks right and the bill is acceptable:
1. Keep `SOURCES["brightdata"] = True` in `pmfarm.py`, **commit and push** it.
2. Add the three GitHub **secrets** (Repo → Settings → Secrets and variables → Actions):
   - `BRIGHTDATA_API_KEY`
   - `BRIGHTDATA_COLLECTOR_ID`
   - `BRIGHTDATA_SEARCH_URL`
   The workflow (`.github/workflows/daily-scrape.yml`) already installs the CLI and writes
   `brightdata_key.txt` from these — mirrors the `ADZUNA_*` pattern. The daily schedule is
   one run/day, so cost stays predictable.

That's it. The toggle stays the master switch: set it back to `False` and commit any time
to make the whole source inert again at $0.

---

## Quick reference
| Thing | Where |
|---|---|
| Master on/off | `SOURCES["brightdata"]` in `pmfarm.py` |
| Credentials | `brightdata_key.txt` (3 lines, gitignored) |
| Spend lock | `.brightdata_last_run` (delete to force a re-run) |
| Row cap | `MAX_PAGE_LOADS` in `pmfarm.py` |
| Check spend | `bdata budget balance` |
| Fix a bad scraper | `bdata scraper heal <collector_id> "<what's wrong>"` |
| Inspect in UI | `https://brightdata.com/cp/scrapers/<collector_id>` |
