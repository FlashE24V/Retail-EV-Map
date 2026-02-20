#!/usr/bin/env python3
"""
OpenChargeMap NYC export (tile + paginate + dedupe) with strong logging.

If output is blank, this script will show you WHY (401/403/429/etc).
"""

import os
import time
import math
import csv
from typing import Dict, Any, List, Tuple, Optional

import requests

OCM_API_KEY = os.getenv("OCM_API_KEY")
if not OCM_API_KEY:
    raise SystemExit(
        "Missing OCM_API_KEY env var.\n"
        "PowerShell:  $env:OCM_API_KEY='YOUR_KEY'\n"
        "CMD:         set OCM_API_KEY=YOUR_KEY\n"
        "Mac/Linux:   export OCM_API_KEY='YOUR_KEY'\n"
    )

BASE_URL = "https://api.openchargemap.io/v3/poi/"

# NYC bounds (with buffer)
NYC_SW_LAT, NYC_SW_LON = 40.4774, -74.2591
NYC_NE_LAT, NYC_NE_LON = 40.9176, -73.7004

TILE_DEG = 0.03
MAX_RESULTS = 200
REQUEST_PAUSE_SEC = 0.2

SESSION = requests.Session()
SESSION.headers.update({
    "User-Agent": "NYC-Fleet-OCM-Exporter/1.0 (contact: internal)",
    "X-API-Key": OCM_API_KEY,  # <-- important
    "Accept": "application/json",
})

def frange(start: float, stop: float, step: float) -> List[float]:
    n = int(math.ceil((stop - start) / step))
    return [start + i * step for i in range(n)]

def norm_text(x) -> str:
    return "" if x is None else str(x).strip()

def safe_get(d: Dict[str, Any], path: List[str], default=""):
    cur: Any = d
    for p in path:
        if not isinstance(cur, dict) or p not in cur:
            return default
        cur = cur[p]
    return cur

def join_unique(items: List[str]) -> str:
    seen = set()
    out = []
    for it in items:
        t = norm_text(it)
        if t and t not in seen:
            seen.add(t)
            out.append(t)
    return ", ".join(out)

def classify_connections(conns: List[Dict[str, Any]]) -> Tuple[str, str, str, str, int, bool]:
    plug_types, current_types, levels = [], [], []
    max_kw = 0.0
    num_points = 0
    has_dc_fast = False

    for c in conns or []:
        qty = c.get("Quantity", 1)
        try:
            qty = int(qty) if qty is not None else 1
        except Exception:
            qty = 1
        num_points += max(qty, 0)

        pkw = c.get("PowerKW")
        try:
            if pkw is not None:
                max_kw = max(max_kw, float(pkw))
        except Exception:
            pass

        plug_types.append(safe_get(c, ["ConnectionType", "Title"], ""))
        current_types.append(safe_get(c, ["CurrentType", "Title"], ""))

        lvl_title = safe_get(c, ["Level", "Title"], "")
        if lvl_title:
            levels.append(lvl_title)
            if "level 3" in lvl_title.lower():
                has_dc_fast = True

        ct = safe_get(c, ["CurrentType", "Title"], "")
        if ct and "dc" in ct.lower() and max_kw >= 50:
            has_dc_fast = True

    return (
        join_unique(plug_types),
        join_unique(current_types),
        join_unique(levels),
        (f"{max_kw:g}" if max_kw > 0 else ""),
        num_points if num_points > 0 else 0,
        bool(has_dc_fast),
    )

def fetch_page(boundingbox: str, offset: int) -> List[Dict[str, Any]]:
    params = {
        "output": "json",
        "boundingbox": boundingbox,
        "maxresults": str(MAX_RESULTS),
        "offset": str(offset),
        # Also pass key as query param (some setups require it)
        "key": OCM_API_KEY,
    }

    resp = SESSION.get(BASE_URL, params=params, timeout=60)

    if resp.status_code != 200:
        print(f"[HTTP {resp.status_code}] {resp.url}")
        # Print a small snippet so we can see the error message
        txt = resp.text
        print(txt[:400] + ("..." if len(txt) > 400 else ""))
        return []

    try:
        data = resp.json()
    except Exception as e:
        print(f"[BAD JSON] {resp.url} :: {e}")
        print(resp.text[:400])
        return []

    if not isinstance(data, list):
        print(f"[UNEXPECTED RESPONSE TYPE] {resp.url} :: {type(data)}")
        return []

    return data

def fetch_tile(sw_lat: float, sw_lon: float, ne_lat: float, ne_lon: float) -> List[Dict[str, Any]]:
    bbox = f"{sw_lat},{sw_lon},{ne_lat},{ne_lon}"
    out: List[Dict[str, Any]] = []
    offset = 0

    while True:
        batch = fetch_page(bbox, offset)
        if not batch:
            break

        out.extend(batch)
        if len(batch) < MAX_RESULTS:
            break

        offset += MAX_RESULTS
        time.sleep(REQUEST_PAUSE_SEC)

    return out

def iter_tiles() -> List[Tuple[float, float, float, float]]:
    tiles = []
    for lat in frange(NYC_SW_LAT, NYC_NE_LAT, TILE_DEG):
        for lon in frange(NYC_SW_LON, NYC_NE_LON, TILE_DEG):
            sw_lat = lat
            sw_lon = lon
            ne_lat = min(lat + TILE_DEG, NYC_NE_LAT)
            ne_lon = min(lon + TILE_DEG, NYC_NE_LON)
            tiles.append((sw_lat, sw_lon, ne_lat, ne_lon))
    return tiles

def flatten_poi(poi: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    pid = poi.get("ID")
    addr = poi.get("AddressInfo") or {}
    conns = poi.get("Connections") or []

    lat = addr.get("Latitude")
    lon = addr.get("Longitude")
    try:
        lat = float(lat)
        lon = float(lon)
    except Exception:
        return None

    plug_types, current_types, charging_levels, max_kw, num_points, has_dc_fast = classify_connections(conns)

    return {
        "ocm_id": pid,
        "lat": lat,
        "lon": lon,
        "name": norm_text(addr.get("Title") or addr.get("AddressLine1") or pid),
        "operator": norm_text(safe_get(poi, ["OperatorInfo", "Title"], "")),
        "plug_types": plug_types,
        "current_types": current_types,
        "charging_levels": charging_levels,
        "max_kw": max_kw,
        "num_points": num_points,
        "status_type": norm_text(safe_get(poi, ["StatusType", "Title"], "")),
        "usage_type": norm_text(safe_get(poi, ["UsageType", "Title"], "")),
        "usage_cost": norm_text(poi.get("UsageCost")),
        "access_comments": norm_text(addr.get("AccessComments")),
        "general_comments": norm_text(poi.get("GeneralComments")),
        "address": norm_text(addr.get("AddressLine1")),
        "town": norm_text(addr.get("Town")),
        "state": norm_text(addr.get("StateOrProvince")),
        "postcode": norm_text(addr.get("Postcode")),
        "has_dc_fast": True if has_dc_fast else False,
    }

def main():
    # ---- Smoke test on one tile in Lower Manhattan area ----
    test_sw = (40.7000, -74.0200)
    test_ne = (40.7350, -73.9700)
    print("Smoke test (Lower Manhattan bbox)...")
    smoke = fetch_tile(test_sw[0], test_sw[1], test_ne[0], test_ne[1])
    print(f"Smoke test results: {len(smoke)} POIs")
    if len(smoke) == 0:
        print("STOP: API returned 0 POIs for a dense NYC bbox. This is almost certainly auth/rate-limit/rejection.")
        print("Check the HTTP logs above (401/403/429). Fix that first, then rerun.")
        # Still continue, but you'll likely get blank output.
        # return

    tiles = iter_tiles()
    print(f"\nTiles: {len(tiles)} (tile size {TILE_DEG}°), page size {MAX_RESULTS}")

    by_id: Dict[int, Dict[str, Any]] = {}
    total_raw = 0

    for i, (sw_lat, sw_lon, ne_lat, ne_lon) in enumerate(tiles, start=1):
        batch = fetch_tile(sw_lat, sw_lon, ne_lat, ne_lon)
        total_raw += len(batch)

        for poi in batch:
            pid = poi.get("ID")
            if pid is None:
                continue
            if pid not in by_id:
                row = flatten_poi(poi)
                if row:
                    by_id[pid] = row

        if i % 25 == 0:
            print(f"[{i}/{len(tiles)}] raw={total_raw} unique={len(by_id)}")
        time.sleep(REQUEST_PAUSE_SEC)

    rows = list(by_id.values())
    rows.sort(key=lambda r: (r["lat"], r["lon"]))

    out_file = "ocm_nyc_retail_locations.csv"
    fieldnames = [
        "ocm_id","lat","lon","name","operator",
        "plug_types","current_types","charging_levels","max_kw","num_points",
        "status_type","usage_type","usage_cost","access_comments","general_comments",
        "address","town","state","postcode","has_dc_fast"
    ]

    with open(out_file, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames)
        w.writeheader()
        for r in rows:
            w.writerow(r)

    print("\nDone.")
    print(f"Raw fetched (incl duplicates): {total_raw}")
    print(f"Unique written: {len(rows)}")
    print(f"Wrote: {out_file}")

    lm = [r for r in rows if (40.700 <= r["lat"] <= 40.735 and -74.020 <= r["lon"] <= -73.970)]
    print(f"Lower Manhattan rough bbox count in output: {len(lm)}")

if __name__ == "__main__":
    main()
