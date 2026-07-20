"""Which states are in the eci_candidates_provisional table vs the
canonical (states + politicians + election_appearances) tables?

If UP is in provisional but not in the canonical states table, we just
need to re-run scripts/migrate_to_eci_only.py — that copies provisional
data into the canonical tables (creates state, constituencies,
politicians, appearances all in one shot).

If UP isn't in provisional either, we need to run
scripts/load_eci_to_db.py first to populate the provisional row from
the extracted CSV.
"""
import os
import sqlalchemy
from sqlalchemy import text
from sqlalchemy.orm import sessionmaker

url = os.environ.get('DATABASE_URL', 'sqlite:///lokvani.db')
engine = sqlalchemy.create_engine(url)
sess = sessionmaker(bind=engine)()

print("=== States in canonical states table ===")
for row in sess.execute(text("SELECT name FROM states ORDER BY name")).fetchall():
    print(f"  {row[0]}")

print("\n=== States + years in eci_candidates_provisional ===")
for row in sess.execute(text(
    "SELECT state, election_year, COUNT(*) FROM eci_candidates_provisional "
    "GROUP BY state, election_year ORDER BY state, election_year"
)).fetchall():
    print(f"  {row[0]:<25s}  {row[1]}  ({row[2]} rows)")

print("\n=== Key check: is 'Uttar Pradesh' in provisional? ===")
for row in sess.execute(text(
    "SELECT COUNT(*) FROM eci_candidates_provisional "
    "WHERE state = 'Uttar Pradesh' AND election_year = 2022"
)).fetchall():
    print(f"  Uttar Pradesh 2022 provisional rows: {row[0]}")
