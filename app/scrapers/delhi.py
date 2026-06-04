"""
Delhi (NCT) Assembly scraper. Reuses the Punjab parser since myneta's table layout
is identical across state-assembly pages.

Delhi cycles on myneta (best-guess; verify via scripts/verify_state_slugs.py
before running a full scrape):
  Delhi 2025 → slug "Delhi2025"   (just-concluded Feb 2025 election)
  Delhi 2020 → slug "delhi2020"
  Delhi 2015 → slug "delhi2015"
  Delhi 2013 → slug "delhi2013"
  Delhi 2008 → slug "delhi2008"

70 MLAs per cycle × 5 cycles × ~15 candidates per seat ≈ 5,000 candidate pages.
Full all-candidates scrape at the standard 2-second rate limit takes about
3 hours, end-to-end. Winners-only is ~12 minutes.
"""
import logging
from app.scrapers.punjab import scrape_winners, scrape_all_candidates, WinnerRow

log = logging.getLogger(__name__)

DELHI_CYCLES = [
    {"year": 2025, "slug": "Delhi2025"},
    {"year": 2020, "slug": "delhi2020"},
    {"year": 2015, "slug": "delhi2015"},
    {"year": 2013, "slug": "delhi2013"},
    {"year": 2008, "slug": "delhi2008"},
]


def scrape_all_delhi() -> dict[int, list[WinnerRow]]:
    """Winners across all Delhi assembly cycles."""
    return {c["year"]: scrape_winners(c["slug"]) for c in DELHI_CYCLES}


def scrape_all_delhi_candidates() -> dict[int, list[WinnerRow]]:
    """All candidates (winners + losers) across all Delhi assembly cycles."""
    return {c["year"]: scrape_all_candidates(c["slug"]) for c in DELHI_CYCLES}
