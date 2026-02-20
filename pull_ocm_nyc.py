#!/usr/bin/env python3
"""
OpenChargeMap NYC export (tile + paginate + dedupe)

Fixes missing dense areas (e.g., Lower Manhattan) by:
- querying a NYC bounding box in tiles
- paginating each tile (offset/maxresults)
- deduping POIs by OCM ID

Output: ocm_nyc_retail_locations.csv
"""

import os
import time
import math
import csv
from typing import Dict, Any, List, Tuple, Optional
import requests

OCM_API_KEY = os.getenv("OCM_API_KEY")  # <-- set this
if not OCM_API_KEY:
    raise SystemExit("Missing OCM_API_KEY env var. Set it first (PowerShell: $env:OCM_API_KEY='...').")

BASE_URL = "https://api.openchargemap.io/v3/poi/"

# NYC bounds (with buffer)
NYC_SW_LAT, NYC_SW_LON = 40.4774, -74.2591
NYC_NE_LAT, NYC_NE_LON = 40.9176, -73.7004

# Tile size in degrees:
# Smaller tiles = more requests but less risk of hitting max results per tile.
# For NYC density, 0.03 is a good starting point.
TILE_DEG = 0.03

# Pagination size (OCM supports maxresults; keep reasonable)
MAX_RESULTS = 200
REQUEST_PAUSE_SEC = 0.25  # be polite to API

# Optional: if you ONLY want operational (public-ish) you can filter later.
# Best practice: pull everything first; filter in your Leaflet or post-processing.


def frange(start: float, stop: float, step: float) -> List[float]:
    vals = []
    x = start
    # avoid float drift
    n = int(math.ceil((stop - start) / step))
    for i in range(n):
        vals.append(start + i * step)
    # ensure stop included by tiling logic externally
    return vals


def safe_get(d: Dict[str, Any], path: List[str], default=None):
    cur = d
    for p in path:
        if not isinstance(cur, dict) or p not in cur:
            return default
        cur = cur[p]
    return cur


def norm_text(x) -> str:
    if x is None:
        return ""
    return str(x).strip()


def join_unique(items: List[str]) -> str:
    seen = set()
    out = []
    for it in items:
        t = norm_text(it)
        if not t:
            continue
        if t not in seen:
            seen.add(t)
            out.append(t)
    return ", ".join(out)


def classify_connections(conns: List[Dict[str, Any]]) -> Tuple[str, str, str, str, int, bool]:
    """
    Returns:
      plug_types, current_types, charging_levels, max_kw, num_points, has_dc_fast
    """
    plug_types = []
    current_types = []
    levels = []
    max_kw = 0.0
    num_points = 0
    has_dc_fast = False

    for c in conns or []:
        qty = c.get("Quantity") or 1
        try:
            qty = int(qty)
        except Exception:
            qty = 1
        num_points += max(qty, 0)

        # PowerKW often exists; sometimes "PowerKW" missing
        pkw = c.get("PowerKW")
        try:
            if pkw is not None:
                max_kw = max(max_kw, float(pkw))
        except Exception:
            pass

        # Plug type
        plug_types.append(safe_get(c, ["ConnectionType", "Title"], ""))

        # Current type
        current_types.append(safe_get(c, ["CurrentType", "Title"], ""))

        # Level
        lvl_title = safe_get(c, ["Level", "Title"], "")
        if lvl_title:
            levels.append(lvl_title)
            if "level 3" in lvl_title.lower():
                has_dc_fast = True

        # Another heuristic: if current is DC and power >= 50, it's DC fast
        ct = safe_get(c, ["CurrentType", "Title"], "")
        try:
            if ct and "dc" in ct.lower() and max_kw >= 50:
                has_dc_fast = True
        except Exception:
            pass

    return (
        join_unique(plug_types),
        join_unique(current_types),
        join_unique(levels),
        (f"{max_kw:g}" if max_kw > 0 else ""),
        num_points if num_points > 0 else 0,
        bool(has_dc_fast),
    )


def fetch_tile(sw_lat: float, sw_lon: float, ne_lat: float, ne_lon: float) -> List[Dict[str, Any]]:
    """
    Fetch all POIs in a bbox tile using pagination.
    """
    results = []
    offset = 0

    while True:
        params = {
            "key": OCM_API_KEY,
            "output": "json",
            "boundingbox": f"{sw_lat},{sw_lon},{ne_lat},{ne_lon}",
            "maxresults": str(MAX_RESULTS),
            "offset": str(offset),
            # You can add filters here if you *really* want, but avoid until after:
            # "includecomments": "true",
        }

        # Retry logic
        for attempt in range(4):
            try:
                resp = requests.get(BASE_URL, params=params, timeout=60)
                if resp.status_code == 429:
                    # rate limited: wait more and retry
                    time.sleep(2 + attempt * 2)
                    continue
                resp.raise_for_status()
                batch = resp.json()
                if not isinstance(batch, list):
                    batch = []
                break
            except Exception as e:
                if attempt == 3:
                    print(f"[WARN] tile fetch failed after retries ({sw_lat},{sw_lon} to {ne_lat},{ne_lon}) offset={offset}: {e}")
                    batch = []
                    break
                time.sleep(1.5 * (attempt + 1))
        else:
            batch = []

        if not batch:
            break

        results.extend(batch)

        # If we got less than maxresults, this tile is done.
        if len(batch) < MAX_RESULTS:
            break

        offset += MAX_RESULTS
        time.sleep(REQUEST_PAUSE_SEC)

    return results


def iter_tiles() -> List[Tuple[float, float, float, float]]:
    tiles = []
    lat_steps = frange(NYC_SW_LAT, NYC_NE_LAT, TILE_DEG)
    lon_steps = frange(NYC_SW_LON, NYC_NE_LON, TILE_DEG)

    for lat in lat_steps:
        for lon in lon_steps:
            sw_lat = lat
            sw_lon = lon
            ne_lat = min(lat + TILE_DEG, NYC_NE_LAT)
            ne_lon = min(lon + TILE_DEG, NYC_NE_LON)
            tiles.append((sw_lat, sw_lon, ne_lat, ne_lon))
    return tiles


def flatten_poi(poi: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """
    Convert OCM POI JSON into one CSV row.
    """
    poi_id = poi.get("ID")
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

    row = {
        "ocm_id": poi_id,
        "lat": lat,
        "lon": lon,
        "name": norm_text(poi.get("AddressInfo", {}).get("Title") or poi.get("AddressInfo", {}).get("AddressLine1") or poi.get("OperatorInfo", {}).get("Title") or poi.get("ID")),
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
    return row


def main():
    tiles = iter_tiles()
    print(f"Tiles: {len(tiles)} (tile size {TILE_DEG}°), page size {MAX_RESULTS}")

    by_id: Dict[int, Dict[str, Any]] = {}
    total_raw = 0

    for i, (sw_lat, sw_lon, ne_lat, ne_lon) in enumerate(tiles, start=1):
        print(f"[{i:03d}/{len(tiles)}] Fetch tile: {sw_lat:.4f},{sw_lon:.4f} -> {ne_lat:.4f},{ne_lon:.4f}")
        batch = fetch_tile(sw_lat, sw_lon, ne_lat, ne_lon)
        total_raw += len(batch)

        for poi in batch:
            pid = poi.get("ID")
            if pid is None:
                continue
            # Deduplicate by ID (keep first; you could also merge)
            if pid not in by_id:
                row = flatten_poi(poi)
                if row:
                    by_id[pid] = row

        time.sleep(REQUEST_PAUSE_SEC)

    rows = list(by_id.values())
    rows.sort(key=lambda r: (r["lat"], r["lon"]))

    out_file = "ocm_nyc_retail_locations.csv"
    fieldnames = [
        "ocm_id",
        "lat", "lon",
        "name",
        "operator",
        "plug_types",
        "current_types",
        "charging_levels",
        "max_kw",
        "num_points",
        "status_type",
        "usage_type",
        "usage_cost",
        "access_comments",
        "general_comments",
        "address",
        "town",
        "state",
        "postcode",
        "has_dc_fast",
    ]

    with open(out_file, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames)
        w.writeheader()
        for r in rows:
            w.writerow(r)

    print("\nDone.")
    print(f"Raw POIs fetched (with duplicates across tiles/pages): {total_raw}")
    print(f"Unique POIs written (deduped by ID): {len(rows)}")
    print(f"Wrote: {out_file}")

    # Quick sanity: count Lower Manhattan points
    lm = [r for r in rows if (40.700 <= r["lat"] <= 40.735 and -74.020 <= r["lon"] <= -73.970)]
    print(f"Lower Manhattan (rough bbox) count: {len(lm)}")


if __name__ == "__main__":
    main()
