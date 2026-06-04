"""
Sanity-check that every assembly-cycle slug registered in app/states.py points
to a real myneta page BEFORE running a multi-hour scrape against the wrong URL.

Run:
    python scripts/verify_state_slugs.py            # all states
    python scripts/verify_state_slugs.py sikkim     # one state only
    python scripts/verify_state_slugs.py sikkim delhi

What it does, per cycle:
  1. Hit the state-summary URL: https://myneta.info/<slug>/
  2. Check the response is HTTP 200 and looks like a real myneta page
     (must contain a "Constituency-wise" or "Candidates" link in the HTML).
  3. Print OK / FAIL with the URL so you can quickly fix bad guesses by
     opening myneta.info in a browser and finding the canonical slug.

This script uses the same polite HTTP client (myneta_client) the scrapers
use, so it's rate-limited and cached. Each fresh request takes ~2 seconds.
"""
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from app.states import ALL_STATES
from app.scrapers.myneta_client import fetch  # the polite, cached HTTP client


def looks_like_real_myneta_page(html: str) -> bool:
    """Cheap heuristic — a real summary page mentions both Candidates and a year."""
    if not html or len(html) < 1000:
        return False
    lower = html.lower()
    return (
        "candidates" in lower
        and "constituency" in lower
        and "404" not in lower[:500]
    )


def verify(state_key: str) -> tuple[int, int]:
    cfg = ALL_STATES[state_key]
    ok = bad = 0
    print(f"\n{cfg.name} ({state_key}):")
    for cycle in cfg.assembly_cycles:
        slug = cycle["slug"]
        url = f"https://myneta.info/{slug}/"
        try:
            html = fetch(url)
            if looks_like_real_myneta_page(html):
                print(f"  OK    {slug:20}  →  {url}")
                ok += 1
            else:
                print(f"  FAIL  {slug:20}  page reached but content looks wrong")
                bad += 1
        except Exception as e:
            print(f"  FAIL  {slug:20}  {type(e).__name__}: {e}")
            bad += 1
    return ok, bad


def main():
    targets = sys.argv[1:] or list(ALL_STATES.keys())
    missing = [t for t in targets if t.lower() not in ALL_STATES]
    if missing:
        print(f"Unknown state(s): {missing}")
        print(f"Known states: {sorted(ALL_STATES.keys())}")
        sys.exit(1)

    total_ok = total_bad = 0
    for t in targets:
        ok, bad = verify(t.lower())
        total_ok += ok
        total_bad += bad

    print("\n" + "=" * 60)
    print(f"Summary: {total_ok} OK, {total_bad} FAIL")
    if total_bad:
        print("Fix any FAIL slugs in app/states.py before running the scraper.")
        sys.exit(1)
    print("All slugs verified. Safe to scrape.")


if __name__ == "__main__":
    main()
