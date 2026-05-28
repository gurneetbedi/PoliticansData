"""
Bihar Assembly scraper.

Reuses the Punjab parser since myneta's table layout is identical across
state-assembly pages. Just configures the right cycle slugs.

Bihar cycles on myneta (confirmed 2026-05):
  Bihar 2025 → slug "Bihar2025"
  Bihar 2020 → slug "Bihar2020"
  Bihar 2015 → slug "bihar2015"
  Bihar 2010 → slug "bih2010"
  Bihar 2005 → slug "bih2005"

Bihar is added as the second tracked state because ADR's 2024 analysis
showed Bihar Assembly has the highest share of MLAs with declared
criminal cases (~67%) — making it the most analytically interesting state.
"""
import logging
from app.scrapers.punjab import (
    scrape_winners,
    scrape_all_candidates,
    WinnerRow,
)

log = logging.getLogger(__name__)

BIHAR_CYCLES = [
    {"year": 2025, "slug": "Bihar2025"},
    {"year": 2020, "slug": "Bihar2020"},
    {"year": 2015, "slug": "bihar2015"},
    {"year": 2010, "slug": "bih2010"},
    {"year": 2005, "slug": "bih2005"},
]


def scrape_all_bihar() -> dict[int, list[WinnerRow]]:
    """Scrape Bihar Assembly winners across all available cycles."""
    return {
        cycle["year"]: scrape_winners(cycle["slug"])
        for cycle in BIHAR_CYCLES
    }


def scrape_all_bihar_candidates() -> dict[int, list[WinnerRow]]:
    """Scrape every Bihar candidate (winners + losers) across all cycles."""
    return {
        cycle["year"]: scrape_all_candidates(cycle["slug"])
        for cycle in BIHAR_CYCLES
    }
