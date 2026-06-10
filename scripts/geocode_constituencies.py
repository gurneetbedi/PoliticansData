"""
Geocode assembly constituencies to (lat, lng) using OpenStreetMap Nominatim.
Run once after the scraper populates the DB. Output is saved as
app/static/constituency_coords.json which the homepage map reads.

Polite to Nominatim:
  - 1 request per second (their published limit is 1/sec for free use)
  - identifies the project via User-Agent
  - results are cached on disk; re-running is instant for already-geocoded
    constituencies (use --refresh to force re-fetch)

Usage:
  python scripts/geocode_constituencies.py                  # all tracked states
  python scripts/geocode_constituencies.py punjab           # one state only
  python scripts/geocode_constituencies.py punjab bihar     # several states
  python scripts/geocode_constituencies.py --refresh        # re-fetch even cached

Output format (nested by state to avoid cross-state name collisions):
  {
    "Punjab": {"ABOHAR": {"lat": ..., "lng": ...}, ...},
    "Bihar":  {"PATNA SAHIB": {...}, ...}
  }

This is backward compatible with the old flat-keyed format: at startup,
if the file looks flat (top-level keys are constituency names, not state
names), it's auto-migrated into a {"Punjab": {flat dict}} structure.
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
from app.states import ALL_STATES

OUT = ROOT / "app" / "static" / "constituency_coords.json"

NOMINATIM = "https://nominatim.openstreetmap.org/search"
USER_AGENT = "PoliTrack/0.1 (open-source transparency project; contact: gurneet.bedi@me.com)"
RATE_LIMIT = 1.1  # seconds between calls (Nominatim free tier policy)


def normalize_name(name: str) -> str:
    """Strip '(SC)' or '(ST)' reservation suffixes — Nominatim hits better on plain names."""
    if not name:
        return ""
    return (
        name.replace("(SC)", "")
            .replace("(ST)", "")
            .replace("(sc)", "")
            .replace("(st)", "")
            .strip()
    )


def geocode(constituency_name: str, state_name: str) -> dict | None:
    """Query Nominatim for a single (constituency, state) tuple."""
    q = f"{normalize_name(constituency_name)}, {state_name}, India"
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
        print(f"      ! error geocoding {constituency_name!r} / {state_name}: {e}")
        return None


def load_existing() -> dict[str, dict[str, dict]]:
    """
    Load the existing coords file. Migrates the old flat format
    ({"ABOHAR": {...}}) into the new nested-by-state format
    ({"Punjab": {"ABOHAR": {...}}}) transparently.
    """
    if not OUT.exists():
        return {}
    try:
        data = json.loads(OUT.read_text())
    except Exception:
        return {}
    if not isinstance(data, dict) or not data:
        return {}
    # Heuristic: if the first value is a {lat, lng} dict, we're in flat format.
    first_val = next(iter(data.values()))
    if isinstance(first_val, dict) and "lat" in first_val and "lng" in first_val:
        # Old flat format — migrate. All historical flat keys were Punjab.
        print("  (migrating old flat-keyed coords file into nested-by-state)")
        return {"Punjab": data}
    return data


def main():
    refresh = "--refresh" in sys.argv
    requested = [a.lower() for a in sys.argv[1:] if not a.startswith("--")]

    # Resolve the state targets. If none specified, process every state in
    # ALL_STATES that has any assembly constituencies in the DB.
    coords = load_existing()

    session = SessionLocal()
    try:
        # Pull every (state_name, constituency_name) tuple that has at least
        # one assembly appearance — this is the universe of constituencies
        # to potentially geocode. Filtered by --refresh + requested-states.
        q = (
            session.query(State.name, Constituency.name)
            .join(Constituency, Constituency.state_id == State.id)
            .filter(Constituency.house == "Assembly")
            .distinct()
            .order_by(State.name, Constituency.name)
        )
        rows = q.all()

        # Group rows by state for nicer per-state progress output
        by_state: dict[str, list[str]] = {}
        for state_name, cons_name in rows:
            if not cons_name:
                continue
            by_state.setdefault(state_name, []).append(cons_name)

        # If the user asked for specific states, filter
        if requested:
            allowed_names = {ALL_STATES[k].name for k in requested if k in ALL_STATES}
            unknown = [k for k in requested if k not in ALL_STATES]
            if unknown:
                print(f"Unknown state keys: {unknown}")
                print(f"Known keys: {sorted(ALL_STATES.keys())}")
                sys.exit(1)
            by_state = {s: c for s, c in by_state.items() if s in allowed_names}

        total_constituencies = sum(len(v) for v in by_state.values())
        print(f"Target: {len(by_state)} states · {total_constituencies} constituencies\n")

        total_added = 0
        total_missed: list[tuple[str, str]] = []

        for state_name, cons_list in by_state.items():
            print(f"=== {state_name} ({len(cons_list)} constituencies) ===")
            bucket = coords.setdefault(state_name, {})
            added_for_state = 0

            for cons_name in cons_list:
                key = normalize_name(cons_name).upper()
                if key in bucket and not refresh:
                    continue

                print(f"    geocoding {cons_name!r} -> {key!r}")
                result = geocode(cons_name, state_name)
                if result:
                    bucket[key] = result
                    added_for_state += 1
                    total_added += 1
                else:
                    total_missed.append((state_name, cons_name))
                time.sleep(RATE_LIMIT)

                # Save incrementally so Ctrl-C mid-run doesn't lose work
                OUT.parent.mkdir(parents=True, exist_ok=True)
                OUT.write_text(json.dumps(coords, indent=2))

            print(f"  → {added_for_state} new for {state_name}\n")

        print("=" * 60)
        print(f"Done. Added {total_added} new coords across {len(by_state)} states.")
        print(f"Cached in {OUT}")
        if total_missed:
            print(f"\nCould not geocode ({len(total_missed)}):")
            for state, cons in total_missed[:30]:
                print(f"  {state}: {cons}")
            if len(total_missed) > 30:
                print(f"  ...and {len(total_missed) - 30} more")
    finally:
        session.close()


if __name__ == "__main__":
    main()
