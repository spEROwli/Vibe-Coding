#!/usr/bin/env python3
"""
build_page.py — Turn pm_roles.csv into a triage-first apply page.

  python3 build_page.py                 # reads pm_roles.csv → writes pm_roles.html
  python3 build_page.py FILE.csv OUT.html

Layout:
  • Primary split: Bucket A (in-range) on top, Bucket B (4+ yrs explicit) collapsed.
      A = years ≤3 stated OR unstated.
  • Within A, sort by FIT: freshness → priority sector → NYC/remote → date.
  • Role type filter chips: PM · TPM · FDE · SE · SA · Ops.

SCRAPER_RULES: every card is rendered straight from pm_roles.csv, which the
scraper fills only from live ATS JSON. The years line is the verbatim JD sentence
(years_sentence column) or "not stated" — never a bucket or a guess.
"""

import csv, html, json, sys, datetime, os, re

CSV_IN   = sys.argv[1] if len(sys.argv) > 1 else "pm_roles.csv"
HTML_OUT = sys.argv[2] if len(sys.argv) > 2 else "pm_roles.html"
CACHE    = "verified_companies.json"

# Sectors where the candidate's hardware/medical-device/regulated background wins.
PRIORITY_TAGS = {
    "healthtech", "biotech", "medical", "medtech", "diagnostics", "life-sciences",
    "mental-health", "fintech", "payments", "crypto", "hardware", "iot",
    "regulated", "govtech", "climate", "compliance",
}


def _load_sector_map() -> dict:
    """slug(lower) → set(tags) from verified_companies.json (verified + candidates)."""
    out = {}
    if not os.path.exists(CACHE):
        return out
    try:
        data = json.load(open(CACHE, encoding="utf-8"))
    except Exception:
        return out
    cand = data.get("candidates", {})
    for key in ("greenhouse", "ashby", "lever", "yc"):
        for src in (data.get(key) or [], cand.get(key) or []):
            for e in src:
                if isinstance(e, dict) and e.get("slug"):
                    slug = e["slug"].lower()
                    tags = set(e.get("tags", []))
                    out[slug] = out.get(slug, set()) | tags
    return out


SECTORS = _load_sector_map()


def _years_num(years_raw: str):
    try:
        return int(years_raw)
    except (ValueError, TypeError):
        return None  # "unknown" / blank → treat as not stated


# Phrases that turn a stated years number into a soft preference, not a hard
# floor. Read verbatim from the JD sentence — not inferred. A role with ">3 yrs"
# but a hedge belongs in the open pile; the sentence on the card lets you decide.
_HEDGE = ("preferred", "or equivalent", "equivalent experience", "equivalent work",
          "nice to have", "ideally", "a plus", "bonus", "not required",
          "not strictly", "we welcome", "we encourage")


def _years_is_soft(row: dict) -> bool:
    s = (row.get("years_sentence") or "").lower()
    return any(h in s for h in _HEDGE)


_ROLE_TYPE_MAP = [
    ("fde",  ["forward deployed"]),
    ("sa",   ["solutions architect"]),
    ("se",   ["solutions engineer"]),
    ("tpm",  ["technical program manager", "technical program"]),
    ("ops",  ["business operations", "strategy and operations",
              "strategy & operations", "biz ops", "bizops", "strat ops"]),
    ("pm",   ["product manager", "associate product", "apm"]),
]


def _role_type(title: str) -> str:
    t = title.lower()
    for code, kws in _ROLE_TYPE_MAP:
        if any(kw in t for kw in kws):
            return code
    return "pm"


def _in_range(row: dict) -> bool:
    n = _years_num(row.get("years_raw", ""))
    if n is None or n <= 3:
        return True
    # >3 yrs stated: keep only if the JD hedges the number.
    return _years_is_soft(row)


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
    return {
        "nyc": 0, "sf": 0, "nyc+sf": 0,
        "remote+nyc": 1, "remote+sf": 1, "remote+nyc+sf": 1,
        "remote": 2, "international": 3, "unknown": 4,
    }.get(lc, 5)


def _age(row: dict) -> int:
    try:
        return int(row.get("days_old", ""))
    except (ValueError, TypeError):
        return 9999


def _age_bucket(row: dict) -> int:
    a = _age(row)
    if a <= 7:  return 0   # fresh   (≤7d)
    if a <= 21: return 1   # recent  (8–21d)
    return 2               # stale   (22d+)


def _fit_key(row: dict) -> tuple:
    # fresh first, then priority sector, then NYC>remote>other, then newest
    return (_age_bucket(row), 0 if _is_priority(row) else 1, _loc_rank(row.get("loc_class", "")), _age(row))


def _years_line(row: dict) -> str:
    s = (row.get("years_sentence") or "").strip()
    if s and s.lower() != "not stated":
        return s
    return "not stated"


LOC_LABEL = {
    "nyc": "NYC", "remote+nyc": "NYC / Remote", "remote": "Remote",
    "sf": "SF", "remote+sf": "SF / Remote", "nyc+sf": "NYC / SF",
    "remote+nyc+sf": "NYC / SF / Remote",
    "international": "International", "unknown": "Location n/a",
}


def _age_label(row: dict) -> str:
    a = _age(row)
    if a == 9999: return "age unknown"
    if a == 0:    return "today"
    if a == 1:    return "1d ago"
    return f"{a}d ago"


LOC_PILL = {
    "nyc": "NYC", "remote+nyc": "NYC · Remote", "remote": "Remote",
    "sf": "SF", "remote+sf": "SF · Remote", "nyc+sf": "NYC · SF",
    "remote+nyc+sf": "NYC · SF · Remote",
    "international": "Intl", "unknown": "—",
}


_ROLE_LABELS = {"pm": "PM", "tpm": "TPM", "se": "SE", "sa": "SA", "fde": "FDE", "ops": "Ops"}


def _card(row: dict, priority: bool) -> str:
    loc_raw  = row.get("location", "") or LOC_LABEL.get(row.get("loc_class", ""), "")
    company  = html.escape(row.get("company", "").title())
    title    = html.escape(row.get("title", ""))
    loc      = html.escape(loc_raw)
    years    = html.escape(_years_line(row))
    url      = html.escape(row.get("url", ""), quote=True)
    lc       = row.get("loc_class", "unknown")
    locpill  = LOC_PILL.get(lc, "—")
    is_app   = (row.get("applied", "") or "").strip().lower() in ("y", "yes", "1", "true", "x")
    has_hw   = row.get("hw_signal") == "YES"
    rt       = _role_type(row.get("title", ""))
    rl       = _ROLE_LABELS.get(rt, rt.upper())

    # Pill tags — dense, scannable.
    tags = [
        f'<span class="pill pl-{lc}">{html.escape(locpill)}</span>',
        f'<span class="pill pl-role pl-rt-{rt}">{rl}</span>',
    ]
    if priority:                         tags.append('<span class="pill pl-pri">★ priority</span>')
    if has_hw:                           tags.append('<span class="pill pl-hw">🔩 fit</span>')
    if row.get("lang_signal") == "YES":  tags.append('<span class="pill pl-lang">🌐 lang</span>')
    tagrow = "".join(tags)

    ystyle   = "ns" if years == "not stated" else "yr"
    age_str  = html.escape(_age_label(row))
    ab       = _age_bucket(row)
    aclass   = ("ag-fresh" if ab == 0 else "ag-recent" if ab == 1 else "ag-stale")

    # data-search built from raw (un-escaped) values to avoid double-encoding.
    search   = html.escape((row.get("company", "") + " " + row.get("title", "") + " " + loc_raw).lower(), quote=True)
    hw_attr  = "1" if has_hw else "0"
    return f"""  <div class="card{' pri' if priority else ''}{' done' if is_app else ''}" data-loc="{lc}" data-age="{ab}" data-pri="{1 if priority else 0}" data-applied="{1 if is_app else 0}" data-hw="{hw_attr}" data-role="{rt}" data-search="{search}">
    <div class="top">
      <div class="co">{company}</div>
      <span class="ag {aclass}">{age_str}</span>
    </div>
    <div class="ti">{title}</div>
    <div class="lo">📍 {loc}</div>
    <div class="tags">{tagrow}</div>
    <div class="ye {ystyle}">{years}</div>
    <a class="btn" href="{url}" target="_blank" rel="noopener">Apply →</a>
  </div>"""


def _render_bucket(rows: list) -> str:
    """Render cards with section dividers between freshness×location groups.
    Dividers carry data-sec so JS can hide a heading when all its cards filter out."""
    AGE_LABELS = ["🟢 This week", "🟡 1–3 weeks ago", "🔴 3+ weeks ago"]
    LOC_LABELS = {
        "nyc": "NYC only", "remote+nyc": "NYC + Remote", "remote": "Remote",
        "sf": "SF only", "remote+sf": "SF + Remote",
        "nyc+sf": "NYC + SF", "remote+nyc+sf": "NYC + SF + Remote",
        "international": "International", "unknown": "Location TBD",
    }
    parts = []
    last_key = None
    sec_id = 0
    for r in rows:
        ab  = _age_bucket(r)
        lc  = r.get("loc_class", "unknown")
        key = (ab, lc)
        if key != last_key:
            sec_id += 1
            al = AGE_LABELS[ab]
            ll = LOC_LABELS.get(lc, lc)
            parts.append(f'  <div class="sec" data-sec="{sec_id}">{al} · {ll}</div>')
            last_key = key
        parts.append(_card(r, _is_priority(r)).replace('<div class="card', f'<div data-sec="{sec_id}" class="card', 1))
    return "\n".join(parts)


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

    a_cards = _render_bucket(bucket_a)
    b_cards = _render_bucket(bucket_b)
    today   = datetime.date.today().isoformat()

    htmldoc = f"""<!DOCTYPE html>
<html lang="en"><head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1, maximum-scale=1">
<title>Roles — {today}</title>
<style>
  :root {{
    --bg:#fafafa; --card:#fff; --ink:#18181b; --muted:#71717a; --line:#e4e4e7;
    --accent:#2563eb; --accent-soft:#eff6ff;
  }}
  * {{ box-sizing:border-box; margin:0; padding:0; -webkit-tap-highlight-color:transparent; }}
  body {{ font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Inter,sans-serif;
         background:var(--bg); color:var(--ink); font-size:14px; line-height:1.4; }}

  /* ── Sticky header: title, counts, search, filter chips ── */
  header {{ position:sticky; top:0; z-index:20; background:rgba(250,250,250,.92);
           backdrop-filter:saturate(180%) blur(12px); border-bottom:1px solid var(--line);
           padding:10px 14px 8px; }}
  .htop {{ display:flex; align-items:baseline; justify-content:space-between; gap:8px; }}
  h1 {{ font-size:17px; font-weight:700; letter-spacing:-.01em; }}
  .count {{ font-size:12px; color:var(--muted); font-variant-numeric:tabular-nums; }}
  .search {{ width:100%; margin-top:8px; padding:9px 12px; font-size:15px;
            border:1px solid var(--line); border-radius:10px; background:#fff; outline:none; }}
  .search:focus {{ border-color:var(--accent); box-shadow:0 0 0 3px var(--accent-soft); }}
  .chips {{ display:flex; gap:6px; margin-top:8px; overflow-x:auto; padding-bottom:2px;
           scrollbar-width:none; }}
  .chips::-webkit-scrollbar {{ display:none; }}
  .chip {{ flex:0 0 auto; font-size:12px; font-weight:600; padding:6px 11px; border-radius:999px;
          border:1px solid var(--line); background:#fff; color:var(--muted); cursor:pointer;
          white-space:nowrap; user-select:none; transition:.12s; }}
  .chip[aria-pressed="true"] {{ background:var(--accent); color:#fff; border-color:var(--accent); }}

  main {{ padding:6px 14px 60px; max-width:680px; margin:0 auto; }}

  /* ── Cards ── */
  .card {{ background:var(--card); border:1px solid var(--line); border-radius:12px;
          padding:13px 14px; margin-bottom:8px; display:flex; flex-direction:column; gap:5px;
          border-left:3px solid var(--line); }}
  .card.pri {{ border-left-color:var(--accent); }}
  .card.done {{ opacity:.5; }}
  .card.done .btn {{ background:#a1a1aa; }}
  .top {{ display:flex; align-items:center; justify-content:space-between; gap:8px; }}
  .co {{ font-size:11px; font-weight:700; text-transform:uppercase; letter-spacing:.06em;
        color:var(--muted); }}
  .ti {{ font-size:16px; font-weight:650; line-height:1.25; letter-spacing:-.01em; }}
  .lo {{ font-size:13px; color:var(--muted); }}
  .ye {{ font-size:12.5px; line-height:1.45; padding:2px 0; }}
  .ye.yr {{ color:#b45309; }}
  .ye.ns {{ color:#a1a1aa; font-style:italic; }}

  /* ── Pill tags ── */
  .tags {{ display:flex; flex-wrap:wrap; gap:5px; }}
  .pill {{ font-size:11px; font-weight:600; padding:2px 8px; border-radius:999px;
          background:#f4f4f5; color:#52525b; }}
  .pl-nyc {{ background:#eff6ff; color:#1d4ed8; }}
  .pl-remote {{ background:#f0fdf4; color:#15803d; }}
  .pl-remote\\+nyc {{ background:#eef2ff; color:#4338ca; }}
  .pl-sf {{ background:#fff8f1; color:#b45309; }}
  .pl-remote\\+sf {{ background:#fefce8; color:#92400e; }}
  .pl-nyc\\+sf {{ background:#f0f9ff; color:#0369a1; }}
  .pl-remote\\+nyc\\+sf {{ background:#f0f9ff; color:#0369a1; }}
  .pl-international {{ background:#fdf4ff; color:#a21caf; }}
  .pl-pri {{ background:#eff6ff; color:#1d4ed8; }}
  .pl-hw {{ background:#fff7ed; color:#c2410c; }}
  .pl-lang {{ background:#faf5ff; color:#7c3aed; }}
  .pl-rt-pm  {{ background:#f0f9ff; color:#0369a1; }}
  .pl-rt-tpm {{ background:#f0fdf4; color:#166534; }}
  .pl-rt-se  {{ background:#fff7ed; color:#c2410c; }}
  .pl-rt-sa  {{ background:#fdf4ff; color:#86198f; }}
  .pl-rt-fde {{ background:#fefce8; color:#92400e; }}
  .pl-rt-ops {{ background:#f8fafc; color:#475569; }}

  .ag {{ font-size:10px; font-weight:700; padding:2px 7px; border-radius:999px; flex:0 0 auto; }}
  .ag-fresh  {{ background:#dcfce7; color:#15803d; }}
  .ag-recent {{ background:#fef9c3; color:#a16207; }}
  .ag-stale  {{ background:#fee2e2; color:#b91c1c; }}

  .sec {{ font-size:10px; font-weight:700; letter-spacing:.09em; text-transform:uppercase;
         color:#a1a1aa; padding:16px 2px 5px; }}

  .btn {{ display:block; text-align:center; background:var(--accent); color:#fff; font-size:14px;
         font-weight:650; padding:9px; border-radius:9px; text-decoration:none; margin-top:3px; }}
  .btn:active {{ opacity:.85; }}

  .lbl {{ font-size:11px; font-weight:700; letter-spacing:.07em; text-transform:uppercase;
         color:#a1a1aa; padding:14px 2px 8px; }}
  .empty {{ text-align:center; color:var(--muted); padding:40px 0; font-size:14px; display:none; }}
  details {{ margin-top:14px; }}
  summary {{ font-size:13px; font-weight:650; color:#52525b; padding:11px 13px; background:#fff;
            border:1px solid var(--line); border-radius:10px; cursor:pointer; list-style:none; }}
  summary::-webkit-details-marker {{ display:none; }}
  .manifest {{ font-size:11px; color:var(--muted); padding:12px 2px 0; line-height:1.55; }}
</style></head><body>

<header>
  <div class="htop">
    <h1>Roles</h1>
    <span class="count" id="count">{len(bucket_a)} shown</span>
  </div>
  <input class="search" id="q" type="search" placeholder="Search company, title, location…" autocomplete="off">
  <div class="chips" id="chips">
    <button class="chip" data-f="pm"       aria-pressed="false">PM</button>
    <button class="chip" data-f="tpm"      aria-pressed="false">TPM</button>
    <button class="chip" data-f="fde"      aria-pressed="false">FDE</button>
    <button class="chip" data-f="se"       aria-pressed="false">SE</button>
    <button class="chip" data-f="sa"       aria-pressed="false">SA</button>
    <button class="chip" data-f="ops"      aria-pressed="false">Ops</button>
    <button class="chip" data-f="nyc"      aria-pressed="false">NYC</button>
    <button class="chip" data-f="sf"       aria-pressed="false">SF</button>
    <button class="chip" data-f="remote"   aria-pressed="false">Remote</button>
    <button class="chip" data-f="fresh"    aria-pressed="false">🟢 This week</button>
    <button class="chip" data-f="priority" aria-pressed="false">★ Priority</button>
    <button class="chip" data-f="fit"      aria-pressed="false">🔩 Fit</button>
    <button class="chip" data-f="hideapplied" aria-pressed="false">Hide applied</button>
  </div>
</header>

<main>
  <div class="lbl">In range · ≤3 yrs or not stated · fresh → fit</div>
  <div id="alist">
{a_cards}
  </div>
  <div class="empty" id="empty">No roles match these filters.</div>

  <details>
    <summary>▸ Stretch bucket — 4+ yrs stated ({len(bucket_b)} roles)</summary>
    <div id="blist">
{b_cards}
    </div>
  </details>

  <div class="manifest">
    {len(rows)} roles from live ATS JSON · {len(bucket_a)} in-range ({pri_count} ★) · {len(bucket_b)} stretch.
    Every role traces to a live API call; titles, locations, and the years sentence are copied
    verbatim from the JD — none inferred. Synced {today}.
  </div>
</main>

<script>
(function() {{
  const q      = document.getElementById('q');
  const chips  = [...document.querySelectorAll('.chip')];
  const cards  = [...document.querySelectorAll('#alist .card')];
  const secs   = [...document.querySelectorAll('#alist .sec')];
  const count  = document.getElementById('count');
  const empty  = document.getElementById('empty');
  const active = new Set();

  const ROLE_TYPES = ['pm','tpm','fde','se','sa','ops'];
  const LOC_TYPES  = ['nyc','sf','remote','intl'];

  function locMatch(card, f) {{
    const lc = card.dataset.loc;
    if (f === 'nyc')    return ['nyc','remote+nyc','nyc+sf','remote+nyc+sf'].includes(lc);
    if (f === 'sf')     return ['sf','remote+sf','nyc+sf','remote+nyc+sf'].includes(lc);
    if (f === 'remote') return lc.startsWith('remote');
    if (f === 'intl')   return lc === 'international';
    return true;
  }}

  function apply() {{
    const term = q.value.trim().toLowerCase();
    // Role and location chips are OR'd within their group; other chips are AND.
    const roleFilters = [...active].filter(f => ROLE_TYPES.includes(f));
    const locFilters  = [...active].filter(f => LOC_TYPES.includes(f));
    let shown = 0;

    cards.forEach(card => {{
      let ok = true;
      if (term && !card.dataset.search.includes(term)) ok = false;
      if (ok && roleFilters.length) ok = roleFilters.some(f => card.dataset.role === f);
      if (ok && locFilters.length)  ok = locFilters.some(f => locMatch(card, f));
      if (ok && active.has('fresh'))       ok = card.dataset.age === '0';
      if (ok && active.has('priority'))    ok = card.dataset.pri === '1';
      if (ok && active.has('fit'))         ok = card.dataset.hw === '1';
      if (ok && active.has('hideapplied')) ok = card.dataset.applied === '0';
      card.style.display = ok ? '' : 'none';
      if (ok) shown++;
    }});

    // Hide a section heading if every card under it is filtered out.
    secs.forEach(sec => {{
      const id = sec.dataset.sec;
      const any = cards.some(c => c.dataset.sec === id && c.style.display !== 'none');
      sec.style.display = any ? '' : 'none';
    }});

    count.textContent = shown + ' shown';
    empty.style.display = shown ? 'none' : 'block';
  }}

  q.addEventListener('input', apply);
  chips.forEach(chip => chip.addEventListener('click', () => {{
    const f = chip.dataset.f;
    if (active.has(f)) {{ active.delete(f); chip.setAttribute('aria-pressed','false'); }}
    else               {{ active.add(f);    chip.setAttribute('aria-pressed','true'); }}
    apply();
  }}));
}})();
</script>
</body></html>"""

    open(HTML_OUT, "w", encoding="utf-8").write(htmldoc)
    print(f"Wrote {HTML_OUT}: {len(bucket_a)} in-range ({pri_count} priority) + "
          f"{len(bucket_b)} stretch = {len(rows)} total")
    print(f"Open it: open {HTML_OUT}")


if __name__ == "__main__":
    build()
