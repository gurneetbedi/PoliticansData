"""
Minimal schema migration for development SQLite.

Adds the `age` and `profession` columns to the politicians table if they
don't already exist. Safe to run multiple times — duplicate-column errors
are caught and ignored.

Usage:  python -m app.migrate
"""
import logging
from sqlalchemy import text

from app.database import engine

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)


# (table, column, ddl_type)
COLUMNS = [
    ("politicians", "age", "INTEGER"),
    ("politicians", "profession", "VARCHAR(255)"),
]


def main():
    with engine.connect() as conn:
        for table, col, ddl in COLUMNS:
            try:
                conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {col} {ddl}"))
                conn.commit()
                log.info("Added %s.%s (%s)", table, col, ddl)
            except Exception as e:
                # SQLite raises OperationalError for duplicate column
                msg = str(e).lower()
                if "duplicate column" in msg or "already exists" in msg:
                    log.info("Column %s.%s already exists — skipping", table, col)
                else:
                    log.warning("Could not add %s.%s: %s", table, col, e)


if __name__ == "__main__":
    main()
