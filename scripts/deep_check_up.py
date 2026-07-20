"""Deep check — where does UP data live in the DB?
Query every table that could plausibly have UP data.
"""
import os
import sqlalchemy
from sqlalchemy import text
from sqlalchemy.orm import sessionmaker

url = os.environ.get('DATABASE_URL', 'sqlite:///lokvani.db')
engine = sqlalchemy.create_engine(url)
sess = sessionmaker(bind=engine)()

print("=== 1. Any state name containing 'utt' (case-insensitive) ===")
for row in sess.execute(text(
    "SELECT id, name FROM states WHERE LOWER(name) LIKE '%utt%' ORDER BY name"
)).fetchall():
    print(f"  id={row[0]}  name={row[1]!r}")

print("\n=== 2. Any constituency in a state matching 'utt' ===")
for row in sess.execute(text("""
    SELECT c.id, c.name, s.name FROM constituencies c
    JOIN states s ON c.state_id = s.id
    WHERE LOWER(s.name) LIKE '%utt%' LIMIT 10
""")).fetchall():
    print(f"  const='{row[1]}' state='{row[2]}'")

print("\n=== 3. Any politician linked to a UP-like state ===")
for row in sess.execute(text("""
    SELECT p.id, p.name, s.name AS state, ea.total_assets_inr, e.year
    FROM politicians p
    JOIN election_appearances ea ON ea.politician_id = p.id
    JOIN elections e ON ea.election_id = e.id
    JOIN constituencies c ON ea.constituency_id = c.id
    JOIN states s ON c.state_id = s.id
    WHERE LOWER(s.name) LIKE '%utt%pradesh%'
    LIMIT 5
""")).fetchall():
    print(f"  {row[1]!r} state={row[2]!r} year={row[4]} wealth={row[3]}")

print("\n=== 4. Total row counts per table ===")
for table in ("states", "elections", "constituencies", "politicians",
              "election_appearances", "parties"):
    n = sess.execute(text(f"SELECT COUNT(*) FROM {table}")).scalar()
    print(f"  {table:<25s} {n:>8d}")

print("\n=== 5. All 2022 elections in DB ===")
for row in sess.execute(text("""
    SELECT e.id, e.year, e.house, s.name AS state
    FROM elections e JOIN states s ON e.state_id = s.id
    WHERE e.year = 2022 ORDER BY s.name
""")).fetchall():
    print(f"  id={row[0]}  {row[1]}  {row[2]}  {row[3]}")

print("\n=== 6. Search for any Yogi Adityanath (well-known UP MLA) ===")
for row in sess.execute(text("""
    SELECT p.name, s.name AS state, c.name AS const, e.year
    FROM politicians p
    LEFT JOIN election_appearances ea ON ea.politician_id = p.id
    LEFT JOIN elections e ON ea.election_id = e.id
    LEFT JOIN constituencies c ON ea.constituency_id = c.id
    LEFT JOIN states s ON c.state_id = s.id
    WHERE LOWER(p.name) LIKE '%yogi%' OR LOWER(p.name) LIKE '%adityanath%'
    LIMIT 5
""")).fetchall():
    print(f"  {row[0]!r}  state={row[1]!r}  const={row[2]!r}  year={row[3]}")

print("\n=== 7. Any politician linked to constituencies without a state? ===")
n = sess.execute(text("""
    SELECT COUNT(*) FROM constituencies WHERE state_id IS NULL
""")).scalar()
print(f"  Constituencies with NULL state_id: {n}")
