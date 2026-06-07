#!/usr/bin/env python3
"""
build_companydb.py — (re)build companies.db from durable, human-readable seeds.

Run this once to create the database, or any time you want to grow the source
list. It is safe to re-run: existing slugs keep their learned hit-rate stats;
only new ones are added.

  python3 build_companydb.py

Seeds, in priority order:
  1. verified_companies.json  — slugs with a confirmed live board (best signal)
  2. CURATED_*                 — a hand-maintained list of well-known companies
                                 likely to post NYC / US-remote PM roles. Unproven
                                 slugs are harmless: a bad one returns nothing and
                                 the DB auto-marks it dead after a few runs.

After building, the scraper (pmfarm.py) reads from companies.db automatically and
records each run's results back, so the list self-cleans over time.
"""

import json
from pathlib import Path

import companydb

JSON_SEED = Path("verified_companies.json")

# ── Curated growth list ───────────────────────────────────────────────────────
# Well-known companies by ATS. Goal: breadth toward NYC + US-remote PM roles.
# Slugs are the board identifier in the ATS URL, e.g.
#   greenhouse.io/<slug>  ·  jobs.ashbyhq.com/<slug>  ·  jobs.lever.co/<slug>
# Unverified guesses are fine — record_results() prunes dead ones automatically.
CURATED_GREENHOUSE = [
    # fintech / payments
    "stripe", "brex", "ramp", "plaid", "affirm", "sofi", "betterment",
    "robinhood", "chime", "current", "mercury", "marqeta", "alloy", "unit",
    "petal", "fundrise", "addepar", "wealthsimple", "public", "stash",
    # consumer / marketplace
    "airbnb", "lyft", "doordash", "instacart", "etsy", "pinterest", "reddit",
    "peloton", "warbyparker", "classpass", "seatgeek", "vimeo", "cameo",
    "thrive-market", "faire", "grubhub", "compass", "fubotv",
    # b2b / saas / infra
    "datadog", "mongodb", "hashicorp", "confluent", "elastic", "gitlab",
    "databricks", "snowflake", "twilio", "sendgrid", "okta", "cloudflare",
    "asana", "airtable", "notion", "figma", "webflow", "amplitude", "segment",
    "braze", "yext", "uipath", "squarespace", "vimeo", "justworks", "gusto",
    "rippling", "deel", "carta", "hubspot", "attentive", "movable-ink",
    # adtech / media
    "doubleverify", "integralads", "mediamath", "taboola", "outbrain",
    "thetradedesk", "nytimes", "spotify", "bloomberg",
    # health / bio
    "oscar", "ro", "hingehealth", "springhealth", "cedar", "komodohealth",
    "flatironhealth", "tempus", "benchling", "capsule", "cityblock",
    "parsleyhealth", "machinify", "devoted-health",
    # security / data / ai
    "verkada", "datarobot", "scaleai", "huggingface", "runwayml",
    # nyc-flavored startups
    "ramp", "fanduel", "draftkings", "trumid", "betterment", "via",
    "boxed", "policygenius", "lemonade", "betterview", "alma", "octave",
]
CURATED_ASHBY = [
    "notion", "ramp", "linear", "mercury", "vanta", "clay", "deel", "retool",
    "openai", "anthropic", "perplexity", "vercel", "cursor", "glean",
    "watershed", "alchemy", "runway", "cohere", "supabase", "replit",
    "scale-ai", "harvey", "sierra", "decagon", "hebbia", "writer",
    "ironclad", "ramp-fintech", "modern-treasury", "rutter", "stytch",
    "knot", "baseten", "together-ai", "fireworks-ai", "pika", "elevenlabs",
    "mistral", "abridge", "ambience", "openevidence", "hippocratic-ai",
    "ondeck", "rampcard", "fal", "browserbase", "lambda", "crusoe",
]
CURATED_LEVER = [
    "shopify", "canva", "asana", "netflix", "spotify", "plaid", "brex",
    "ramp", "attentive", "lattice", "gem", "checkr", "kong", "sourcegraph",
    "voiceflow", "pomelo", "alma", "veeva", "salvohealth", "cents",
    "tenna", "voltus", "kindbody", "found", "nuna", "headway", "spring-health",
    "cedar", "twin-health", "ophelia", "suki-ai", "abridge", "fabric",
]

CURATED = {
    "greenhouse": CURATED_GREENHOUSE,
    "ashby":      CURATED_ASHBY,
    "lever":      CURATED_LEVER,
}


def _rows_from_json() -> list[dict]:
    """Pull verified + candidate + yc slugs out of verified_companies.json."""
    rows: list[dict] = []
    if not JSON_SEED.exists():
        print(f"  (no {JSON_SEED} — skipping JSON seed)")
        return rows
    data = json.loads(JSON_SEED.read_text(encoding="utf-8"))
    cand = data.get("candidates", {})
    for ats in ("greenhouse", "ashby", "lever"):
        # verified entries are the strongest signal → mark live-leaning 'seed'.
        for e in data.get(ats, []) or []:
            rows.append({"ats": ats, "slug": e["slug"], "name": e.get("name"),
                         "tags": e.get("tags", []), "source": "verified"})
        for e in cand.get(ats, []) or []:
            slug = e["slug"] if isinstance(e, dict) else e
            name = e.get("name") if isinstance(e, dict) else None
            rows.append({"ats": ats, "slug": slug, "name": name,
                         "source": "candidate"})
    # YC companies probe across greenhouse first (most common for YC alumni).
    for e in data.get("yc", []) or []:
        rows.append({"ats": "greenhouse", "slug": e["slug"], "name": e.get("name"),
                     "tags": e.get("tags", []), "source": "yc"})
    return rows


def _rows_from_curated() -> list[dict]:
    rows = []
    for ats, slugs in CURATED.items():
        for slug in slugs:
            rows.append({"ats": ats, "slug": slug, "source": "candidate"})
    return rows


def main() -> None:
    companydb.init_db()
    before = companydb.stats().get("_total", 0)

    n_json = companydb.add_companies(_rows_from_json())
    n_cur  = companydb.add_companies(_rows_from_curated())

    s = companydb.stats()
    print(f"Built {companydb.DB_FILE}")
    print(f"  added {n_json} from {JSON_SEED.name}, {n_cur} from curated list "
          f"(was {before}, now {s.pop('_total')} total)")
    for ats, counts in sorted(s.items()):
        line = "  ".join(f"{k}={v}" for k, v in sorted(counts.items()))
        print(f"  {ats:11s} {line}")
    print("\npmfarm.py will now read from companies.db and record results back "
          "each run (live slugs float up, dead 404s get pruned).")


if __name__ == "__main__":
    main()
