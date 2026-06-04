"""
Sikkim Assembly scraper. Reuses the Punjab parser since myneta's table layout
is identical across state-assembly pages.

Sikkim cycles on myneta (best-guess; verify via scripts/verify_state_slugs.py
before running a full scrape):
  Sikkim 2024 → slug "Sikkim2024"
  Sikkim 2019 → slug "sikkim2019"
  Sikkim 2014 → slug "sikkim2014"
  Sikkim 2009 → slug "sikkim2009"

Small state (32 MLAs per cycle) so the all-candidates pull is fast:
roughly 30 minutes for all 4 cycles at the standard 2-second rate limit.
"""
import logging
from app.scrapers.punjab import scrape_winners, scrape_all_candidates, WinnerRow

log = logging.getLogger(__name__)

SIKKIM_CYCLES = [
    {"year": 2024, "slug": "Sikkim2024"},
    {"year": 2019, "slug": "sikkim2019"},
    {"year": 2014, "slug": "sikkim2014"},
    {"year": 2009, "slug": "sikkim2009"},
]


def scrape_all_sikkim() -> dict[int, list[WinnerRow]]:
    """Winners across all Sikkim assembly cycles."""
    return {c["year"]: scrape_winners(c["slug"]) for c in SIKKIM_CYCLES}


def scrape_all_sikkim_candidates() -> dict[int, list[WinnerRow]]:
    """All candidates (winners + losers) across all Sikkim assembly cycles."""
    return {c["year"]: scrape_all_candidates(c["slug"]) for c in SIKKIM_CYCLES}
