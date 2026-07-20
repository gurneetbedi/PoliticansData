"""Why does state_coverage_report show UP 2022 with 0 in DB even though
apply_llm_extraction committed the cycle? Investigates the mismatch by
comparing:
  - The state name my report queries with (via NAMES map)
  - The actual state name(s) UP is stored under in the DB
  - Sample name/constituency normalization for UP candidates
"""
import os
import sqlalchemy
from sqlalchemy import text
from sqlalchemy.orm import sessionmaker
import json
from pathlib import Path

url = os.environ.get('DATABASE_URL', 'sqlite:///lokvani.db')
engine = sqlalchemy.create_engine(url)
sess = sessionmaker(bind=engine)()

print(f'DB: {url[:60]}...\n')

print('=== States that contain "utt" or "pradesh" ===')
for row in sess.execute(text(
    "SELECT DISTINCT name FROM states WHERE LOWER(name) LIKE '%utt%' "
    "OR LOWER(name) LIKE '%pradesh%' ORDER BY name")).fetchall():
    print(f'  {row[0]!r}')

print('\n=== Count election_appearances for possible UP state names, 2022 ===')
for s in ("Uttar Pradesh", "UTTAR PRADESH", "Uttar pradesh", "UP",
          "Uttarakhand", "Uttar Pradesh "):
    r = sess.execute(text("""
        SELECT COUNT(*) FROM election_appearances ea
        JOIN elections e ON ea.election_id = e.id
        JOIN constituencies c ON ea.constituency_id = c.id
        JOIN states s ON c.state_id = s.id
        WHERE s.name = :s AND e.year = :y
    """), {"s": s, "y": 2022}).scalar()
    if r:
        print(f'  s.name={s!r:<25s} year=2022  →  {r} rows')

print('\n=== Sample UP 2022 election_appearances (top 5 with wealth) ===')
r = sess.execute(text("""
    SELECT p.name, c.name, s.name AS state, ea.total_assets_inr
    FROM election_appearances ea
    JOIN politicians p ON ea.politician_id = p.id
    JOIN elections e ON ea.election_id = e.id
    JOIN constituencies c ON ea.constituency_id = c.id
    JOIN states s ON c.state_id = s.id
    WHERE e.year = 2022 AND ea.total_assets_inr IS NOT NULL
      AND (LOWER(s.name) LIKE '%uttar%' OR LOWER(s.name) LIKE '%pradesh%')
    LIMIT 5
""")).fetchall()
for row in r:
    print(f'  p.name={row[0]!r}  c.name={row[1]!r}  state={row[2]!r}  wealth={row[3]}')

# Also count total 2022 rows with wealth to see if apply worked
r = sess.execute(text("""
    SELECT COUNT(*) FROM election_appearances ea
    JOIN elections e ON ea.election_id = e.id
    WHERE e.year = 2022 AND ea.total_assets_inr IS NOT NULL
""")).scalar()
print(f'\n=== Total 2022 appearances WITH wealth in DB: {r} ===')

print('\n=== Sample manifest UP names for comparison ===')
mf_path = Path('data/eci/raw_pdfs/uttarpradesh-2022/manifest.jsonl')
for line in mf_path.read_text().splitlines()[:5]:
    r = json.loads(line)
    print(f'  manifest.name={r.get("name")!r}  manifest.constituency={r.get("constituency")!r}  manifest.state={r.get("state")!r}')
