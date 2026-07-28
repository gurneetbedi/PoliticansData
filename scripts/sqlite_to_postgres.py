"""
One-time data loader: copy every row from your local SQLite DB into a remote
Postgres (e.g. Neon). Use this once after creating the Neon DB so you don't have
to re-scrape myneta.

Usage:
    # 1. Set the Neon connection string (postgres://... or postgresql://...)
    export DATABASE_URL="postgresql://user:pass@ep-xyz.neon.tech/lokvani?sslmode=require"

    # 2. Run it. The SQLite source path defaults to ./lokvani.db
    python scripts/sqlite_to_postgres.py

The script:
  - creates the schema in Postgres (CREATE TABLE IF NOT EXISTS)
  - copies every table in dependency order (states → parties → constituencies →
    elections → politicians → election_appearances → assets → criminal_cases)
  - uses chunked inserts so it works on Neon's free tier (no big-blob limits)
  - resets the Postgres sequences after import so new inserts don't collide
  - is idempotent: skips rows whose primary key already exists in Postgres

Re-running is safe — it will not duplicate existing rows.
"""
import os
import sys
from pathlib import Path

# Make the project root importable so `from app.models import Base` works
# regardless of where the user ran the script from (root, scripts/, anywhere).
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from sqlalchemy import create_engine, MetaData, text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import sessionmaker

# Default SQLite path is relative to the project root, not the cwd.
SQLITE_PATH = os.getenv("SQLITE_PATH", str(PROJECT_ROOT / "lokvani.db"))
PG_URL      = os.getenv("DATABASE_URL")

if not PG_URL:
    sys.exit("ERROR: set DATABASE_URL to your Neon connection string first.")

# Normalize for SQLAlchemy 2.x
if PG_URL.startswith("postgres://"):
    PG_URL = PG_URL.replace("postgres://", "postgresql://", 1)

RESET = "--reset" in sys.argv

print(f"Source SQLite: {SQLITE_PATH}")
print(f"Target Postgres: {PG_URL.split('@')[-1]}")  # don't print credentials
if RESET:
    print("RESET MODE: existing tables will be dropped before reload.")

src_engine = create_engine(f"sqlite:///{SQLITE_PATH}")
dst_engine = create_engine(PG_URL)

# 1. (Optional) wipe the existing schema. Use this after model changes so the new
#    column types take effect — create_all() does NOT alter existing tables.
from app.models import Base       # noqa: E402  (import after engine setup)
import app.gpi_models             # noqa: E402,F401  registers GPI tables on Base
if RESET:
    Base.metadata.drop_all(dst_engine)
    print("Dropped existing tables on Postgres.")

# 2. Create (or recreate) the schema on the destination.
# Because we imported gpi_models above, this also creates:
#   gpi_pillars, gpi_sources, gpi_indicators, gpi_indicator_values,
#   gpi_pillar_scores, gpi_cag_extractions, gpi_cag_pa_extractions, gpi_scores
Base.metadata.create_all(dst_engine)
print("Schema created on Postgres.")

# 2. Reflect the source so we know which tables exist + dependency order
src_meta = MetaData()
src_meta.reflect(bind=src_engine)

# Sort tables in foreign-key dependency order (parents before children)
table_order = src_meta.sorted_tables

SrcSession = sessionmaker(bind=src_engine)
DstSession = sessionmaker(bind=dst_engine)
src = SrcSession()
dst = DstSession()

CHUNK = 500
import time

# We resolve target-side Table objects from the destination Postgres schema so
# pg_insert() emits the correct dialect SQL. The source-reflected tables only
# give us column *names*; we need the real Postgres Table objects for the
# .on_conflict_do_nothing() clause to work.
dst_meta = MetaData()
dst_meta.reflect(bind=dst_engine)

for table in table_order:
    name = table.name
    rows = src.execute(table.select()).mappings().all()
    if not rows:
        print(f"  {name:<25} empty, skipped")
        continue

    dst_table = dst_meta.tables.get(name)
    if dst_table is None:
        print(f"  {name:<25} MISSING on destination, skipped")
        continue

    # Print the table name + leave the line open so we can append progress dots.
    # `flush=True` forces the terminal to show output immediately instead of
    # waiting for a newline (otherwise the dots wouldn't appear in real time).
    total_batches = (len(rows) + CHUNK - 1) // CHUNK
    print(f"  {name:<25} ", end="", flush=True)

    inserted = 0
    t0 = time.time()
    for i in range(0, len(rows), CHUNK):
        batch = [dict(r) for r in rows[i:i+CHUNK]]
        # pg_insert(...).values([dict, dict, ...]) generates a SINGLE INSERT
        # with all rows in one VALUES clause — one network round-trip per batch
        # instead of one per row. ~50–100x faster against a remote DB.
        stmt = pg_insert(dst_table).values(batch).on_conflict_do_nothing()
        dst.execute(stmt)
        dst.commit()
        inserted += len(batch)
        print(".", end="", flush=True)  # one dot per 500-row batch

    # Pad short table names so the row counts line up; show elapsed seconds too.
    pad = " " * max(0, 50 - total_batches)
    print(f"{pad} {inserted:>6,} rows  ({time.time() - t0:.1f}s)")

# 3. Reset Postgres sequences so future INSERTs don't try to reuse IDs.
# Each reset runs on its OWN connection so a failure on one table
# (e.g. it doesn't exist on the destination) doesn't abort the whole
# transaction and cascade-fail every subsequent table.
print("Resetting sequences...")
for table in table_order:
    # Skip tables that only live on the source SQLite side (e.g. the
    # eci_candidates_provisional ingestion staging table isn't part
    # of the production Postgres schema at all).
    if table.name not in dst_meta.tables:
        continue
    try:
        with dst_engine.connect() as conn:
            conn.execute(text(
                f"SELECT setval(pg_get_serial_sequence('{table.name}', 'id'), "
                f"COALESCE((SELECT MAX(id) FROM {table.name}), 1))"
            ))
            conn.commit()
    except Exception as e:
        # Log short reason (not the full SQL blob) so the output stays readable
        msg = str(e).splitlines()[0][:120]
        print(f"  sequence reset skipped for {table.name}: {msg}")

print("\nDone. Visit your Render service — the data should now be live.")
