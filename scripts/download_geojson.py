"""
Download Punjab district boundaries.

Source confirmed working:
  https://github.com/datta07/INDIAN-SHAPEFILES (raw: PUNJAB_DISTRICTS.geojson)

The file has 23 Punjab district polygons. The homepage map renders these
and colors them by Punjab's three traditional sub-regions: Majha (north-west),
Doaba (central), and Malwa (south). MLA-level data drill-down stays in the
leaderboards, search, and constituency-level pages.

When a working Punjab assembly-constituency GeoJSON becomes available, drop
it at app/static/punjab_ac.geojson and the homepage will prefer it.

Usage:  python scripts/download_geojson.py
"""
import json
import sys
from pathlib import Path

import requests

OUT = Path(__file__).resolve().parent.parent / "app" / "static" / "punjab_districts.geojson"

URL = "https://raw.githubusercontent.com/datta07/INDIAN-SHAPEFILES/master/STATES/PUNJAB/PUNJAB_DISTRICTS.geojson"


def main():
    OUT.parent.mkdir(parents=True, exist_ok=True)

    # If the user passed an override URL on the command line, use it.
    url = sys.argv[1] if len(sys.argv) > 1 else URL

    try:
        print(f"Downloading {url}")
        r = requests.get(url, timeout=60)
        r.raise_for_status()
        data = r.json()
        if data.get("type") != "FeatureCollection" or not data.get("features"):
            print("Downloaded file is not a valid GeoJSON FeatureCollection.")
            sys.exit(1)
        print(f"  got {len(data['features'])} features")

        # Peek at the property keys so the frontend join is easy to set up
        sample_keys = list(data["features"][0].get("properties", {}).keys())
        print(f"  property keys: {sample_keys[:8]}")

        OUT.write_text(json.dumps(data))
        print(f"Saved -> {OUT}")
    except requests.RequestException as e:
        print(f"Download failed: {e}")
        print(f"\nManual fallback: download the file in your browser:")
        print(f"  {url}")
        print(f"and save it as: {OUT}")
        sys.exit(1)


if __name__ == "__main__":
    main()
