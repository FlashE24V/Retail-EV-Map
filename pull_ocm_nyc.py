#!/usr/bin/env python3
"""
Open Charge Map (OCM) -> NYC Retail Export (full coverage) with Operator + Plug Types.

Why this version:
- Avoids the "500 results only" problem by TILING the NYC bounding box and deduping by OCM ID.
- Ensures Operator/charging company shows up by using compact=false and verbose=true.
- Extracts plug types + power info from Connections[].
- Outputs a Leaflet/PapaParse-friendly CSV you can filter in the UI later.

GitHub Actions:
- Repo Secret: OCM_API_KEY
- Workflow env: OCM_API_KEY: ${{ secrets.OCM_API_KEY }}

Output:
- ocm_nyc_retail_locations.csv
"""

import os
import time
import requests
import pandas as pd


# =========================
# CONFIG (adjust as needed)
# =========================

API_KEY = os.getenv("OCM_API_KEY")
if not API_KEY:
    raise SystemExit("Missing OCM_API_KEY environment variable (set repo secret OCM_API_KEY).")

URL = "https://api.openchargemap.io/v3/poi"

# NYC bounding box (covers all 5 boroughs)
# Top-left (NW):  40.9176, -74.2591
# Bottom-right(SE):40.4774, -73.7004
NW_LAT, NW_LON = 40.9176, -74.2591
SE_LAT, SE_LON = 40.4774, -73.7004

# Tiling: increase if you still see tiles hitting MAXRESULTS_PER_TILE
TILES_X = 8
TILES_Y = 8

# API per-request cap you set; if a tile returns exactly this many, it's likely truncated.
MAXRESULTS_PER_TILE = 500

# Request pacing
SLEEP_BETWEEN_CALLS_SEC = 0.25
TIMEOUT_SEC = 90

# Output file (this matches what you already uploaded/used)
OUTPUT_CSV = "ocm_nyc_retail_locations.csv"

# Optional but recommended: identify your client
CLIENT_NAME = "NYC-Retail-Leaflet-Map"

# Optional: smaller payload by removing null items.
# Leave True unless you specifically need nulls for debugging.
VERBOSE = True

HEADERS = {
    "X-API-Key": API_KEY,
    "User-Agent": CLIENT_NAME,
}


# =========================
# HELPERS
# =========================

def bbox_param(nw_lat: float, nw_lon: float, se_lat: float, se_lon: float) -> str:
    # OCM expects: (lat,lng),(lat2,lng2) = (top-left),(bottom-right)
    return f"({nw_lat:.6f},{nw_lon:.6f}),({se_lat:.6f},{se_lon:.6f})"


def fetch_tile(nw_lat: float, nw_lon: float, se_lat: float, se_lon: float):
    params = {
        "boundingbox": bbox_param(nw_lat, nw_lon, se_lat, se_lon),
        "countrycode": "US",
        "maxresults": MAXRESULTS_PER_TILE,
        "output": "json",

        # IMPORTANT: keep reference objects (OperatorInfo, ConnectionType, etc.)
        # If you set compact=True, those become IDs and your "operator" will look missing.
        "compact": False,

        # Default is true; keep true so OperatorInfo/UsageType/etc are present and nulls removed.
        "verbose": VERBOSE,

        # Optional but recommended to identify client
        "client": CLIENT_NAME,

        # Optional: keep snake_case keys (default). Set camelcase=True if you prefer camelCase.
        "camelcase": False,
    }

    r = requests.get(URL, params=params, headers=HEADERS, timeout=TIMEOUT_SEC)
    r.raise_for_status()
    return r.json()


def extract_connection_summary(poi: dict):
    """
    Returns:
      plug_types_str: e.g. "CCS Type 1 | CHAdeMO | J1772"
      current_types_str: e.g. "AC (Single-Phase) | DC"
      max_kw: max PowerKW found (0 if unknown)
      level_titles_str: e.g. "Level 2 : Medium (Over 2kW) | Level 3 : High (Over 40kW)"
      has_dc_fast: True if any connection is DC or Level 3
    """
    plugs = set()
    currents = set()
    levels = set()
    max_kw = 0.0
    has_dc_fast = False

    for c in poi.get("Connections") or []:
        # Plug type name
        plug = (c.get("ConnectionType") or {}).get("Title")
        if plug:
            plugs.add(plug)

        # Current type (AC/DC)
        cur = (c.get("CurrentType") or {}).get("Title")
        if cur:
            currents.add(cur)
            if "dc" in cur.lower():
                has_dc_fast = True

        # Level title (computed category)
        lvl = (c.get("Level") or {}).get("Title")
        if lvl:
            levels.add(lvl)
            if "level 3" in lvl.lower():
                has_dc_fast = True

        # Power
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
        max_kw,
        " | ".join(sorted(levels)),
        has_dc_fast,
    )


def poi_to_row(p: dict) -> dict:
    addr = p.get("AddressInfo") or {}

    operator_title = (p.get("OperatorInfo") or {}).get("Title") or ""
    usage_title = (p.get("UsageType") or {}).get("Title") or ""
    status_title = (p.get("StatusType") or {}).get("Title") or ""

    plug_types, current_types, max_kw, level_titles, has_dc_fast = extract_connection_summary(p)

    return {
        # Core identifiers
        "ocm_id": p.get("ID"),
        "uuid": p.get("UUID"),

        # Site name & ownership/network
        "name": addr.get("Title"),
        "operator": operator_title,

        # Access / general site classification
        "usage_type": usage_title,
        "status_type": status_title,
        "num_points": p.get("NumberOfPoints"),

        # Location
        "lat": addr.get("Latitude"),
        "lon": addr.get("Longitude"),
        "address": addr.get("AddressLine1"),
        "town": addr.get("Town"),
        "state": addr.get("StateOrProvince"),
        "postcode": addr.get("Postcode"),

        # Plug / power summary (for filtering & icons later)
        "plug_types": plug_types,
        "current_types": current_types,
        "charging_levels": level_titles,
        "max_kw": max_kw,
        "has_dc_fast": int(has_dc_fast),  # 1/0 for easy filtering
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

    for y in range(TILES_Y):
        for x in range(TILES_X):
            tile_nw_lat = NW_LAT - (lat_span * y / TILES_Y)
            tile_se_lat = NW_LAT - (lat_span * (y + 1) / TILES_Y)

            tile_nw_lon = NW_LON + (lon_span * x / TILES_X)
            tile_se_lon = NW_LON + (lon_span * (x + 1) / TILES_X)

            pois = fetch_tile(tile_nw_lat, tile_nw_lon, tile_se_lat, tile_se_lon)

            # If a tile hits the cap, it's likely you have MORE in that region.
            if len(pois) >= MAXRESULTS_PER_TILE:
                truncation_tiles += 1

            added = 0
            for poi in pois:
                pid = poi.get("ID")
                if not pid or pid in seen_ids:
                    continue
                seen_ids.add(pid)
                rows.append(poi_to_row(poi))
                added += 1

            print(
                f"Tile ({x+1}/{TILES_X},{y+1}/{TILES_Y}) "
                f"returned {len(pois)} POIs; added {added}; unique total {len(seen_ids)}"
            )

            time.sleep(SLEEP_BETWEEN_CALLS_SEC)

    df = pd.DataFrame(rows)

    # Basic cleanup
    if not df.empty:
        df = df.dropna(subset=["lat", "lon"]).drop_duplicates(subset=["ocm_id"])

    df.to_csv(OUTPUT_CSV, index=False)
    print(f"Saved {len(df)} rows -> {OUTPUT_CSV}")

    if truncation_tiles:
        print(
            f"WARNING: {truncation_tiles} tiles returned >= {MAXRESULTS_PER_TILE} results.\n"
            f"Those tiles are likely capped. Increase TILES_X/TILES_Y (e.g., 10x10 or 12x12)\n"
            f"to capture more locations in dense areas."
        )


if __name__ == "__main__":
    main()
