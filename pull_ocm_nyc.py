#!/usr/bin/env python3
"""
Open Charge Map (OCM) -> NEW YORK STATE export (includes NYC) with:
- Tiling (avoids the 500-result cap)
- Filters OUT NJ/CT/etc by keeping only StateOrProvince that looks like NY
- Includes operator (charging company)
- Includes plug types + max kW summary
- Cleans dataset:
    * removes blank operator
    * removes Tesla operator
    * removes Private usage types (includes "Private - Restricted Access")

GitHub Actions:
- Repo Secret: OCM_API_KEY
- Workflow runs: python pull_ocm_nyc.py

Output:
- ocm_nyc_retail_locations.csv
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
# NOTE: bbox will include some spillover, which we remove via NY-only state filter below.
NW_LAT, NW_LON = 45.0159, -79.7624
SE_LAT, SE_LON = 40.4961, -71.8562

# Tiling: increase if you still see tiles returning exactly MAXRESULTS_PER_TILE
TILES_X = 12
TILES_Y = 12

MAXRESULTS_PER_TILE = 500
SLEEP_BETWEEN_CALLS_SEC = 0.25
TIMEOUT_SEC = 90

# IMPORTANT: must match what your workflow commits + what your map loads
OUTPUT_CSV = "ocm_nyc_retail_locations.csv"

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

        # IMPORTANT: keep reference objects like OperatorInfo / ConnectionType / CurrentType
        # If compact=True you often won't see operator names.
        "compact": False,
        "verbose": True,

        # Recommended to identify your client
        "client": CLIENT_NAME,

        # Keep default snake_case property names
        "camelcase": False,
    }
    r = requests.get(URL, params=params, headers=HEADERS, timeout=TIMEOUT_SEC)
    r.raise_for_status()
    return r.json()


def is_new_york_state(addr: dict) -> bool:
    """
    Keeps NY only. Catches common variants:
      - "NY"
      - "New York"
      - "New York State"
      - anything starting with "new york"
    """
    state = (addr.get("StateOrProvince") or "").strip().lower()
    return state in ("ny", "new york") or state.startswith("new york")


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
    filtered_nonny_total = 0

    for y in range(TILES_Y):
        for x in range(TILES_X):
            tile_nw_lat = NW_LAT - (lat_span * y / TILES_Y)
            tile_se_lat = NW_LAT - (lat_span * (y + 1) / TILES_Y)

            tile_nw_lon = NW_LON + (lon_span * x / TILES_X)
            tile_se_lon = NW_LON + (lon_span * (x + 1) / TILES_X)

            pois = fetch_tile(tile_nw_lat, tile_nw_lon, tile_se_lat, tile_se_lon)

            if len(pois) >= MAXRESULTS_PER_TILE:
                truncation_tiles += 1

            kept = 0
            filtered_nonny = 0

            for poi in pois:
                pid = poi.get("ID")
                if not pid or pid in seen_ids:
                    continue

                row = poi_to_row(poi)
                if row is None:
                    filtered_nonny += 1
                    continue

                seen_ids.add(pid)
                rows.append(row)
                kept += 1

            filtered_nonny_total += filtered_nonny

            print(
                f"Tile ({x+1}/{TILES_X},{y+1}/{TILES_Y}) "
                f"returned {len(pois)}; kept {kept}; filtered_nonNY {filtered_nonny}; unique_kept {len(seen_ids)}"
            )
            time.sleep(SLEEP_BETWEEN_CALLS_SEC)

    df = pd.DataFrame(rows)

    # Basic cleanup
    if not df.empty:
        df = df.dropna(subset=["lat", "lon"]).drop_duplicates(subset=["ocm_id"])

        # -----------------------------
        # CLEANING FILTERS (your asks)
        # -----------------------------
        df["operator"] = df["operator"].fillna("").astype(str).str.strip()
        df["usage_type"] = df["usage_type"].fillna("").astype(str).str.strip()

        # 1) remove blank operator
        df = df[df["operator"] != ""]

        # 2) remove Tesla operator
        df = df[~df["operator"].str.lower().str.contains("tesla", na=False)]

        # 3) remove Private + Private - Restricted Access (and any other private variants)
        df = df[~df["usage_type"].str.lower().str.contains("private", na=False)]

    # DEBUG: show what states remain (should be NY only)
    if not df.empty and "state" in df.columns:
        s = df["state"].fillna("").astype(str).str.strip()
        print("\nTop states in OUTPUT (should be NY variants only):")
        print(s.value_counts().head(20).to_string())

    df.to_csv(OUTPUT_CSV, index=False)
    print(f"\nSaved {len(df)} NY retail locations -> {OUTPUT_CSV}")
    print(f"Filtered out non-NY candidates (spillover): {filtered_nonny_total}")

    if truncation_tiles:
        print(
            f"\nWARNING: {truncation_tiles} tiles returned >= {MAXRESULTS_PER_TILE} results.\n"
            f"Those tiles are likely capped. Increase TILES_X/TILES_Y (e.g., 14x14) to capture more."
        )


if __name__ == "__main__":
    main()
