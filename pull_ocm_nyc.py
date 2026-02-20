#!/usr/bin/env python3
"""
Open Charge Map (OCM) -> NEW YORK STATE export (full coverage) with:
- Tiling to avoid the 500-result cap
- Filters OUT NJ/CT/etc by keeping only StateOrProvince == NY/New York
- Includes operator (charging company)
- Includes plug types + max kW summary

GitHub Actions:
- Repo Secret: OCM_API_KEY
- Workflow runs: python pull_ocm_nys.py

Output:
- ocm_nys_retail_locations.csv
"""

import os
import time
import requests
import pandas as pd

# =========================
# CONFIG
# =========================

API_KEY = os.getenv("OCM_API_KEY")
if not API_KEY:
    raise SystemExit("Missing OCM_API_KEY environment variable (set repo secret OCM_API_KEY).")

URL = "https://api.openchargemap.io/v3/poi"

# Approx New York State bounding box (covers Long Island + upstate)
# NOTE: bbox will include some spillover, which we remove via StateOrProvince filter.
NW_LAT, NW_LON = 45.0159, -79.7624
SE_LAT, SE_LON = 40.4961, -71.8562

# Increase tiles if you still see many tiles returning exactly MAXRESULTS_PER_TILE.
TILES_X = 12
TILES_Y = 12

MAXRESULTS_PER_TILE = 500
SLEEP_BETWEEN_CALLS_SEC = 0.25
TIMEOUT_SEC = 90

OUTPUT_CSV = "ocm_nys_retail_locations.csv"

CLIENT_NAME = "NY-Retail-Leaflet-Map"

HEADERS = {
    "X-API-Key": API_KEY,
    "User-Agent": CLIENT_NAME,
}

# =========================
# HELPERS
# =========================

def bbox_param(nw_lat: float, nw_lon: float, se_lat: float, se_lon: float) -> str:
    # OCM format: (lat,lng),(lat2,lng2) = (top-left),(bottom-right)
    return f"({nw_lat:.6f},{nw_lon:.6f}),({se_lat:.6f},{se_lon:.6f})"


def fetch_tile(nw_lat: float, nw_lon: float, se_lat: float, se_lon: float):
    params = {
        "boundingbox": bbox_param(nw_lat, nw_lon, se_lat, se_lon),
        "countrycode": "US",
        "maxresults": MAXRESULTS_PER_TILE,
        "output": "json",

        # CRITICAL: keep reference objects like OperatorInfo / ConnectionType / CurrentType
        "compact": False,
        "verbose": True,

        # Optional but recommended
        "client": CLIENT_NAME,
        "camelcase": False,
    }
    r = requests.get(URL, params=params, headers=HEADERS, timeout=TIMEOUT_SEC)
    r.raise_for_status()
    return r.json()


def is_new_york_state(addr: dict) -> bool:
    # Keep ONLY NY (removes NJ/CT/PA spillover from bbox)
    state = (addr.get("StateOrProvince") or "").strip().lower()
    return state in ("ny", "new york")


def extract_connection_summary(poi: dict):
    plugs = set()
    currents = set()
    levels = set()
    max_kw = 0.0
    has_dc_fast = False

    for c in poi.get("Connections") or []:
        plug = (c.get("ConnectionType") or {}).get("Title")
        if plug:
            plugs.add(plug)

        cur = (c.get("CurrentType") or {}).get("Title")
        if cur:
            currents.add(cur)
            if "dc" in cur.lower():
                has_dc_fast = True

        lvl = (c.get("Level") or {}).get("Title")
        if lvl:
            levels.add(lvl)
            if "level 3" in lvl.lower():
                has_dc_fast = True

        kw = c.get("PowerKW")
        try:
            if kw is not None:
                kwf = float(kw)
                if kwf > max_kw:
                    max_kw = kwf
        except (TypeError, ValueError):
            pass

    return (
        " | ".join(sorted(plugs)),
        " | ".join(sorted(currents)),
        " | ".join(sorted(levels)),
        max_kw,
        int(has_dc_fast),
    )


def poi_to_row(p: dict):
    addr = p.get("AddressInfo") or {}
    if not is_new_york_state(addr):
        return None

    operator_title = (p.get("OperatorInfo") or {}).get("Title") or ""
    usage_title = (p.get("UsageType") or {}).get("Title") or ""
    status_title = (p.get("StatusType") or {}).get("Title") or ""

    plug_types, current_types, charging_levels, max_kw, has_dc_fast = extract_connection_summary(p)

    return {
        "ocm_id": p.get("ID"),
        "uuid": p.get("UUID"),

        "name": addr.get("Title"),
        "operator": operator_title,

        "usage_type": usage_title,
        "status_type": status_title,
        "num_points": p.get("NumberOfPoints"),

        "lat": addr.get("Latitude"),
        "lon": addr.get("Longitude"),
        "address": addr.get("AddressLine1"),
        "town": addr.get("Town"),
        "state": addr.get("StateOrProvince"),
        "postcode": addr.get("Postcode"),

        "plug_types": plug_types,
        "current_types": current_types,
        "charging_levels": charging_levels,
        "max_kw": max_kw,
        "has_dc_fast": has_dc_fast,
    }


# =========================
# MAIN
# =========================

def main():
    lat_span = NW_LAT - SE_LAT
    lon_span = SE_LON - NW_LON

    rows = []
    seen_ids = set()
    truncation_tiles = 0
    kept_nonny = 0

    for y in range(TILES_Y):
        for x in range(TILES_X):
            tile_nw_lat = NW_LAT - (lat_span * y / TILES_Y)
            tile_se_lat = NW_LAT - (lat_span * (y + 1) / TILES_Y)

            tile_nw_lon = NW_LON + (lon_span * x / TILES_X)
            tile_se_lon = NW_LON + (lon_span * (x + 1) / TILES_X)

            pois = fetch_tile(tile_nw_lat, tile_nw_lon, tile_se_lat, tile_se_lon)

            if len(pois) >= MAXRESULTS_PER_TILE:
                truncation_tiles += 1

            added = 0
            filtered_out = 0

            for poi in pois:
                pid = poi.get("ID")
                if not pid or pid in seen_ids:
                    continue

                row = poi_to_row(poi)
                if row is None:
                    filtered_out += 1
                    continue

                seen_ids.add(pid)
                rows.append(row)
                added += 1

            kept_nonny += filtered_out

            print(
                f"Tile ({x+1}/{TILES_X},{y+1}/{TILES_Y}) "
                f"returned {len(pois)}; kept {added}; filtered_nonNY {filtered_out}; unique_kept {len(seen_ids)}"
            )
            time.sleep(SLEEP_BETWEEN_CALLS_SEC)

    df = pd.DataFrame(rows)
    if not df.empty:
        df = df.dropna(subset=["lat", "lon"]).drop_duplicates(subset=["ocm_id"])

    df.to_csv(OUTPUT_CSV, index=False)
    print(f"Saved {len(df)} NYS locations -> {OUTPUT_CSV}")
    print(f"Filtered out non-NY candidates (spillover): {kept_nonny}")

    if truncation_tiles:
        print(
            f"WARNING: {truncation_tiles} tiles returned >= {MAXRESULTS_PER_TILE} results.\n"
            f"Those tiles are likely capped. Increase TILES_X/TILES_Y (e.g., 14x14) to capture more."
        )


if __name__ == "__main__":
    main()
