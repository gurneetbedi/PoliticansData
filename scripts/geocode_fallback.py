"""
Fill geocoding gaps for constituencies that OSM Nominatim couldn't
find on the first pass (scripts/geocode_constituencies.py).

Two-pass strategy:

Pass 1 — smarter Nominatim queries. Retry with:
  - (BL)/(SC)/(ST) suffix stripped
  - First hyphen-token only (e.g. "Aizawl East-I" → "Aizawl")
  - "<name>, <district>, <state>" hint

Pass 2 — hardcoded district centroids. For constituencies that share
a district-word prefix (Aizawl East-I, Aizawl North-II, etc.), we
place all of them on the district centroid. Approximate but functional
for a heatmap; distinguishable by state/party but overlapping visually.
Editorial polish (per-constituency accuracy) can come later.

Usage:
    python scripts/geocode_fallback.py
"""
from __future__ import annotations

import json
import re
import sys
import time
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parent.parent
COORDS_PATH = ROOT / "app/static/constituency_coords.json"

USER_AGENT = ("Lokvani/0.1 (open-source civic transparency; "
              "contact: gurneet.bedi@me.com)")
RATE_LIMIT = 1.1   # Nominatim asks for 1 req/sec; add slight buffer.


# ---------------------------------------------------------------------------
# Hardcoded district centroids for fallback.
# Keyed by (state, first-word or district-name-lowered).
# Values are approximate lat/lng of the district's central city.
# ---------------------------------------------------------------------------

DISTRICT_CENTROIDS = {
    # Mizoram
    ("Mizoram", "aizawl"):    (23.7307, 92.7173),
    ("Mizoram", "champhai"):  (23.4562, 93.3277),
    ("Mizoram", "lunglei"):   (22.8814, 92.7379),
    ("Mizoram", "lawngtlai"): (22.5290, 92.8925),
    ("Mizoram", "kolasib"):   (24.2262, 92.6789),
    ("Mizoram", "serchhip"):  (23.3060, 92.8543),
    ("Mizoram", "mamit"):     (23.9310, 92.4907),
    ("Mizoram", "hachhek"):   (23.9310, 92.4907),   # Mamit district
    ("Mizoram", "chalfilh"):  (23.7307, 92.7173),   # Aizawl district
    ("Mizoram", "hrangturzo"):(23.4562, 93.3277),   # Serchhip area
    ("Mizoram", "thorang"):   (23.7307, 92.7173),
    ("Mizoram", "east"):      (23.3060, 92.8543),   # East Tuipui/etc
    ("Mizoram", "west"):      (23.3060, 92.8543),
    ("Mizoram", "south"):     (22.8814, 92.7379),
    ("Mizoram", "north"):     (23.7307, 92.7173),

    # Sikkim
    ("Sikkim", "gangtok"):    (27.3389, 88.6065),
    ("Sikkim", "namchi"):     (27.1750, 88.3639),
    ("Sikkim", "mangan"):     (27.5087, 88.5303),
    ("Sikkim", "namcheybung"):(27.3389, 88.6065),   # East district
    ("Sikkim", "kabi"):       (27.4300, 88.5600),   # North district
    ("Sikkim", "djongu"):     (27.4300, 88.5600),
    ("Sikkim", "barfung"):    (27.2100, 88.4300),
    ("Sikkim", "temi"):       (27.2000, 88.4300),
    ("Sikkim", "poklok"):     (27.1750, 88.3639),   # Namchi area
    ("Sikkim", "namthang"):   (27.1750, 88.3639),
    ("Sikkim", "rangang"):    (27.2100, 88.4300),
    ("Sikkim", "salghari"):   (27.1750, 88.3639),
    ("Sikkim", "chujachen"):  (27.3389, 88.6065),
    ("Sikkim", "yoksam"):     (27.3808, 88.2296),
    ("Sikkim", "gyalshing"):  (27.2822, 88.2617),
    ("Sikkim", "daramdin"):   (27.2822, 88.2617),
    ("Sikkim", "maneybung"):  (27.2822, 88.2617),
    ("Sikkim", "soreng"):     (27.1636, 88.2050),
    ("Sikkim", "martam"):     (27.3389, 88.6065),
    ("Sikkim", "gnathang"):   (27.3050, 88.9000),
    ("Sikkim", "shyari"):     (27.3389, 88.6065),
    ("Sikkim", "tumen"):      (27.5087, 88.5303),
    ("Sikkim", "upper"):      (27.3389, 88.6065),
    ("Sikkim", "sangha"):     (27.3389, 88.6065),

    # Nagaland
    ("Nagaland", "kohima"):        (25.6751, 94.1086),
    ("Nagaland", "dimapur"):       (25.9091, 93.7266),
    ("Nagaland", "mokokchung"):    (26.3255, 94.5158),
    ("Nagaland", "wokha"):         (26.0940, 94.2586),
    ("Nagaland", "zunheboto"):     (26.0200, 94.5300),
    ("Nagaland", "tuensang"):      (26.2765, 94.8281),
    ("Nagaland", "mon"):           (26.7189, 95.0989),
    ("Nagaland", "phek"):          (25.6600, 94.4750),
    ("Nagaland", "kiphire"):       (25.9000, 94.8000),
    ("Nagaland", "longleng"):      (26.5049, 94.8280),
    ("Nagaland", "peren"):         (25.5164, 93.7370),
    ("Nagaland", "northern"):      (25.7000, 94.1500),   # N. Angami
    ("Nagaland", "southern"):      (25.6500, 94.0500),   # S. Angami
    ("Nagaland", "western"):       (25.5900, 94.0500),   # W. Angami
    ("Nagaland", "chazouba"):      (25.6800, 94.4500),
    ("Nagaland", "arkakong"):      (26.3255, 94.5158),
    ("Nagaland", "aonglenden"):    (26.3255, 94.5158),
    ("Nagaland", "angetyongpang"): (26.3255, 94.5158),
    ("Nagaland", "jangpetkong"):   (26.3255, 94.5158),
    ("Nagaland", "koridang"):      (26.3255, 94.5158),
    ("Nagaland", "longkhim"):      (26.2765, 94.8281),
    ("Nagaland", "moka"):          (26.3255, 94.5158),
    ("Nagaland", "seyochung"):     (25.9000, 94.8000),
    ("Nagaland", "shamator"):      (26.2765, 94.8281),
    ("Nagaland", "tapi"):          (26.5049, 94.8280),
    ("Nagaland", "tehok"):         (26.7189, 95.0989),
    ("Nagaland", "tenning"):       (25.5164, 93.7370),
    ("Nagaland", "tyui"):          (26.0940, 94.2586),
    ("Nagaland", "alongtaki"):     (26.5049, 94.8280),
    ("Nagaland", "ghaspani"):      (25.9091, 93.7266),

    # Puducherry (Tamil-transliterated names)
    ("Puducherry", "ariankuppam"): (11.8890, 79.7970),
    ("Puducherry", "kadirgamam"):  (11.9339, 79.8345),
    ("Puducherry", "karaikal"):    (10.9254, 79.8380),
    ("Puducherry", "manavely"):    (10.8956, 79.7936),
    ("Puducherry", "neravy"):      (10.9130, 79.8500),
    ("Puducherry", "orleampeth"):  (11.9139, 79.8145),
    ("Puducherry", "oupalam"):     (11.9403, 79.8069),
    ("Puducherry", "raj"):         (11.9339, 79.8345),   # Raj Bhavan area

    # Goa
    ("Goa", "maem"):     (15.5911, 73.9147),   # Bicholim area
    ("Goa", "siroda"):   (15.3822, 74.0433),   # Ponda area
    ("Goa", "st"):       (15.5100, 73.9000),   # St. Andre / Ilhas de Goa

    # Himachal Pradesh
    ("Himachal Pradesh", "doon"):        (30.9587, 76.5232),   # Solan district
    ("Himachal Pradesh", "jaswan"):      (31.9200, 76.1300),   # Kangra
    ("Himachal Pradesh", "nachan"):      (31.7000, 76.9500),   # Mandi
    ("Himachal Pradesh", "seraj"):       (31.5800, 77.2500),   # Mandi
    ("Himachal Pradesh", "sri"):         (31.3200, 76.5100),   # Bilaspur area (Sri Naina Deviji)
    ("Himachal Pradesh", "renukaji"):    (30.6100, 77.4600),   # Sirmaur
}


def normalize_key(name: str) -> str:
    """Match the normalization in geocode_constituencies.py."""
    s = re.sub(r"[^A-Z0-9]+", "", (name or "").upper()).strip()
    return s


def strip_reservation(name: str) -> str:
    """Remove (BL)/(SC)/(ST) reservation suffixes."""
    s = name.strip()
    for suf in ("(BL)", "(SC)", "(ST)"):
        if s.endswith(suf):
            s = s[: -len(suf)].strip()
    return s


def first_word(name: str) -> str:
    """Extract the first word (before any space or hyphen)."""
    s = strip_reservation(name).strip()
    m = re.match(r"^([A-Za-z]+)", s)
    return m.group(1).lower() if m else ""


def nominatim_geocode(query: str) -> tuple[float, float] | None:
    try:
        r = requests.get(
            "https://nominatim.openstreetmap.org/search",
            params={"q": query, "format": "json", "limit": 1,
                    "countrycodes": "in"},
            headers={"User-Agent": USER_AGENT},
            timeout=15,
        )
        r.raise_for_status()
        data = r.json()
        if data:
            return (float(data[0]["lat"]), float(data[0]["lon"]))
    except Exception as e:
        print(f"    Nominatim error: {e}")
    return None


def fallback_centroid(state: str, cons: str) -> tuple[float, float] | None:
    """Look up hardcoded district centroid for this constituency."""
    stripped = strip_reservation(cons).lower()
    # Try full first-word first, then any hyphen-token
    keys = []
    for tok in re.split(r"[-\s]+", stripped):
        if tok:
            keys.append(tok)
    for k in keys:
        hit = DISTRICT_CENTROIDS.get((state, k))
        if hit:
            return hit
    return None


def main():
    if not COORDS_PATH.exists():
        sys.exit(f"Coords file not found at {COORDS_PATH}")

    coords = json.loads(COORDS_PATH.read_text())

    # Get missing constituencies from the DB
    import sqlite3
    con = sqlite3.connect(ROOT / "lokvani.db")
    cur = con.cursor()

    total_added = 0
    total_missing_still = []

    for state in ("Goa", "Himachal Pradesh", "Mizoram", "Nagaland",
                   "Puducherry", "Sikkim"):
        cur.execute("""
            SELECT c.name FROM constituencies c
            JOIN states s ON c.state_id = s.id
            WHERE s.name = ? AND c.house = 'Assembly'
            ORDER BY c.name
        """, (state,))
        all_cons = [r[0] for r in cur.fetchall()]
        bucket = coords.setdefault(state, {})
        have = {normalize_key(k) for k in bucket.keys()}
        missing = [c for c in all_cons if normalize_key(c) not in have]

        if not missing:
            continue
        print(f"\n=== {state}: {len(missing)} missing ===")

        for cons in missing:
            key = normalize_key(cons)
            result = None

            # Pass 1a — Nominatim with (BL)/(SC)/(ST) stripped
            stripped = strip_reservation(cons)
            if stripped != cons:
                q = f"{stripped}, {state}, India"
                print(f"  → try stripped: {q!r}")
                result = nominatim_geocode(q)
                time.sleep(RATE_LIMIT)

            # Pass 1b — first hyphen-token only
            if not result and "-" in cons:
                first = cons.split("-")[0].strip()
                q = f"{first}, {state}, India"
                print(f"  → try first-token: {q!r}")
                result = nominatim_geocode(q)
                time.sleep(RATE_LIMIT)

            # Pass 2 — hardcoded district centroid
            if not result:
                result = fallback_centroid(state, cons)
                if result:
                    print(f"  → fallback centroid {result}")

            if result:
                bucket[key] = {"lat": result[0], "lng": result[1]}
                total_added += 1
                print(f"    ✓ added {cons!r} → {result}")
            else:
                total_missing_still.append((state, cons))
                print(f"    ✗ still missing {cons!r}")

            # Save incrementally so Ctrl-C doesn't lose work
            COORDS_PATH.write_text(json.dumps(coords, indent=2))

    con.close()

    print()
    print("=" * 60)
    print(f"Added {total_added} centroids.")
    if total_missing_still:
        print(f"Still missing ({len(total_missing_still)}):")
        for s, c in total_missing_still:
            print(f"  {s}: {c}")
    print(f"Saved to {COORDS_PATH}")


if __name__ == "__main__":
    main()
