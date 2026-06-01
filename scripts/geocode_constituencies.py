"""
Geocode each Punjab assembly constituency to a (lat, lng) using OpenStreetMap
Nominatim. Run once after the scraper populates the DB. Output is saved as
app/static/constituency_coords.json which the homepage map reads.

Polite to Nominatim:
  - 1 request per second (their published limit is 1/sec for free use)
  - identifies the project via User-Agent
  - results are cached on disk; re-running is instant

Usage:
  python scripts/geocode_constituencies.py
  python scripts/geocode_constituencies.py --refresh   # re-fetch even cached

If you get an empty result for some constituencies, the script logs them.
The map renders dots only for constituencies it has coords for; missing ones
simply do not show a dot (no crash).
"""
import json
import sys
import time
from pathlib import Path

import requests
from sqlalchemy import distinct

# Allow running this from anywhere
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.database import SessionLocal
from app.models import Constituency, State

OUT = ROOT / "app" / "static" / "constituency_coords.json"

NOMINATIM = "https://nominatim.openstreetmap.org/search"
USER_AGENT = "PoliTrack/0.1 (open-source transparency project; contact: gurneet.bedi@me.com)"
RATE_LIMIT = 1.1  # seconds between calls (Nominatim free tier policy)


def normalize_name(name: str) -> str:
    """Strip '(SC)' or '(ST)' suffixes — Nominatim hits better on plain names."""
    if not name:
        return ""
    return (
        name.replace("(SC)", "")
            .replace("(ST)", "")
            .replace("(sc)", "")
            .replace("(st)", "")
            .strip()
    )


def geocode(name: str) -> dict | None:
    """Query Nominatim for a single constituency. Returns {'lat': ..., 'lng': ...} or None."""
    q = f"{normalize_name(name)}, Punjab, India"
    try:
        r = requests.get(
            NOMINATIM,
            params={"q": q, "format": "json", "limit": 1, "countrycodes": "in"},
            headers={"User-Agent": USER_AGENT},
            timeout=20,
        )
        r.raise_for_status()
        data = r.json()
        if not data:
            return None
        return {"lat": float(data[0]["lat"]), "lng": float(data[0]["lon"])}
    except Exception as e:
        print(f"  ! error geocoding {name!r}: {e}")
        return None


def main():
    refresh = "--refresh" in sys.argv

    # Load existing coords (so we don't re-geocode every run)
    existing: dict[str, dict] = {}
    if OUT.exists() and not refresh:
        try:
            existing = json.loads(OUT.read_text())
        except Exception:
            existing = {}

    session = SessionLocal()
    try:
        # Get all Punjab assembly constituencies in the DB
        rows = (
            session.query(Constituency)
            .join(State, Constituency.state_id == State.id)
            .filter(State.name == "Punjab")
            .filter(Constituency.house == "Assembly")
            .order_by(Constituency.name)
            .all()
        )
        print(f"Found {len(rows)} Punjab assembly constituencies in DB")

        coords = dict(existing)
        added = 0
        missed = []

        for c in rows:
            key = normalize_name(c.name).upper()
            if key in coords and not refresh:
                continue
            print(f"  geocoding {c.name!r} -> {key!r}")
            result = geocode(c.name)
            if result:
                coords[key] = result
                added += 1
            else:
                missed.append(c.name)
            time.sleep(RATE_LIMIT)

            # Save incrementally so a Ctrl-C mid-run does not lose work
            OUT.parent.mkdir(parents=True, exist_ok=True)
            OUT.write_text(json.dumps(coords, indent=2))

        print(f"\nDone. Added {added} new coords. Total: {len(coords)}.")
        if missed:
            print(f"Could not geocode ({len(missed)}): {missed}")
        print(f"Saved -> {OUT}")
    finally:
        session.close()


if __name__ == "__main__":
    main()
