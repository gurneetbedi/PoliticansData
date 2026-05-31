"""
Goa Assembly scraper. Reuses the Punjab parser since myneta's table layout
is identical across state-assembly pages.

Goa cycles on myneta (guess based on the standard pattern; verify when running):
  Goa 2022 → slug "Goa2022"
  Goa 2017 → slug "goa2017"
  Goa 2012 → slug "goa2012"
  Goa 2007 → slug "goa2007"

Goa was added as the third tracked state per user request after Bihar.
40 MLAs per cycle so the all-candidates pull is very fast (~10 min).
"""
import logging
from app.scrapers.punjab import scrape_winners, scrape_all_candidates, WinnerRow

log = logging.getLogger(__name__)

GOA_CYCLES = [
    {"year": 2022, "slug": "Goa2022"},
    {"year": 2017, "slug": "goa2017"},
    {"year": 2012, "slug": "goa2012"},
    {"year": 2007, "slug": "goa2007"},
]


def scrape_all_goa() -> dict[int, list[WinnerRow]]:
    """Winners across all Goa assembly cycles."""
    return {c["year"]: scrape_winners(c["slug"]) for c in GOA_CYCLES}


def scrape_all_goa_candidates() -> dict[int, list[WinnerRow]]:
    """All candidates (winners + losers) across all Goa assembly cycles."""
    return {c["year"]: scrape_all_candidates(c["slug"]) for c in GOA_CYCLES}
