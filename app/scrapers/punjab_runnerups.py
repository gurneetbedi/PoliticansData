"""
Punjab runner-up scraper (STUB).

The goal: for each Punjab assembly constituency in each cycle, identify the
candidate who came second (the runner-up). This is *not* available in the
winners-list URL we already scrape — we need to drill into either:

  1. The per-constituency results page on myneta:
     https://myneta.info/<slug>/index.php?action=show_constituency_candidates&const_id=<id>

  2. Or the individual candidate detail pages which include vote share,
     and aggregate across all candidates per constituency.

Status: NOT IMPLEMENTED. This module is shipped as a placeholder so the
homepage tooltip code can branch on "runner-up data available?" without
crashing. To complete:

  1. Inspect the URL pattern for per-constituency candidate listings on myneta
     (open a single constituency page in your browser and copy the URL).
  2. Fill in `scrape_runnerups_for_cycle()` below, returning rows of
     {constituency, name, party, votes_received, vote_share_pct, year}.
  3. Write the results to app/static/punjab_runnerups.json.

Until then, the homepage tooltip checks the JSON file and shows runner-up
info only when present, otherwise hides that section gracefully.
"""
import json
import logging
from pathlib import Path

log = logging.getLogger(__name__)

OUT = Path(__file__).resolve().parent.parent / "static" / "punjab_runnerups.json"


def scrape_runnerups_for_cycle(election_slug: str) -> list[dict]:
    """
    TODO: Implement scraping for runner-up data.

    Return a list of dicts:
      [{
        "constituency": "ABOHAR",
        "name": "Some Candidate",
        "party": "AAP",
        "votes_received": 12345,
        "vote_share_pct": 28.5,
        "year": 2022,
      }, ...]
    """
    log.warning("Runner-up scraper not implemented yet for %s", election_slug)
    return []


def write_runnerups(rows: list[dict]):
    """Write a runner-up dataset to the static JSON the homepage reads."""
    by_const = {}
    for r in rows:
        key = r["constituency"].upper().replace("(SC)", "").replace("(ST)", "").strip()
        by_const[key] = r
    OUT.write_text(json.dumps(by_const, indent=2))
    log.info("Wrote %d runner-up rows to %s", len(by_const), OUT)


if __name__ == "__main__":
    print("Runner-up scraper is a stub. See module docstring for implementation guidance.")
