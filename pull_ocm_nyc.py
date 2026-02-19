import os
import requests
import pandas as pd

API_KEY = os.getenv("OCM_API_KEY")
if not API_KEY:
    raise SystemExit("Set OCM_API_KEY env var first")

URL = "https://api.openchargemap.io/v3/poi/"

# NYC bbox (covers all 5 boroughs)
# Top-left (NW):  40.9176, -74.2591
# Bottom-right(SE):40.4774, -73.7004
NYC_BBOX = "(40.9176,-74.2591),(40.4774,-73.7004)"

params = {
    "output": "json",
    "countrycode": "US",
    "boundingbox": NYC_BBOX,
    "maxresults": 500,     # if you need more than cap, tile the bbox (see below)
    "compact": True,
    "verbose": False,
}

headers = {
    "X-API-Key": API_KEY,  # supported auth style for OCM API keys :contentReference[oaicite:2]{index=2}
    "User-Agent": "NYC-Retail-Map/1.0 (contact: you@yourorg.com)",
}

r = requests.get(URL, params=params, headers=headers, timeout=60)
r.raise_for_status()
pois = r.json()

rows = []
for p in pois:
    a = p.get("AddressInfo") or {}
    rows.append({
        "ocm_id": p.get("ID"),
        "name": a.get("Title"),
        "address": a.get("AddressLine1"),
        "town": a.get("Town"),
        "state": a.get("StateOrProvince"),
        "postcode": a.get("Postcode"),
        "lat": a.get("Latitude"),
        "lon": a.get("Longitude"),
        "num_points": p.get("NumberOfPoints"),
        "operator": (p.get("OperatorInfo") or {}).get("Title"),
        "usage_type": (p.get("UsageType") or {}).get("Title"),
        "status_type": (p.get("StatusType") or {}).get("Title"),
    })

df = pd.DataFrame(rows).dropna(subset=["lat", "lon"]).drop_duplicates(subset=["ocm_id"])
df.to_csv("ocm_nyc_retail_locations.csv", index=False)
print(f"Saved {len(df)} locations -> ocm_nyc_retail_locations.csv")
