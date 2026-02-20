#!/usr/bin/env python3
"""
OCM NYC CSV generator (tile + recursive split to avoid 500-result cap)

Why needed:
- OpenChargeMap limits geo searches to max 500 results.
- One big NYC search will be truncated (missing chargers).
- This script recursively splits the NYC area until each query returns < 500.

Output:
- ocm_nyc_retail_locations.csv  (fields aligned to your Leaflet map)
"""

import csv
import math
import time
import requests
from typing import Dict, List, Tuple, Any, Set

# =========================
# CONFIG
# =========================

API_KEY = "PUT_YOUR_OCM_API_KEY_HERE"  # or set to "" if your key isn't required
OUT_CSV = "ocm_nyc_retail_locations.csv"

# NYC bounding box (safe)
NYC_BBOX = {
    "south": 40.4774,
    "north": 40.9176,
    "west": -74.2591,
    "east": -73.7004,
}

# If a tile still returns 500, we split it. Stop splitting below these sizes:
MIN_LAT_SPAN = 0.01   # ~1.1 km
MIN_LON_SPAN = 0.01   # ~0.8 km in NYC lat

# Request tuning
MAXRESULTS = 500
PAUSE_S = 0.25  # politeness delay

# Optional: filter to "public-ish" usage types later if you want
# For now, we do NOT filter, because filtering is how stations go missing.

# =========================
# GEO HELPERS
# =========================

def haversine_km(lat1, lon1, lat2, lon2) -> float:
    R = 6371.0088
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlmb = math.radians(lon2 - lon1)
    a = math.sin(dphi/2)**2 + math.cos(phi1)*math.cos(phi2)*math.sin(dlmb/2)**2
    return 2 * R * math.asin(math.sqrt(a))

def bbox_center(b: Dict[str, float]) -> Tuple[float, float]:
    return ((b["south"] + b["north"]) / 2.0, (b["west"] + b["east"]) / 2.0)

def bbox_spans(b: Dict[str, float]) -> Tuple[float, float]:
    return (b["north"] - b["south"], b["east"] - b["west"])

def split_bbox_4(b: Dict[str, float]) -> List[Dict[str, float]]:
    mid_lat = (b["south"] + b["north"]) / 2.0
    mid_lon = (b["west"] + b["east"]) / 2.0
    return [
        {"south": b["south"], "north": mid_lat, "west": b["west"], "east": mid_lon},  # SW
        {"south": b["south"], "north": mid_lat, "west": mid_lon, "east": b["east"]},  # SE
        {"south": mid_lat, "north": b["north"], "west": b["west"], "east": mid_lon},  # NW
        {"south": mid_lat, "north": b["north"], "west": mid_lon, "east": b["east"]},  # NE
    ]

# =========================
# OCM API
# =========================

def ocm_query_circle(lat: float, lon: float, distance_km: float) -> List[Dict[str, Any]]:
    """
    Query OCM using a circle search around (lat, lon).
    We deliberately set maxresults=500 and then detect truncation.
    """
    url = "https://api.openchargemap.io/v3/poi/"
    headers = {}
    if API_KEY:
        headers["X-API-Key"] = API_KEY

    params = {
        "output": "json",
        "latitude": lat,
        "longitude": lon,
        "distance": distance_km,
        "distanceunit": "KM",
        "maxresults": MAXRESULTS,
        "compact": "false",
        "verbose": "true",
    }

    r = requests.get(url, params=params, headers=headers, timeout=60)
    r.raise_for_status()
    data = r.json()
    if not isinstance(data, list):
        return []
    return data

def query_bbox_adaptive(b: Dict[str, float], seen: Set[int]) -> List[Dict[str, Any]]:
    """
    Recursively query a bbox by converting it to a circle query.
    If we hit 500 results, split into 4 and retry.
    Dedup by OCM ID using `seen`.
    """
    lat_span, lon_span = bbox_spans(b)
    c_lat, c_lon = bbox_center(b)

    # radius: max distance from center to bbox corner (covers bbox)
    corners = [
        (b["south"], b["west"]),
        (b["south"], b["east"]),
        (b["north"], b["west"]),
        (b["north"], b["east"]),
    ]
    radius = max(haversine_km(c_lat, c_lon, clat, clon) for clat, clon in corners)

    data = ocm_query_circle(c_lat, c_lon, radius)
    time.sleep(PAUSE_S)

    # If it looks truncated (maxed), split unless tile is already tiny
    if len(data) >= MAXRESULTS and lat_span > MIN_LAT_SPAN and lon_span > MIN_LON_SPAN:
        out: List[Dict[str, Any]] = []
        for child in split_bbox_4(b):
            out.extend(query_bbox_adaptive(child, seen))
        return out

    # Otherwise accept this tile and dedupe
    out: List[Dict[str, Any]] = []
    for item in data:
        try:
            oid = int(item.get("ID"))
        except Exception:
            continue
        if oid in seen:
            continue
        seen.add(oid)
        out.append(item)
    return out

# =========================
# FLATTEN TO YOUR CSV SHAPE
# =========================

def safe_get(d: Any, *path, default=""):
    cur = d
    for p in path:
        if cur is None:
            return default
        if isinstance(cur, dict):
            cur = cur.get(p)
        else:
            return default
    return cur if cur is not None else default

def norm_str(x) -> str:
    if x is None:
        return ""
    return str(x).strip()

def compute_max_kw(item: Dict[str, Any]) -> str:
    # Try to infer max kW from connections
    conns = item.get("Connections") or []
    best = None
    for c in conns:
        kw = safe_get(c, "PowerKW", default=None)
        try:
            kw = float(kw)
        except Exception:
            kw = None
        if kw is not None:
            best = kw if best is None else max(best, kw)
    return "" if best is None else str(best)

def compute_levels_and_dc(item: Dict[str, Any]) -> Tuple[str, int]:
    conns = item.get("Connections") or []
    levels = set()
    has_dc = 0
    for c in conns:
        lvl = safe_get(c, "Level", "Title", default="")
        t = norm_str(lvl).lower()
        if t:
            levels.add(norm_str(lvl))
        # DC heuristic: look for Level 3 in title OR CurrentType contains DC
        current = norm_str(safe_get(c, "CurrentType", "Title", default="")).lower()
        if "level 3" in t or "dc" in current:
            has_dc = 1
    return (", ".join(sorted(levels)), has_dc)

def compute_plugs(item: Dict[str, Any]) -> str:
    conns = item.get("Connections") or []
    plugs = []
    for c in conns:
        plugs.append(norm_str(safe_get(c, "ConnectionType", "Title", default="")))
    plugs = [p for p in plugs if p]
    # de-dupe while preserving order-ish
    seen = set()
    out = []
    for p in plugs:
        if p in seen:
            continue
        seen.add(p)
        out.append(p)
    return ", ".join(out)

def compute_current_types(item: Dict[str, Any]) -> str:
    conns = item.get("Connections") or []
    types = []
    for c in conns:
        types.append(norm_str(safe_get(c, "CurrentType", "Title", default="")))
    types = [t for t in types if t]
    seen = set()
    out = []
    for t in types:
        if t in seen:
            continue
        seen.add(t)
        out.append(t)
    return ", ".join(out)

def compute_num_points(item: Dict[str, Any]) -> str:
    # OCM has NumberOfPoints at station-level often
    n = item.get("NumberOfPoints")
    if n is None:
        # fallback: count connections
        conns = item.get("Connections") or []
        n = len(conns) if conns else ""
    return "" if n == "" else str(n)

def flatten_item(item: Dict[str, Any]) -> Dict[str, Any]:
    addr = item.get("AddressInfo") or {}
    operator = safe_get(item, "OperatorInfo", "Title", default="")

    levels, has_dc = compute_levels_and_dc(item)

    return {
        "ocm_id": safe_get(item, "ID", default=""),
        "lat": addr.get("Latitude"),
        "lon": addr.get("Longitude"),
        "name": norm_str(addr.get("Title") or item.get("Name") or ""),
        "operator": norm_str(operator),
        "address": norm_str(addr.get("AddressLine1") or ""),
        "town": norm_str(addr.get("Town") or ""),
        "state": norm_str(addr.get("StateOrProvince") or ""),
        "postcode": norm_str(addr.get("Postcode") or ""),
        "status_type": norm_str(safe_get(item, "StatusType", "Title", default="")),
        "usage_type": norm_str(safe_get(item, "UsageType", "Title", default="")),
        "usage_cost": norm_str(item.get("UsageCost") or ""),
        "access_comments": norm_str(addr.get("AccessComments") or ""),
        "general_comments": norm_str(item.get("GeneralComments") or ""),
        "instructions": norm_str(addr.get("Instructions") or ""),
        "num_points": compute_num_points(item),
        "max_kw": compute_max_kw(item),
        "plug_types": compute_plugs(item),
        "current_types": compute_current_types(item),
        "charging_levels": levels,
        "has_dc_fast": has_dc,
    }

# =========================
# MAIN
# =========================

def main():
    seen: Set[int] = set()
    print("Querying NYC via adaptive tiles (splitting on 500-cap)...")
    items = query_bbox_adaptive(NYC_BBOX, seen)
    print(f"Unique stations collected: {len(items)}")

    rows = []
    dropped = 0
    for it in items:
        row = flatten_item(it)
        # Validate lat/lon are numeric
        try:
            row["lat"] = float(row["lat"])
            row["lon"] = float(row["lon"])
        except Exception:
            dropped += 1
            continue
        rows.append(row)

    print(f"Rows written: {len(rows)} (dropped {dropped} missing lat/lon)")

    # Columns your Leaflet code uses (and some extras)
    fieldnames = [
        "ocm_id", "lat", "lon", "name", "operator",
        "address", "town", "state", "postcode",
        "num_points", "max_kw",
        "plug_types", "current_types", "charging_levels", "has_dc_fast",
        "status_type", "usage_type",
        "usage_cost", "access_comments", "general_comments", "instructions",
    ]

    with open(OUT_CSV, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames)
        w.writeheader()
        for r in rows:
            w.writerow(r)

    print(f"Done: {OUT_CSV}")

if __name__ == "__main__":
    main()
