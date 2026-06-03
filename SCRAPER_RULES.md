# SCRAPER_RULES.md — non-negotiable. Read before every run.

## The one rule everything else serves
If a role was not returned by a live API call in this run, it does not exist.
Do not emit it. Never invent, infer, complete, or "fill in" a role, an ID, a
title, a location, a salary, or a years requirement. A blank is correct. A
plausible guess is a failure.

## Why this file exists
Past runs produced polished dashboards containing roles that were not on the
companies' live boards. Examples that shipped and were false:
- "Maven Clinic, Associate PM, Fertility" — Maven had only a Senior PM live.
- "Mantra Health, PM, New Product" — Mantra had zero PM roles live.
- "Ramp, PM, International" — Ramp had only PM, Agentic CX live.
- "Stripe, Ecosystem Risk, Remote + NYC, 3+ yrs" — role is in Dublin, JD states no years.
These were generated to fill the table after a fetch failed. That is the exact
behavior this file bans.

## Allowed data sources (JSON only)
Pull ONLY from these endpoints. They return full content without 403s:
- Greenhouse: https://boards-api.greenhouse.io/v1/boards/{slug}/jobs?content=true
- Ashby:      https://api.ashbyhq.com/posting-api/job-board/{slug}?includeCompensation=true
- Lever:      https://api.lever.co/v0/postings/{slug}?mode=json

## Banned actions
- Do NOT WebFetch HTML career pages (for example stripe.com/jobs or the HTML
  Greenhouse/Ashby board). They 403 and tempt gap-filling. If JSON fails, the
  role is simply omitted.
- Do NOT construct, edit, or "reconstruct" a job URL. Use only the URL the API
  returned for that exact job object.
- Do NOT assign a years number or a "X+ yrs" bucket unless that exact phrase
  appears in the JD content field. No inference from seniority, title, or vibe.
- Do NOT relabel location. Print the API location field verbatim. A Dublin role
  is Dublin, never NYC or Remote.

## Field rules
- title: verbatim from API.
- location: verbatim from API location field.
- years: copy the exact sentence containing "year(s)" from the content field.
  If no such sentence exists, write "not stated".  Never a number you derived.
- url: the absolute_url / jobUrl / hostedUrl the API returned. Nothing else.
- source + job_id: record which API and the API's own id for every row.

## Mandatory validation gate (run before writing output)
For every role about to be emitted:
1. Confirm its job_id is present in the raw API response captured this run.
2. Confirm its url is the one the API returned for that job_id.
3. Confirm the years string is either an exact substring of the content field
   or the literal text "not stated".
Drop any role that fails any check. Log what was dropped and why.

## Output manifest (print at top of every output)
- run timestamp
- per source: companies queried, boards that resolved, total roles returned
- count emitted, count dropped by the validation gate
- this exact line: "Every role below was returned by a live API call in this
  run. No titles, IDs, locations, or years were inferred or invented."
If you cannot truthfully print that line, do not produce the dashboard.

## Dedupe (keep doing this, it works)
- Pull Gmail confirmations (past 30 days), extract company names from
  "thank you for applying" / "application received" messages.
- Load applied.csv. Normalize URLs (strip query string and trailing slash).
- Skip any role matching an applied company or URL. Log what was skipped.

## Honesty over completeness
A short, fully verified list is the goal. Ten real roles beat forty with three
invented ones, because one fabricated role destroys trust in all forty. When in
doubt, omit and say so. Thin output is an acceptable, honest result.
