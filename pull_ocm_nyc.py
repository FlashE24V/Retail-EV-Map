#!/usr/bin/env python3
import os
import re
import time
import requests
import pandas as pd

API_KEY = os.getenv("OCM_API_KEY")
if not API_KEY:
    raise SystemExit("Missing OCM_API_KEY env var (set GitHub Secret OCM_API_KEY).")

BASE_URL = "https://api.openchargemap.io/v3/poi/"
HEADERS = {
    "X-API-Key": API_KEY,
    "User-Agent": "NY-Retail-Map/1.0 (contact: you@yourorg.com)",
}

# ---- New York State bounding box (approx; includes small spillover) ----
# Top-left (NW):  45.0159, -79.7624
# Bottom-right(SE):40.4961, -71.8562
NYS_NW_LAT, NYS_NW_LON = 45.0159, -79.7624
NYS_SE_LAT, NYS_SE_LON = 40.4961, -71.8562

# ---- Tiling controls (increase tiles for more coverage) ----
TILES_X = 10
TILES_Y = 10

# IMPORTANT: this is the per-request cap you set.
# If a tile returns exactly this many, that tile likely has more results -> increase tiling.
MAXRESULTS = 500

SLEEP_SEC = 0.25
OUT_CSV = "ocm_nys.csv"

# ---- Operator filtering (optional) ----
# If INCLUDE_OPERATORS is non-empty: keep ONLY those matching.
INCLUDE_OPERATORS = [
    # "EVgo",
    # "Electrify America",
    # "Tesla",
]

# Always drop these if they match.
EXCLUDE_OPERATORS = [
    # "ChargePoint",
    # "Blink",
]

DROP_UNKNOWN_OPERATOR = False


def norm(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "").strip()).lower()


include_n = [norm(x) for x in INCLUDE_OPERATORS if norm(x)]
exclude_n = [norm(x) for x in EXCLUDE_OPERATORS if norm(x)]


def bbox_param(nw_lat: float, nw_lon: float, se_lat: float, se_lon: float) -> str:
    # OCM bounding box format: (lat,lng),(lat2,lng2) (top-left, bottom-right)
    return f"({nw_lat:.6f},{nw_lon:.6f}),({se_lat:.6f},{se_lon:.6f})"


def fetch_tile(nw_lat: float, nw_lon: float, se_lat: float, se_lon: float):
    params = {
        "output": "json",
        "countrycode": "US",
        "boundingbox": bbox_param(nw_lat, nw_lon, se_lat, se_lon),
        "maxresults": MAXRESULTS,

        # KEY FIXES:
        "compact": False,  # <-- ensures OperatorInfo and other objects can be populated
        "verbose": True,   # <-- include expanded objects (default true)
    }
    r = requests.get(BASE_URL, params=params, headers=HEADERS, timeout=90)
    r.raise_for_status()
    return r.json()


def passes_operator(op_title: str) -> bool:
    opn = norm(op_title)
    if DROP_UNKNOWN_OPERATOR and not opn:
        return False
    if exclude_n and any(x in opn for x in exclude_n):
        return False
    if include_n and not any(x in opn for x in include_n):
        return False
    return True


lat_span = NYS_NW_LAT - NYS_SE_LAT
lon_span = NYS_SE_LON - NYS_NW_LON

rows = []
seen_ids = set()
truncation_warnings = 0

for y in range(TILES_Y):
    for x in range(TILES_X):
        tile_nw_lat = NYS_NW_LAT - (lat_span * y / TILES_Y)
        tile_se_lat = NYS_NW_LAT - (lat_span * (y + 1) / TILES_Y)
        tile_nw_lon = NYS_NW_LON + (lon_span * x / TILES_X)
        tile_se_lon = NYS_NW_LON + (lon_span * (x + 1) / TILES_X)

        pois = fetch_tile(tile_nw_lat, tile_nw_lon, tile_se_lat, tile_se_lon)

        if len(pois) >= MAXRESULTS:
            truncation_warnings += 1

        for p in pois:
            pid = p.get("ID")
            if not pid or pid in seen_ids:
                continue

            addr = p.get("AddressInfo") or {}
            op_title = (p.get("OperatorInfo") or {}).get("Title") or ""

            if not passes_operator(op_title):
                continue

            seen_ids.add(pid)
            rows.append({
                "ocm_id": pid,
                "name": addr.get("Title"),
                "operator": op_title,  # <-- charging company/operator name
                "usage_type": (p.get("UsageType") or {}).get("Title"),
                "status_type": (p.get("StatusType") or {}).get("Title"),
                "num_points": p.get("NumberOfPoints"),
                "lat": addr.get("Latitude"),
                "lon": addr.get("Longitude"),
                "address": addr.get("AddressLine1"),
                "town": addr.get("Town"),
                "state": addr.get("StateOrProvince"),
                "postcode": addr.get("Postcode"),
            })

        print(f"Tile ({x+1}/{TILES_X},{y+1}/{TILES_Y}) -> {len(pois)} POIs | unique kept: {len(seen_ids)}")
        time.sleep(SLEEP_SEC)

df = pd.DataFrame(rows).dropna(subset=["lat", "lon"]).drop_duplicates(subset=["ocm_id"])
df.to_csv(OUT_CSV, index=False)

print(f"Saved {len(df)} NYS locations -> {OUT_CSV}")

if truncation_warnings:
    print(f"WARNING: {truncation_warnings} tiles returned >= MAXRESULTS ({MAXRESULTS}).")
    print("Those tiles are likely truncated. Increase TILES_X/TILES_Y (smaller tiles) to capture all locations.")
