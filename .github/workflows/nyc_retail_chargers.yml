"""
Pull all public/retail EV charging stations in NYC from the NYSERDA /
Open Data NY Socrata dataset (7rrd-248n), tag each with borough, and
write nyc_retail_chargers.csv.

Run locally:
    pip install requests
    python nyc_retail_chargers.py

Run in GitHub Actions: see workflow.yml below.
"""

import csv
import sys
import requests

SODA_ENDPOINT = "https://data.ny.gov/resource/7rrd-248n.json"

# NYC ZIP prefixes: Manhattan 100-102, Bronx 104, Staten Island 103,
# Brooklyn 112, Queens 110/111/113/114/116
NYC_ZIP_PREFIXES = ["100", "101", "102", "103", "104", "110", "111", "112", "113", "114", "116"]

FIELDS = [
    "borough", "station_name", "street_address", "city", "zip", "ev_network",
    "ev_level1_evse_num", "ev_level2_evse_num", "ev_dc_fast_count",
    "ev_connector_types", "access_days_time", "cards_accepted",
    "latitude", "longitude", "date_last_confirmed", "id",
]

def borough_from_zip(zip_code):
    z = (zip_code or "")[:3]
    if z in ("100", "101", "102"):
        return "Manhattan"
    if z == "104":
        return "Bronx"
    if z == "103":
        return "Staten Island"
    if z == "112":
        return "Brooklyn"
    if z in ("110", "111", "113", "114", "116"):
        return "Queens"
    return "Unknown"

def fetch_all_nyc_stations():
    where_clause = " OR ".join(f"starts_with(zip, '{p}')" for p in NYC_ZIP_PREFIXES)
    params = {"$where": where_clause, "$limit": 10000, "$order": "city"}
    resp = requests.get(SODA_ENDPOINT, params=params, timeout=60)
    resp.raise_for_status()
    return resp.json()

def main():
    try:
        stations = fetch_all_nyc_stations()
    except requests.RequestException as e:
        print(f"Request failed: {e}", file=sys.stderr)
        sys.exit(1)

    for s in stations:
        s["borough"] = borough_from_zip(s.get("zip"))

    with open("nyc_retail_chargers.csv", "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=FIELDS, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(stations)

    from collections import Counter
    counts = Counter(s["borough"] for s in stations)
    print(f"Wrote {len(stations)} NYC stations to nyc_retail_chargers.csv")
    for b, c in counts.most_common():
        print(f"  {b}: {c}")

if __name__ == "__main__":
    main()
