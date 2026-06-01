"""
One-time data loader: copy every row from your local SQLite DB into a remote
Postgres (e.g. Neon). Use this once after creating the Neon DB so you don't have
to re-scrape myneta.

Usage:
    # 1. Set the Neon connection string (postgres://... or postgresql://...)
    export DATABASE_URL="postgresql://user:pass@ep-xyz.neon.tech/politrack?sslmode=require"

    # 2. Run it. The SQLite source path defaults to ./politrack.db
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
from sqlalchemy import create_engine, MetaData, text
from sqlalchemy.orm import sessionmaker

SQLITE_PATH = os.getenv("SQLITE_PATH", "./politrack.db")
PG_URL      = os.getenv("DATABASE_URL")

if not PG_URL:
    sys.exit("ERROR: set DATABASE_URL to your Neon connection string first.")

# Normalize for SQLAlchemy 2.x
if PG_URL.startswith("postgres://"):
    PG_URL = PG_URL.replace("postgres://", "postgresql://", 1)

print(f"Source SQLite: {SQLITE_PATH}")
print(f"Target Postgres: {PG_URL.split('@')[-1]}")  # don't print credentials

src_engine = create_engine(f"sqlite:///{SQLITE_PATH}")
dst_engine = create_engine(PG_URL)

# 1. Create the schema on the destination
from app.models import Base  # noqa: E402  (import after engine setup)
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
for table in table_order:
    name = table.name
    rows = src.execute(table.select()).mappings().all()
    if not rows:
        print(f"  {name}: empty, skipped")
        continue

    # Insert with ON CONFLICT DO NOTHING so re-runs don't crash on duplicate PKs.
    cols = ", ".join(rows[0].keys())
    placeholders = ", ".join(f":{k}" for k in rows[0].keys())
    sql = text(
        f"INSERT INTO {name} ({cols}) VALUES ({placeholders}) "
        f"ON CONFLICT DO NOTHING"
    )
    inserted = 0
    for i in range(0, len(rows), CHUNK):
        batch = [dict(r) for r in rows[i:i+CHUNK]]
        dst.execute(sql, batch)
        dst.commit()
        inserted += len(batch)
    print(f"  {name}: {inserted} rows")

# 3. Reset Postgres sequences so future INSERTs don't try to reuse IDs.
print("Resetting sequences...")
with dst_engine.connect() as conn:
    for table in table_order:
        # Postgres auto-creates a sequence called <table>_id_seq for SERIAL columns.
        # setval to MAX(id) so the next insert uses MAX+1.
        try:
            conn.execute(text(
                f"SELECT setval(pg_get_serial_sequence('{table.name}', 'id'), "
                f"COALESCE((SELECT MAX(id) FROM {table.name}), 1))"
            ))
            conn.commit()
        except Exception as e:
            print(f"  sequence reset skipped for {table.name}: {e}")

print("\nDone. Visit your Render service — the data should now be live.")
