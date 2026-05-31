"""
Download India states GeoJSON for the homepage Political Integrity map.

Tries several known open data sources. The first one that returns a
valid FeatureCollection with state polygons wins. Saved to
app/static/india_states.geojson.

Usage:  python scripts/download_india_geojson.py
"""
import json
import sys
from pathlib import Path

import requests

OUT = Path(__file__).resolve().parent.parent / "app" / "static" / "india_states.geojson"

# Known open data sources for India state boundaries
SOURCES = [
    "https://raw.githubusercontent.com/datameet/maps/master/States/Admin2.geojson",
    "https://raw.githubusercontent.com/Anujarya300/bubble_maps/master/data/geojson-data/india-states.geojson",
    "https://gist.githubusercontent.com/jbrobst/56c13bbbf9d97d187fea01ca62ea5112/raw/india_states.geojson",
    "https://raw.githubusercontent.com/geohacker/india/master/states/india_states.geojson",
]


def try_url(url: str) -> bool:
    try:
        print(f"Trying {url}")
        r = requests.get(url, timeout=30)
        r.raise_for_status()
        data = r.json()
        if data.get("type") != "FeatureCollection" or not data.get("features"):
            print("  not a valid FeatureCollection")
            return False
        OUT.parent.mkdir(parents=True, exist_ok=True)
        OUT.write_text(json.dumps(data))
        print(f"  got {len(data['features'])} features")
        print(f"Saved -> {OUT}")
        return True
    except Exception as e:
        print(f"  failed: {e}")
        return False


def main():
    if len(sys.argv) > 1:
        if try_url(sys.argv[1]):
            return
        sys.exit(1)
    for url in SOURCES:
        if try_url(url):
            return
    print("\nAll sources failed. Download an India states GeoJSON manually")
    print(f"and save it as: {OUT}")
    sys.exit(1)


if __name__ == "__main__":
    main()
