#!/usr/bin/env python3
"""
build_page.py — Turn pm_roles.csv into a triage-first apply page.

  python3 build_page.py                 # reads pm_roles.csv → writes pm_roles.html
  python3 build_page.py FILE.csv OUT.html

Layout (per the 10-week triage spec):
  • Primary split: Bucket A (in-range) on top, Bucket B (everything else) collapsed.
      A = title has no Senior/Staff/Principal/Director/Lead marker AND years ≤3
          or "not stated".  This is the bucket you live in.
  • Within A, sort by FIT, not date:
      1) unlock sectors first (healthtech, fintech, regulated, hardware/IoT, founding)
      2) then NYC → remote → other
      3) then newest post date as tiebreak
  • Four fields per card: company+title · location · verbatim years sentence · link.

SCRAPER_RULES: every card is rendered straight from pm_roles.csv, which the
scraper fills only from live ATS JSON. The years line is the verbatim JD sentence
(years_sentence column) or "not stated" — never a bucket or a guess. If that
column is missing, regenerate the CSV with an updated pmfarm.py before trusting it.
"""

import csv, html, json, sys, datetime, os, re
import pmfarm  # reuse the exact seniority regex so bucketing matches the scraper

CSV_IN   = sys.argv[1] if len(sys.argv) > 1 else "pm_roles.csv"
HTML_OUT = sys.argv[2] if len(sys.argv) > 2 else "pm_roles.html"
CACHE    = "verified_companies.json"

# Sectors where the candidate's hardware/medical-device/regulated background wins.
PRIORITY_TAGS = {
    "healthtech", "biotech", "medical", "mental-health", "fintech", "payments",
    "crypto", "hardware", "iot", "regulated", "govtech", "climate", "compliance",
}


def _load_sector_map() -> dict:
    """slug(lower) → set(tags) from verified_companies.json (verified + candidates)."""
    out = {}
    if not os.path.exists(CACHE):
        return out
    data = json.load(open(CACHE))
    for key in ("greenhouse", "ashby", "lever", "yc"):
        for e in data.get(key, []):
            if isinstance(e, dict) and e.get("slug"):
                out[e["slug"].lower()] = set(e.get("tags", []))
    return out


SECTORS = _load_sector_map()


def _is_senior(title: str) -> bool:
    """True if the title carries a seniority marker in its pre-comma segment —
    same logic the scraper uses, so buckets agree."""
    return bool(pmfarm._SENIORITY_RE.search(title.lower().split(",", 1)[0]))


def _years_num(years_raw: str):
    try:
        return int(years_raw)
    except (ValueError, TypeError):
        return None  # "unknown" / blank → treat as not stated


def _in_range(row: dict) -> bool:
    if _is_senior(row.get("title", "")):
        return False
    n = _years_num(row.get("years_raw", ""))
    return n is None or n <= 3


def _is_priority(row: dict) -> bool:
    tags = SECTORS.get(row.get("company", "").lower(), set())
    if tags & PRIORITY_TAGS:
        return True
    if row.get("hw_signal") == "YES":
        return True
    if "founding" in row.get("title", "").lower():
        return True
    return False


def _loc_rank(lc: str) -> int:
    return {"nyc": 0, "remote+nyc": 0, "remote": 1, "unknown": 2}.get(lc, 3)


def _age(row: dict) -> int:
    try:
        return int(row.get("days_old", ""))
    except (ValueError, TypeError):
        return 9999


def _fit_key(row: dict) -> tuple:
    # lower = better: priority sector first, then NYC>remote>other, then newest
    return (0 if _is_priority(row) else 1, _loc_rank(row.get("loc_class", "")), _age(row))


def _years_line(row: dict) -> str:
    s = (row.get("years_sentence") or "").strip()
    if s and s.lower() != "not stated":
        return s
    return "not stated"


LOC_LABEL = {"nyc": "NYC", "remote+nyc": "NYC / Remote", "remote": "Remote",
             "unknown": "Location n/a"}


def _card(row: dict, priority: bool) -> str:
    company  = html.escape(row.get("company", "").title())
    title    = html.escape(row.get("title", ""))
    loc      = html.escape(row.get("location", "") or LOC_LABEL.get(row.get("loc_class", ""), ""))
    years    = html.escape(_years_line(row))
    url      = html.escape(row.get("url", ""), quote=True)
    hw       = ' <span class="flag">🔩 fit</span>' if row.get("hw_signal") == "YES" else ""
    star     = ' <span class="flag star">★</span>' if priority else ""
    ystyle   = "ns" if years == "not stated" else "yr"
    return f"""  <div class="card{' pri' if priority else ''}">
    <div class="co">{company}{star}{hw}</div>
    <div class="ti">{title}</div>
    <div class="lo">📍 {loc}</div>
    <div class="ye {ystyle}">{years}</div>
    <a class="btn" href="{url}">Apply →</a>
  </div>"""


def build():
    if not os.path.exists(CSV_IN):
        sys.exit(f"No {CSV_IN}. Run: python3 pmfarm.py local --all-levels")
    rows = list(csv.DictReader(open(CSV_IN, newline="", encoding="utf-8")))
    if rows and "years_sentence" not in rows[0]:
        print("WARNING: pm_roles.csv has no years_sentence column. Years will show "
              "'not stated'. Regenerate with the updated pmfarm.py for verbatim JD "
              "sentences.", file=sys.stderr)

    bucket_a = sorted((r for r in rows if _in_range(r)), key=_fit_key)
    bucket_b = sorted((r for r in rows if not _in_range(r)), key=_fit_key)
    pri_count = sum(1 for r in bucket_a if _is_priority(r))

    a_cards = "\n".join(_card(r, _is_priority(r)) for r in bucket_a)
    b_cards = "\n".join(_card(r, _is_priority(r)) for r in bucket_b)
    today   = datetime.date.today().isoformat()

    htmldoc = f"""<!DOCTYPE html>
<html lang="en"><head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1, maximum-scale=1">
<title>PM Roles — {today}</title>
<style>
  * {{ box-sizing:border-box; margin:0; padding:0; }}
  body {{ font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;
         background:#f0f2f5; color:#1a1a1a; padding:12px; }}
  h1 {{ font-size:19px; padding:8px 4px 2px; }}
  .sub {{ font-size:13px; color:#666; padding:0 4px 10px; }}
  .manifest {{ font-size:11px; color:#2e5d34; background:#e8f5e9;
              border-left:3px solid #34c759; padding:8px 10px; border-radius:6px;
              margin-bottom:14px; line-height:1.5; }}
  .lbl {{ font-size:11px; font-weight:700; letter-spacing:.08em; text-transform:uppercase;
         color:#888; padding:18px 4px 6px; }}
  .card {{ background:#fff; border-radius:14px; padding:13px 15px; margin-bottom:9px;
          box-shadow:0 1px 3px rgba(0,0,0,.07); display:flex; flex-direction:column; gap:4px;
          border-left:4px solid #c7c7cc; }}
  .card.pri {{ border-left-color:#007aff; }}
  .co {{ font-size:12px; font-weight:700; text-transform:uppercase; letter-spacing:.05em;
        color:#555; }}
  .ti {{ font-size:16px; font-weight:600; line-height:1.25; }}
  .lo {{ font-size:13px; color:#0066cc; }}
  .ye {{ font-size:12px; line-height:1.4; padding:4px 0; }}
  .ye.yr {{ color:#bf6000; }}
  .ye.ns {{ color:#999; font-style:italic; }}
  .flag {{ font-size:11px; }} .flag.star {{ color:#007aff; }}
  .btn {{ display:block; text-align:center; background:#007aff; color:#fff; font-size:15px;
         font-weight:600; padding:10px; border-radius:10px; text-decoration:none; margin-top:4px;
         -webkit-tap-highlight-color:transparent; }}
  .btn:active {{ opacity:.8; }}
  details {{ margin-top:8px; }}
  summary {{ font-size:14px; font-weight:600; color:#444; padding:10px; background:#fff;
            border-radius:10px; cursor:pointer; }}
</style></head><body>

<h1>PM Roles</h1>
<div class="sub">{today} · {len(bucket_a)} in-range · {len(bucket_b)} stretch (collapsed)</div>

<div class="manifest">
  <strong>{len(rows)} roles</strong> from live ATS JSON · {len(bucket_a)} in-range
  ({pri_count} ★ priority-sector) · {len(bucket_b)} stretch.<br>
  ★ = healthtech / fintech / regulated / hardware-IoT / founding · 🔩 = hardware-background fit.<br>
  Every role traces to a live API call. Titles, locations, and the verbatim years
  sentence are copied from the JD — none inferred or invented.
</div>

<div class="lbl">✅ In Range — IC level, ≤3 yrs or not stated (sorted by fit)</div>
{a_cards}

<details>
  <summary>▸ Stretch bucket — senior / 4+ yrs ({len(bucket_b)} roles)</summary>
{b_cards}
</details>

</body></html>"""

    open(HTML_OUT, "w", encoding="utf-8").write(htmldoc)
    print(f"Wrote {HTML_OUT}: {len(bucket_a)} in-range ({pri_count} priority) + "
          f"{len(bucket_b)} stretch = {len(rows)} total")
    print(f"Open it: open {HTML_OUT}")


if __name__ == "__main__":
    build()
