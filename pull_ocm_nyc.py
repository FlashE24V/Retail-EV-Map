#!/usr/bin/env python3
"""
Open Charge Map -> NYS (or NYC) export with Operator include/exclude filtering.

- Pulls POIs from OpenChargeMap /v3/poi
- Supports:
  * NYC single bbox OR NYS tiling (recommended for full state)
  * INCLUDE_OPERATORS: keep only matching operators (optional)
  * EXCLUDE_OPERATORS: drop matching operators (optional)
  * Writes a Leaflet/PapaParse-friendly CSV with an `operator` column

GitHub Actions:
- Store your API key in repo secrets as: OCM_API_KEY
- The workflow should set env: OCM_API_KEY: ${{ secrets.OCM_API_KEY }}

Output:
- ocm_export.csv
"""

import os
import re
import time
from typing import Dict, List, Any, Tuple

import requests
import pandas as pd


# =========================
# CONFIG
# =========================

MODE = "NYS"  # "NYC" or "NYS"

# NYC bbox (5 boroughs)
NYC_BBOX = (40.9176, -74.2591, 40.4774, -73.7004)  # (nw_lat, nw_lon, se_lat, se_lon)

# Approx New York State bbox (covers Long Island + upstate; slight spillover is normal)
NYS_BBOX = (45.0159, -79.7624, 40.4961, -71.8562)  # (nw_lat, nw_lon, se_lat, se_lon)

# NYS tiling (increase tiles if you hit caps; smaller tiles = fewer results per request)
TILES_X = 8
TILES_Y = 8

MAXRESULTS_PER_REQUEST = 500
SLEEP_BETWEEN_CALLS_SEC = 0.25
REQUEST_TIMEOUT_SEC = 60

OUTPUT_CSV = "ocm_export.csv"

# Operator filters (case-insensitive substring match).
# If INCLUDE_OPERATORS is non-empty: ONLY operators matching one of these are kept.
INCLUDE_OPERATORS: List[str] = [
    # Examples:
    # "EVgo",
    # "Electrify America",
    # "Tesla",
]

# Always exclude these operators if they match.
EXCLUDE_OPERATORS: List[str] = [
    # Examples:
    # "ChargePoint",
    # "Blink",
]

DROP_UNKNOWN_OPERATOR = False  # True to drop rows with blank/missing operator name


# =========================
# INTERNALS
# =========================

API_KEY = os.getenv("OCM_API_KEY")
if not API_KEY:
    raise SystemExit("Missing OCM_API_KEY environment variable (set GitHub Secret OCM_API_KEY).")

BASE_URL = "https://api.openchargemap.io/v3/poi/"
HEADERS = {
    "X-API-Key": API_KEY,
    "User-Agent": "NY-Retail-Map/1.0 (contact: you@yourorg.com)",
}


def norm(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "").strip()).lower()


INCLUDE_N = [norm(x) for x in INCLUDE_OPERATORS if norm(x)]
EXCLUDE_N = [norm(x) for x in EXCLUDE_OPERATORS if norm(x)]


def bbox_param(nw_lat: float, nw_lon: float, se_lat: float, se_lon: float) -> str:
    # OCM expects: (lat,lng),(lat2,lng2) = (top-left),(bottom-right)
    return f"({nw_lat:.6f},{nw_lon:.6f}),({se_lat:.6f},{se_lon:.6f})"


def fetch_pois_for_bbox(nw_lat: float, nw_lon: float, se_lat: float, se_lon: float) -> List[Dict[str, Any]]:
    params = {
        "output": "json",
        "countrycode": "US",
        "boundingbox": bbox_param(nw_lat, nw_lon, se_lat, se_lon),
        "maxresults": MAXRESULTS_PER_REQUEST,
        "compact": True,
        "verbose": False,
    }
    r = requests.get(BASE_URL, params=params, headers=HEADERS, timeout=REQUEST_TIMEOUT_SEC)
    r.raise_for_status()
    return r.json()


def passes_operator_filters(op_title: str) -> bool:
    opn = norm(op_title)
    if DROP_UNKNOWN_OPERATOR and not opn:
        return False
    if EXCLUDE_N and any(x in opn for x in EXCLUDE_N):
        return False
    if INCLUDE_N and not any(x in opn for x in INCLUDE_N):
        return False
    return True


def poi_to_row(p: Dict[str, Any]) -> Dict[str, Any]:
    addr = p.get("AddressInfo") or {}
    op_title = (p.get("OperatorInfo") or {}).get("Title") or ""
    usage = (p.get("UsageType") or {}).get("Title") or ""
    status = (p.get("StatusType") or {}).get("Title") or ""

    return {
        "ocm_id": p.get("ID"),
        "name": addr.get("Title"),
        "operator": op_title,
        "usage_type": usage,
        "status_type": status,
        "num_points": p.get("NumberOfPoints"),
        "lat": addr.get("Latitude"),
        "lon": addr.get("Longitude"),
        "address": addr.get("AddressLine1"),
        "town": addr.get("Town"),
        "state": addr.get("StateOrProvince"),
        "postcode": addr.get("Postcode"),
    }


def export_nyc() -> pd.DataFrame:
    nw_lat, nw_lon, se_lat, se_lon = NYC_BBOX
    pois = fetch_pois_for_bbox(nw_lat, nw_lon, se_lat, se_lon)

    rows = []
    for p in pois:
        op = (p.get("OperatorInfo") or {}).get("Title") or ""
        if not passes_operator_filters(op):
            continue
        rows.append(poi_to_row(p))

    df = pd.DataFrame(rows)
    return df


def export_nys_tiled() -> pd.DataFrame:
    nw_lat, nw_lon, se_lat, se_lon = NYS_BBOX

    lat_span = nw_lat - se_lat
    lon_span = se_lon - nw_lon

    seen_ids = set()
    rows = []

    for y in range(TILES_Y):
        for x in range(TILES_X):
            tile_nw_lat = nw_lat - (lat_span * y / TILES_Y)
            tile_se_lat = nw_lat - (lat_span * (y + 1) / TILES_Y)

            tile_nw_lon = nw_lon + (lon_span * x / TILES_X)
            tile_se_lon = nw_lon + (lon_span * (x + 1) / TILES_X)

            pois = fetch_pois_for_bbox(tile_nw_lat, tile_nw_lon, tile_se_lat, tile_se_lon)

            for p in pois:
                pid = p.get("ID")
                if not pid or pid in seen_ids:
                    continue

                op = (p.get("OperatorInfo") or {}).get("Title") or ""
                if not passes_operator_filters(op):
                    continue

                seen_ids.add(pid)
                rows.append(poi_to_row(p))

            print(
                f"Tile ({x+1}/{TILES_X},{y+1}/{TILES_Y}) -> "
                f"{len(pois)} records; total unique kept: {len(seen_ids)}"
            )
            time.sleep(SLEEP_BETWEEN_CALLS_SEC)

    df = pd.DataFrame(rows)
    return df


def main():
    if MODE.upper() == "NYC":
        df = export_nyc()
    elif MODE.upper() == "NYS":
        df = export_nys_tiled()
    else:
        raise SystemExit('MODE must be "NYC" or "NYS"')

    # Clean
    if not df.empty:
        df = df.dropna(subset=["lat", "lon"]).drop_duplicates(subset=["ocm_id"])

    df.to_csv(OUTPUT_CSV, index=False)
    print(f"Saved {len(df)} rows -> {OUTPUT_CSV}")


if __name__ == "__main__":
    main()
