#!/usr/bin/env python3
"""
Bridge POV scanner — Mississippi River, Minneapolis.

For each bridge, samples points along the deck, queries Google Street View
metadata for the most recent car-SV pano, pulls 2 headings (upstream +
downstream), and tags kayak-targetable structure with Claude vision.

Why bridges:
- Recent Google car SV (1-3 yrs old) — fresh data
- Bridges concentrate fish (pilings, current breaks, shade)
- Overhead-ish POV reveals structure invisible from shore
- Kayak-accessible regardless of bank ownership

Setup:
    pip install requests anthropic
    export GOOGLE_MAPS_KEY=…
    export ANTHROPIC_API_KEY=…

Usage:
    python bridge_scanner.py                    # all bridges
    python bridge_scanner.py --only hennepin    # one bridge
    python bridge_scanner.py --min-score 5      # filter low-signal frames
"""

import argparse, base64, json, math, os, sys, time
from pathlib import Path
import requests
from anthropic import Anthropic

# ── Bridges. Each has a midspan point and an axis bearing (deck direction). ──
# Headings = perpendicular to deck axis = looking upstream / downstream at water.

BRIDGES = {
    "610":        {"mid": (-93.2810, 45.0680), "axis": 90,  "deck_m": 400},
    "camden":     {"mid": (-93.2768, 45.0166), "axis": 100, "deck_m": 220},
    "lowry":      {"mid": (-93.2754, 45.0086), "axis": 95,  "deck_m": 240},
    "plymouth":   {"mid": (-93.2680, 44.9920), "axis": 90,  "deck_m": 200},
    "broadway":   {"mid": (-93.2682, 44.9970), "axis": 95,  "deck_m": 220},
    "hennepin":   {"mid": (-93.2615, 44.9842), "axis": 100, "deck_m": 220},
    "3rd_ave":    {"mid": (-93.2585, 44.9818), "axis": 105, "deck_m": 220},
    "i35w":       {"mid": (-93.2466, 44.9696), "axis": 110, "deck_m": 350},
    "10th_ave":   {"mid": (-93.2510, 44.9755), "axis": 110, "deck_m": 230},
    "washington": {"mid": (-93.2473, 44.9728), "axis": 115, "deck_m": 220},
}

DECK_SAMPLES = 5      # points sampled along deck (more = more frames)
RADIUS_M     = 30     # tighter than corridor — bridges are well-mapped
IMG_SIZE     = "640x640"
FOV          = 90

PROMPT = """You are scouting a frame from a Google Street View bridge crossing on the Mississippi River in Minneapolis, looking up- or downstream. The user fishes from a kayak — bank access does not matter. Tag only what a kayaker would target.

Return ONLY valid JSON, no prose, no fences.

{
  "water_visible": true|false,
  "water_clarity": "clear"|"stained"|"muddy"|"unclear",
  "structure": [<any of:
    "bridge_piling","piling_eddy","current_seam","shadow_line",
    "rip_rap","boulder","laydown","weed_edge","gravel_bar",
    "wing_dam","point","cut_bank","slack_water","barge_terminal"
  >],
  "primary_target": "the single best feature in this frame, e.g. 'downstream eddy off east piling'",
  "current_strength": "slack"|"moderate"|"fast"|"unclear",
  "kayak_score": 0-10,
  "notes": "one short sentence — what would you cast and where"
}

Rules:
- water not visible → score 0, structure []
- be conservative; only tag clearly visible features
- kayak_score = how much you would want to fish this exact spot from a kayak"""


# ── Geo helpers ───────────────────────────────────────────────────────────────

def offset(lon, lat, bearing_deg, dist_m):
    """Move dist_m meters along bearing from (lon, lat). Flat-earth ok at this scale."""
    R = 6371000
    br = math.radians(bearing_deg)
    dlat = (dist_m * math.cos(br)) / R
    dlon = (dist_m * math.sin(br)) / (R * math.cos(math.radians(lat)))
    return (lon + math.degrees(dlon), lat + math.degrees(dlat))


def deck_samples(mid, axis_deg, deck_m, n):
    """Points along deck axis, centered on mid."""
    if n == 1:
        return [mid]
    step = deck_m / (n - 1)
    return [offset(mid[0], mid[1], axis_deg, -deck_m / 2 + i * step) for i in range(n)]


# ── Google Street View ────────────────────────────────────────────────────────

def find_pano(lon, lat, key, radius=RADIUS_M):
    r = requests.get(
        "https://maps.googleapis.com/maps/api/streetview/metadata",
        params={"location": f"{lat},{lon}", "radius": radius,
                "source": "outdoor", "key": key},
        timeout=15,
    )
    j = r.json()
    if j.get("status") != "OK":
        return None
    return {
        "pano_id": j["pano_id"],
        "lat": j["location"]["lat"],
        "lon": j["location"]["lng"],
        "date": j.get("date"),
    }


def pano_url(pano_id, heading, key):
    return (
        f"https://maps.googleapis.com/maps/api/streetview"
        f"?size={IMG_SIZE}&pano={pano_id}&heading={heading}&fov={FOV}&key={key}"
    )


def fetch_b64(url):
    r = requests.get(url, timeout=30)
    r.raise_for_status()
    return base64.standard_b64encode(r.content).decode()


# ── Claude ────────────────────────────────────────────────────────────────────

def analyze(client, b64):
    msg = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=400,
        messages=[{
            "role": "user",
            "content": [
                {"type": "image", "source": {"type": "base64",
                                             "media_type": "image/jpeg",
                                             "data": b64}},
                {"type": "text", "text": PROMPT},
            ],
        }],
    )
    text = msg.content[0].text.strip()
    # strip any accidental markdown fences
    if text.startswith("`"):
        text = text.split("`")[1]
    if text.lstrip().startswith("json"):
        text = text.lstrip()[4:]
    return json.loads(text.strip())


# ── Driver ────────────────────────────────────────────────────────────────────

def scan_bridge(name, spec, gkey, client, min_score):
    print(f"\n=== {name} ===")
    samples     = deck_samples(spec["mid"], spec["axis"], spec["deck_m"], DECK_SAMPLES)
    upstream_h  = (spec["axis"] - 90) % 360
    downstream_h = (spec["axis"] + 90) % 360

    panos = {}
    for lon, lat in samples:
        p = find_pano(lon, lat, gkey)
        if p and p["pano_id"] not in panos:
            panos[p["pano_id"]] = p
        time.sleep(0.05)
    print(f"  {len(panos)} unique panos along deck")

    features = []
    for pid, p in panos.items():
        for label, h in [("upstream", upstream_h), ("downstream", downstream_h)]:
            url = pano_url(pid, h, gkey)
            try:
                tags = analyze(client, fetch_b64(url))
            except Exception as e:
                print(f"  {pid[:8]} {label} FAIL: {e}")
                continue
            score = tags.get("kayak_score", 0) or 0
            print(f"  {pid[:8]} {label:10} score={score} struct={tags.get('structure')}")
            if score < min_score:
                continue
            tags.update({
                "bridge":    name,
                "pano_id":   pid,
                "heading":   h,
                "look":      label,
                "captured":  p["date"],
                "image_url": url,
            })
            features.append({
                "type": "Feature",
                "geometry":   {"type": "Point", "coordinates": [p["lon"], p["lat"]]},
                "properties": tags,
            })
            time.sleep(0.2)
    return features


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--only",      help="run a single bridge by name")
    ap.add_argument("--min-score", type=int, default=0)
    ap.add_argument("--out",       default="bridges.geojson")
    a = ap.parse_args()

    gkey = os.environ.get("GOOGLE_MAPS_KEY")
    akey = os.environ.get("ANTHROPIC_API_KEY")
    if not gkey or not akey:
        sys.exit("Missing GOOGLE_MAPS_KEY or ANTHROPIC_API_KEY")
    client = Anthropic(api_key=akey)

    if a.only and a.only not in BRIDGES:
        sys.exit(f"Unknown bridge. Options: {', '.join(BRIDGES)}")
    bridges = {a.only: BRIDGES[a.only]} if a.only else BRIDGES

    all_feats = []
    for name, spec in bridges.items():
        all_feats.extend(scan_bridge(name, spec, gkey, client, a.min_score))

    Path(a.out).write_text(json.dumps(
        {"type": "FeatureCollection", "features": all_feats}, indent=2
    ))
    print(f"\n[done] {len(all_feats)} features → {a.out}")


if __name__ == "__main__":
    main()
