#!/usr/bin/env python3
"""
pull_ocm_ny.py — OpenChargeMap export with adaptive tiling (no 500-cap truncation)

✅ Reads API key from env var: OCM_API_KEY (GitHub Secrets)
✅ Sends key to OCM via header: X-API-Key
✅ Adaptive tiling: recursively splits tiles that hit the 500 result cap
✅ CRITICAL: split decision uses RAW response length (before filters)
✅ Coverage: ALL OF NEW YORK STATE (so you do NOT lose NYS chargers)
✅ Filters:
   - Remove New Jersey only
   - Remove Tesla operator
   - ONLY allow approved operators:
     AmpUp, Blink, EV Connect, ChargePoint, EV Gateway, EVgo, FLO, Ionna,
     Noodoe, PowerFlex, Revel, ViaLynk
   - Access must be Public only (UsageType contains "public")
✅ Dedupes by OCM ID
✅ Outputs: ocm_nyc_retail_locations.csv (compatible with your Leaflet map)
"""

import os
import csv
import math
import time
import random
from typing import Dict, List, Tuple, Any, Set, Optional

import requests


# =========================
# CONFIG
# =========================

OCM_API_KEY = os.getenv("OCM_API_KEY", "").strip()
if not OCM_API_KEY:
    raise SystemExit(
        "Missing OCM_API_KEY. Add it to GitHub Secrets and pass env: OCM_API_KEY to the workflow step."
    )

OUT_CSV = "ocm_nyc_retail_locations.csv"

# ✅ ALL NEW YORK STATE bounding box (keeps everything you had before)
# Approx NYS extents:
# South ~40.49  North ~45.02  West ~-79.76  East ~-71.85
NY_STATE_BBOX = {
    "south": 40.49,
    "north": 45.02,
    "west": -79.76,
    "east": -71.85,
}

MAXRESULTS = 500

# Stop splitting when already small
MIN_LAT_SPAN = 0.02
MIN_LON_SPAN = 0.02

PAUSE_S = 0.6
MAX_RETRIES = 6
TIMEOUT_S = 60


# =========================
# HELPERS
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
    return "" if x is None else str(x).strip()


# =========================
# FILTERS (ONLY what you asked)
# =========================

APPROVED_OPERATORS = {
    "ampup",
    "blink",
    "ev connect",
    "chargepoint",
    "ev gateway",
    "evgo",
    "flo",
    "ionna",
    "noodoe",
    "powerflex",
    "revel",
    "vialynk",
}


def normalize_operator_name(op: str) -> str:
    op = (op or "").lower()
    op = op.replace(",", " ").replace(".", " ")
    op = " ".join(op.split())
    return op


def operator_allowed(item: Dict[str, Any]) -> bool:
    op = normalize_operator_name(norm_str(safe_get(item, "OperatorInfo", "Title", default="")))
    if not op:
        return False
    for allowed in APPROVED_OPERATORS:
        if allowed in op:
            return True
    return False


def is_public_access(item: Dict[str, Any]) -> bool:
    usage = norm_str(safe_get(item, "UsageType", "Title", default="")).lower()
    return bool(usage) and ("public" in usage)


def is_new_jersey(item: Dict[str, Any]) -> bool:
    st = norm_str(safe_get(item, "AddressInfo", "StateOrProvince", default="")).upper()
    return st == "NJ" or "NEW JERSEY" in st


def is_tesla_operator(item: Dict[str, Any]) -> bool:
    op = normalize_operator_name(norm_str(safe_get(item, "OperatorInfo", "Title", default="")))
    return "tesla" in op


# =========================
# GEO HELPERS
# =========================

def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    R = 6371.0088
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlmb = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlmb / 2) ** 2
    return 2 * R * math.asin(math.sqrt(a))


def bbox_center(b: Dict[str, float]) -> Tuple[float, float]:
    return (b["south"] + b["north"]) / 2.0, (b["west"] + b["east"]) / 2.0


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


def item_in_bbox(item: Dict[str, Any], b: Dict[str, float]) -> bool:
    addr = item.get("AddressInfo") or {}
    lat = addr.get("Latitude")
    lon = addr.get("Longitude")
    try:
        lat = float(lat)
        lon = float(lon)
    except Exception:
        return False
    return (b["south"] <= lat <= b["north"]) and (b["west"] <= lon <= b["east"])


# =========================
# OCM API
# =========================

def request_with_retries(url: str, params: Dict[str, Any], headers: Dict[str, str]) -> List[Dict[str, Any]]:
    last_err: Optional[Exception] = None
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            r = requests.get(url, params=params, headers=headers, timeout=TIMEOUT_S)

            if r.status_code == 200:
                data = r.json()
                return data if isinstance(data, list) else []

            if r.status_code in (429, 500, 502, 503, 504, 403):
                retry_after = r.headers.get("Retry-After")
                sleep_s = None
                if retry_after:
                    try:
                        sleep_s = float(retry_after)
                    except Exception:
                        sleep_s = None

                base = 0.8 * (2 ** (attempt - 1))
                jitter = random.uniform(0.0, 0.6)
                wait = sleep_s if sleep_s is not None else min(20.0, base + jitter)
                print(f"OCM HTTP {r.status_code} (attempt {attempt}/{MAX_RETRIES}). Backing off {wait:.1f}s...")
                time.sleep(wait)
                continue

            r.raise_for_status()

        except Exception as e:
            last_err = e
            base = 0.8 * (2 ** (attempt - 1))
            jitter = random.uniform(0.0, 0.6)
            wait = min(20.0, base + jitter)
            print(f"Request error (attempt {attempt}/{MAX_RETRIES}): {e}. Backing off {wait:.1f}s...")
            time.sleep(wait)

    raise RuntimeError(f"Failed after {MAX_RETRIES} retries. Last error: {last_err}")


def ocm_query_circle(lat: float, lon: float, distance_km: float) -> List[Dict[str, Any]]:
    url = "https://api.openchargemap.io/v3/poi/"
    headers = {"X-API-Key": OCM_API_KEY}
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
    data = request_with_retries(url, params, headers)
    time.sleep(PAUSE_S)
    return data


def query_bbox_adaptive(b: Dict[str, float], seen: Set[int]) -> List[Dict[str, Any]]:
    """
    CRITICAL:
    - Decide split based on RAW response length (cap happens before filters).
    - Then apply only the requested filters.
    """
    lat_span, lon_span = bbox_spans(b)
    c_lat, c_lon = bbox_center(b)

    corners = [
        (b["south"], b["west"]),
        (b["south"], b["east"]),
        (b["north"], b["west"]),
        (b["north"], b["east"]),
    ]
    radius = max(haversine_km(c_lat, c_lon, clat, clon) for clat, clon in corners)

    raw = ocm_query_circle(c_lat, c_lon, radius)
    raw_len = len(raw)

    # Split if we hit the cap (before filtering)
    if raw_len >= MAXRESULTS and lat_span > MIN_LAT_SPAN and lon_span > MIN_LON_SPAN:
        out: List[Dict[str, Any]] = []
        for child in split_bbox_4(b):
            out.extend(query_bbox_adaptive(child, seen))
        return out

    # Keep only points truly in this bbox (prevents circle bleed far away)
    data = [it for it in raw if item_in_bbox(it, b)]

    # Apply ONLY the filters you asked for:
    data = [it for it in data if not is_new_jersey(it)]
    data = [it for it in data if not is_tesla_operator(it)]
    data = [it for it in data if operator_allowed(it)]
    data = [it for it in data if is_public_access(it)]

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
# FLATTEN TO CSV
# =========================

def compute_max_kw(item: Dict[str, Any]) -> str:
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
        lvl = norm_str(safe_get(c, "Level", "Title", default=""))
        if lvl:
            levels.add(lvl)
        t = lvl.lower()
        current = norm_str(safe_get(c, "CurrentType", "Title", default="")).lower()
        if "level 3" in t or "dc" in current:
            has_dc = 1
    return (", ".join(sorted(levels)), has_dc)


def compute_plugs(item: Dict[str, Any]) -> str:
    conns = item.get("Connections") or []
    plugs = [norm_str(safe_get(c, "ConnectionType", "Title", default="")) for c in conns]
    plugs = [p for p in plugs if p]
    seen_local = set()
    out = []
    for p in plugs:
        if p in seen_local:
            continue
        seen_local.add(p)
        out.append(p)
    return ", ".join(out)


def compute_current_types(item: Dict[str, Any]) -> str:
    conns = item.get("Connections") or []
    types = [norm_str(safe_get(c, "CurrentType", "Title", default="")) for c in conns]
    types = [t for t in types if t]
    seen_local = set()
    out = []
    for t in types:
        if t in seen_local:
            continue
        seen_local.add(t)
        out.append(t)
    return ", ".join(out)


def compute_num_points(item: Dict[str, Any]) -> str:
    n = item.get("NumberOfPoints")
    if n is None:
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


def main():
    print("OCM key detected:", True)
    print("Querying ALL NYS via adaptive tiles (splitting correctly on 500-cap)...")

    seen: Set[int] = set()
    items = query_bbox_adaptive(NY_STATE_BBOX, seen)

    print(f"Unique NY stations collected (filtered): {len(items)}")

    rows: List[Dict[str, Any]] = []
    dropped = 0
    for it in items:
        row = flatten_item(it)
        try:
            row["lat"] = float(row["lat"])
            row["lon"] = float(row["lon"])
        except Exception:
            dropped += 1
            continue
        rows.append(row)

    print(f"Rows written: {len(rows)} (dropped {dropped} missing lat/lon)")

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
