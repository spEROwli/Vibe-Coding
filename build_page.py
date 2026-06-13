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
    if priority:                         tags.append('<span class="pill pl-pri">priority</span>')
    if has_hw:                           tags.append('<span class="pill pl-hw">fit</span>')
    if row.get("lang_signal") == "YES":  tags.append('<span class="pill pl-lang">lang</span>')
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
    <div class="lo">{loc}</div>
    <div class="tags">{tagrow}</div>
    <div class="ye {ystyle}">{years}</div>
    <a class="btn" href="{url}" target="_blank" rel="noopener">Apply →</a>
  </div>"""


def _render_bucket(rows: list) -> str:
    """Render cards with section dividers between freshness×location groups.
    Dividers carry data-sec so JS can hide a heading when all its cards filter out."""
    AGE_LABELS = ["This week", "1–3 weeks ago", "3+ weeks ago"]
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
            parts.append(f'  <div class="sec" data-sec="{sec_id}" data-tier="{ab}">{al} · {ll}</div>')
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
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=EB+Garamond:ital,wght@0,400;0,500;1,400&family=Inter:wght@400;500;700&family=JetBrains+Mono:wght@400;500&display=swap" rel="stylesheet">
<style>
  /* Couch Fisherman design system — warm sand, rust accent, light + dark */
  :root {{
    --serif:'EB Garamond',Georgia,serif;
    --sans:'Inter',-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;
    --mono:'JetBrains Mono',ui-monospace,monospace;
    --bg:#EDE7DC; --card:#FAF7F1; --header-bg:rgba(237,231,220,.82);
    --fg1:#1C1C1A; --fg2:#4A4A46; --fg3:#8A8882; --line:#D6CFC2; --rust:#B3502E;
    --pill-bg:rgba(28,28,26,.045); --pill-fg:#6B6862; --pill-line:rgba(28,28,26,.07);
    --pri-bg:rgba(179,80,46,.10); --pri-fg:#8C3E22;
    --dot-fresh:#B3502E; --dot-recent:#B6A98F; --dot-stale:#CFC7B8;
    --chip-fg:#4A4A46; --chip-border:#D6CFC2; --chip-active-bg:#1C1C1A; --chip-active-fg:#F5F1EA;
    --shadow1:0 1px 2px rgba(28,28,26,.05); --shadow2:0 12px 30px -16px rgba(28,28,26,.28);
  }}
  @media (prefers-color-scheme: dark) {{
    :root {{
      --bg:#1A1815; --card:#232019; --header-bg:rgba(26,24,21,.82);
      --fg1:#F2EEE5; --fg2:#C2BCAE; --fg3:#8C887B; --line:#36322A; --rust:#CA6038;
      --pill-bg:rgba(245,241,234,.06); --pill-fg:#B0AB9D; --pill-line:rgba(245,241,234,.06);
      --pri-bg:rgba(202,96,56,.18); --pri-fg:#E59873;
      --dot-fresh:#CA6038; --dot-recent:#8C887B; --dot-stale:#4A463C;
      --chip-fg:#C2BCAE; --chip-border:#3C382F; --chip-active-bg:#F2EEE5; --chip-active-fg:#1A1815;
      --shadow1:0 1px 2px rgba(0,0,0,.32); --shadow2:0 12px 30px -14px rgba(0,0,0,.6);
    }}
  }}
  * {{ box-sizing:border-box; margin:0; padding:0; -webkit-tap-highlight-color:transparent; }}
  body {{ font-family:var(--sans); background:var(--bg); color:var(--fg1);
         font-size:13px; line-height:1.4; -webkit-font-smoothing:antialiased; }}

  /* ── Sticky header ── */
  header {{ position:sticky; top:0; z-index:20; background:var(--header-bg);
           backdrop-filter:saturate(150%) blur(12px); -webkit-backdrop-filter:saturate(150%) blur(12px);
           border-bottom:.5px solid var(--line); padding:16px 18px 10px; }}
  .htop {{ display:flex; align-items:baseline; justify-content:space-between; gap:10px; }}
  .hbrand {{ display:flex; align-items:baseline; gap:11px; min-width:0; }}
  h1 {{ font-family:var(--serif); font-size:26px; font-weight:500; letter-spacing:-.015em; color:var(--fg1); }}
  h1::after {{ content:"."; color:var(--rust); }}
  .subtitle {{ font-family:var(--mono); font-size:11px; color:var(--fg3); white-space:nowrap; }}
  .count {{ font-family:var(--mono); font-size:11px; color:var(--fg3); font-variant-numeric:tabular-nums; }}
  .search {{ width:100%; margin-top:12px; padding:7px 0; font-family:var(--sans); font-size:14px;
            color:var(--fg1); background:transparent; border:none; border-bottom:.5px solid var(--line); outline:none; }}
  .search::placeholder {{ color:var(--fg3); }}
  .search:focus {{ border-bottom-color:var(--rust); }}
  .chips {{ display:flex; gap:6px; margin-top:12px; overflow-x:auto; padding-bottom:4px; scrollbar-width:none; }}
  .chips::-webkit-scrollbar {{ display:none; }}
  .chip {{ flex:0 0 auto; font-family:var(--sans); font-size:12px; font-weight:500; padding:6px 11px;
          border-radius:4px; border:.5px solid var(--chip-border); background:transparent; color:var(--chip-fg);
          cursor:pointer; white-space:nowrap; user-select:none; appearance:none; -webkit-appearance:none;
          transition:background .15s,color .15s,border-color .15s; }}
  .chip[aria-pressed="true"] {{ background:var(--chip-active-bg); color:var(--chip-active-fg); border-color:var(--chip-active-bg); }}

  main {{ padding:6px 18px 60px; max-width:680px; margin:0 auto; }}

  /* ── Cards ── */
  .card {{ background:var(--card); border:.5px solid var(--line); border-left:2px solid transparent;
          border-radius:8px; padding:14px 16px; margin-bottom:8px; display:flex; flex-direction:column; gap:6px;
          box-shadow:var(--shadow1);
          transition:transform .15s cubic-bezier(.2,0,0,1),box-shadow .15s,border-color .15s; }}
  .card:hover {{ transform:translateY(-1px); box-shadow:var(--shadow2); border-color:var(--rust); }}
  .card.pri {{ border-left-color:var(--rust); }}
  .card.done {{ opacity:.5; }}
  .card.done .btn {{ background:var(--fg3); }}
  .top {{ display:flex; align-items:center; justify-content:space-between; gap:10px; }}
  .co {{ font-family:var(--mono); font-size:11px; letter-spacing:.02em; color:var(--fg3); white-space:nowrap; }}
  .ti {{ font-family:var(--sans); font-size:15px; font-weight:700; line-height:1.25; letter-spacing:-.012em; color:var(--fg1); }}
  .lo {{ font-family:var(--sans); font-size:12px; color:var(--fg3); line-height:1.4; }}
  .ye {{ font-family:var(--sans); font-size:12.5px; line-height:1.45; color:var(--fg2); padding-top:2px; }}
  .ye.ns {{ font-family:var(--serif); font-style:italic; font-size:13px; color:var(--fg3); }}

  /* ── Pills — uniform low-contrast; only priority carries the rust accent ── */
  .tags {{ display:flex; flex-wrap:wrap; align-items:center; gap:6px; margin-top:2px; }}
  .pill {{ font-family:var(--mono); font-size:10px; font-weight:500; letter-spacing:.03em; line-height:15px;
          padding:2px 7px; border-radius:2px; background:var(--pill-bg); color:var(--pill-fg);
          border:.5px solid var(--pill-line); white-space:nowrap; }}
  .pl-pri {{ background:var(--pri-bg); color:var(--pri-fg); border-color:transparent; }}

  /* ── Age dot + label (mono) ── */
  .ag {{ font-family:var(--mono); font-size:11px; color:var(--fg3); font-variant-numeric:tabular-nums;
        display:inline-flex; align-items:center; gap:6px; flex:0 0 auto; }}
  .ag::before {{ content:""; width:5px; height:5px; border-radius:50%; background:var(--dot-stale); flex:0 0 auto; }}
  .ag-fresh::before  {{ background:var(--dot-fresh); }}
  .ag-recent::before {{ background:var(--dot-recent); }}

  /* ── Section divider with tier dot + hairline rule ── */
  .sec {{ display:flex; align-items:center; gap:9px; font-family:var(--sans); font-size:10px; font-weight:700;
         letter-spacing:.11em; text-transform:uppercase; color:var(--fg3); padding:22px 0 9px; }}
  .sec::before {{ content:""; width:6px; height:6px; border-radius:1px; background:var(--dot-stale); flex:0 0 auto; }}
  .sec[data-tier="0"]::before {{ background:var(--dot-fresh); }}
  .sec[data-tier="1"]::before {{ background:var(--dot-recent); }}
  .sec::after {{ content:""; flex:1; height:0; border-top:.5px solid var(--line); }}

  .btn {{ display:block; text-align:center; background:var(--chip-active-bg); color:var(--chip-active-fg);
         font-family:var(--sans); font-size:13px; font-weight:600; padding:9px; border-radius:6px;
         text-decoration:none; margin-top:4px; transition:opacity .15s; }}
  .btn:hover {{ opacity:.88; }}

  .lbl {{ font-family:var(--mono); font-size:10px; letter-spacing:.07em; text-transform:uppercase;
         color:var(--fg3); padding:14px 0 2px; }}
  .empty {{ text-align:center; font-family:var(--serif); font-style:italic; font-size:16px;
           color:var(--fg3); padding:64px 0; display:none; }}
  details {{ margin-top:14px; }}
  summary {{ font-family:var(--sans); font-size:13px; font-weight:600; color:var(--fg2); padding:11px 13px;
            background:var(--card); border:.5px solid var(--line); border-radius:8px; cursor:pointer; list-style:none; }}
  summary::-webkit-details-marker {{ display:none; }}
  .manifest {{ font-family:var(--mono); font-size:10.5px; line-height:1.7; color:var(--fg3);
              padding:28px 0 0; border-top:.5px solid var(--line); margin-top:28px; }}
</style></head><body>

<header>
  <div class="htop">
    <div class="hbrand"><h1>Roles</h1><span class="subtitle">{len(rows)} roles · {pri_count} priority</span></div>
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
    <button class="chip" data-f="fresh"    aria-pressed="false">This week</button>
    <button class="chip" data-f="priority" aria-pressed="false">Priority</button>
    <button class="chip" data-f="fit"      aria-pressed="false">Fit</button>
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
    <summary>Stretch bucket — 4+ yrs stated ({len(bucket_b)} roles)</summary>
    <div id="blist">
{b_cards}
    </div>
  </details>

  <div class="manifest">
    {len(rows)} roles from live ATS JSON · {len(bucket_a)} in-range ({pri_count} priority) · {len(bucket_b)} stretch.
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
