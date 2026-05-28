"""
SUPERSEDED — see app/scrapers/punjab.py for the actual generic implementation.

This file is intentionally empty. The functions `scrape_winners`,
`scrape_all_candidates`, and `_parse_candidate_table_row` in punjab.py
already work for any state, since myneta's HTML structure is identical
across state-assembly pages. State-specific config lives in app/states.py
and per-state scraper modules (e.g. app/scrapers/bihar.py).
"""
