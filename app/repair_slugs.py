"""
One-off repair: regenerate slugs for any politicians whose slug is empty,
NULL, or duplicated. Safe to run multiple times.

Usage:  python -m app.repair_slugs
"""
import logging
from slugify import slugify

from app.database import SessionLocal
from app.models import Politician

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)


def unique_slug(session, base: str, exclude_id: int | None = None) -> str:
    """Find a slug starting with `base` that isn't already used."""
    slug = base
    n = 1
    while True:
        q = session.query(Politician).filter(Politician.slug == slug)
        if exclude_id is not None:
            q = q.filter(Politician.id != exclude_id)
        if not q.first():
            return slug
        n += 1
        slug = f"{base}-{n}"


def repair():
    session = SessionLocal()
    fixed = 0
    try:
        # Find politicians whose slug is empty, None, or otherwise broken.
        broken = (
            session.query(Politician)
            .filter((Politician.slug == "") | (Politician.slug.is_(None)))
            .all()
        )
        log.info("Found %d politicians with empty/null slugs", len(broken))

        for p in broken:
            base = slugify(p.name or f"politician-{p.id}")[:200]
            if not base:
                # Name slugified to nothing (e.g., only punctuation); fall back to ID
                base = f"politician-{p.id}"
            new_slug = unique_slug(session, base, exclude_id=p.id)
            log.info("Repairing id=%d name=%r  '' -> %r", p.id, p.name, new_slug)
            p.slug = new_slug
            fixed += 1

        # Also catch duplicate slugs that may have snuck in from older runs.
        from sqlalchemy import func
        dupes = (
            session.query(Politician.slug, func.count(Politician.id).label("c"))
            .group_by(Politician.slug)
            .having(func.count(Politician.id) > 1)
            .all()
        )
        for slug_val, count in dupes:
            rows = session.query(Politician).filter(Politician.slug == slug_val).all()
            # Keep the first row's slug, regenerate the rest.
            for p in rows[1:]:
                base = slugify(p.name or f"politician-{p.id}")[:200]
                new_slug = unique_slug(session, base, exclude_id=p.id)
                log.info("De-duplicating id=%d  %r -> %r", p.id, slug_val, new_slug)
                p.slug = new_slug
                fixed += 1

        session.commit()
        log.info("Done. Repaired %d politicians.", fixed)
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


if __name__ == "__main__":
    repair()
