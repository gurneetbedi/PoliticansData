"""
Find politicians with empty or whitespace-only names and try to repair them.

Strategy:
  1. Re-run the Punjab AC scraper. Its on-disk cache makes this fast.
  2. For every WinnerRow whose candidate_id matches a politician in the DB
     with an empty name, copy the name across.
  3. Print a summary.

Safe to run multiple times. If the scraper is also unable to find a name
(e.g., a corrupted row), the politician is left with the Politician.display_name
fallback ("Candidate #<id>") which renders safely in the UI.

Usage:  python -m app.repair_names
"""
import logging

from app.database import SessionLocal
from app.models import Politician
from app.scrapers.punjab import scrape_all_punjab
from app.scrapers.punjab_ls import scrape_all_punjab_ls

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)


def main():
    session = SessionLocal()
    try:
        broken = (
            session.query(Politician)
            .filter((Politician.name == "") | (Politician.name.is_(None)))
            .all()
        )
        log.info("Found %d politicians with empty names", len(broken))
        if not broken:
            return

        by_cand_id: dict[int, Politician] = {p.myneta_candidate_id: p for p in broken if p.myneta_candidate_id}
        if not by_cand_id:
            log.warning("No empty-name politicians have a myneta_candidate_id — can't repair from cache")
            return

        # Re-scrape (fast — uses on-disk cache) and find matching names
        repaired = 0
        for year, winners in scrape_all_punjab().items():
            for row in winners:
                p = by_cand_id.get(row.candidate_id)
                if p and row.name and row.name.strip():
                    log.info("Repairing id=%d cand_id=%d -> %r", p.id, p.myneta_candidate_id, row.name)
                    p.name = row.name.strip()
                    repaired += 1

        # Also check LS data
        try:
            for year, winners in scrape_all_punjab_ls().items():
                for row in winners:
                    p = by_cand_id.get(row.candidate_id)
                    if p and not p.name and row.name and row.name.strip():
                        log.info("Repairing (LS) id=%d cand_id=%d -> %r", p.id, p.myneta_candidate_id, row.name)
                        p.name = row.name.strip()
                        repaired += 1
        except Exception as e:
            log.info("Skipping LS pass: %s", e)

        session.commit()
        log.info("Done. Repaired %d names.", repaired)

        # Anything still broken?
        still_broken = (
            session.query(Politician)
            .filter((Politician.name == "") | (Politician.name.is_(None)))
            .count()
        )
        if still_broken:
            log.warning(
                "%d politicians still have empty names. They will display as "
                "'Candidate #<id>' in the UI via Politician.display_name.",
                still_broken,
            )
    finally:
        session.close()


if __name__ == "__main__":
    main()
