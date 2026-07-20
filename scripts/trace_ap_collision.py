"""Trace one AP collision case to see why the matcher fix isn't landing.
Picks the first AP candidate with 'gemini_ok_but_apply' status and shows:
  - What party is in the manifest / provisional
  - What party the DB election_appearances have for that (name, const)
  - Whether they match after normalization
"""
import json
import re
import sqlite3
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# Pick a known collision case from the earlier diagnose output
STEM = "ANANDA_BABU_NAKKA__2624"

# Load Gemini extraction to get aff_id
lx = ROOT / f"data/eci/for_ai/llm_extracted/andhrapradesh_2024/{STEM}.json"
lxd = json.loads(lx.read_text())
aff_id = str(lxd.get("affidavit_id"))
print(f"Gemini file: {STEM}.json")
print(f"  affidavit_id: {aff_id}\n")

# Load manifest for the candidate
mf = ROOT / "data/eci/raw_pdfs/andhrapradesh-2024/manifest.jsonl"
mf_row = None
for line in mf.read_text().splitlines():
    try:
        r = json.loads(line)
    except:
        continue
    if str(r.get("affidavit_id","")) == aff_id:
        mf_row = r
        break

if not mf_row:
    print(f"  ✗ No manifest match for aff_id {aff_id}")
else:
    print(f"Manifest row:")
    print(f"  name:  {mf_row.get('name')!r}")
    print(f"  const: {mf_row.get('constituency')!r}")
    print(f"  party: {mf_row.get('party')!r}")

# Query SQLite: what's in provisional?
con = sqlite3.connect(str(ROOT / "lokvani.db"))
cur = con.cursor()
try:
    cur.execute("""
        SELECT candidate_name, constituency, party
        FROM eci_candidates_provisional
        WHERE affidavit_id = ? AND state = 'Andhra Pradesh' AND election_year = 2024
    """, (aff_id,))
    r = cur.fetchone()
    if r:
        print(f"\nProvisional row:")
        print(f"  candidate_name: {r[0]!r}")
        print(f"  constituency:   {r[1]!r}")
        print(f"  party:          {r[2]!r}")
    else:
        print(f"\n✗ No provisional row for aff_id={aff_id}")
except sqlite3.OperationalError as e:
    print(f"\n(no provisional table or column: {e})")

# Query all election_appearances matching (name, const) for AP — LIKE style
name = mf_row.get('name') if mf_row else ''
const = mf_row.get('constituency') if mf_row else ''
# Strip SC/ST suffix from const for matching
const_bare = re.sub(r"\s*\(S[CT]\)\s*$", "", const, flags=re.IGNORECASE).strip()

# Name — split into tokens, match on last token (surname) LIKE
name_tokens = [t for t in name.split() if len(t) >= 3]
last_tok = name_tokens[-1] if name_tokens else ""

cur.execute("""
    SELECT ea.id, p.name, c.name, par.short_name, ea.total_assets_inr
    FROM election_appearances ea
    JOIN politicians p ON ea.politician_id = p.id
    JOIN constituencies c ON ea.constituency_id = c.id
    JOIN elections e ON ea.election_id = e.id
    JOIN states s ON c.state_id = s.id
    LEFT JOIN parties par ON ea.party_id = par.id
    WHERE s.name = 'Andhra Pradesh' AND e.year = 2024
      AND (LOWER(p.name) LIKE LOWER(?) OR LOWER(p.name) LIKE LOWER(?))
      AND (LOWER(c.name) LIKE LOWER(?) OR LOWER(c.name) LIKE LOWER(?))
""", (f"%{last_tok}%", f"%{name.split()[0]}%",
      f"%{const_bare}%", f"%{const}%"))
print(f"\nDB election_appearances matching name~'{name}' const~'{const_bare}':")
rows = cur.fetchall()
if not rows:
    print(f"  (none found)")
    # Show ANY AP 2024 row that has 'Vemuru' in the name for reference
    cur.execute("""
        SELECT ea.id, p.name, c.name, par.short_name, ea.total_assets_inr
        FROM election_appearances ea
        JOIN politicians p ON ea.politician_id = p.id
        JOIN constituencies c ON ea.constituency_id = c.id
        JOIN elections e ON ea.election_id = e.id
        JOIN states s ON c.state_id = s.id
        LEFT JOIN parties par ON ea.party_id = par.id
        WHERE s.name = 'Andhra Pradesh' AND e.year = 2024
          AND LOWER(c.name) LIKE '%vemuru%'
        LIMIT 5
    """)
    print(f"\nAny AP 2024 row with 'vemuru' in constituency name:")
    for row in cur.fetchall():
        print(f"  id={row[0]}  name={row[1]!r}  const={row[2]!r}  party={row[3]!r}  wealth={row[4]}")
else:
    for row in rows:
        print(f"  id={row[0]}  name={row[1]!r}  const={row[2]!r}  party={row[3]!r}  wealth={row[4]}")
