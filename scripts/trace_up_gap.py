"""Pick the first UP 2022 gap and print everything about its state
across manifest / provisional / DB / Gemini extraction, so we can see
EXACTLY what's blocking the match.
"""
import csv
import json
import re
import sqlite3
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# Load first gap from CSV
csv_path = ROOT / "data/reports/gaps_uttarpradesh_2022.csv"
row = next(csv.DictReader(csv_path.open()))
pdf = row["pdf"]
stem = pdf[:-4]
print(f"Investigating: {pdf}")
print(f"CSV row: {row}\n")

# Gemini extraction
lx = ROOT / f"data/eci/for_ai/llm_extracted/uttarpradesh_2022/{stem}.json"
if lx.exists():
    d = json.loads(lx.read_text())
    aff_id = str(d.get("affidavit_id"))
    ext = d.get("extraction") or {}
    print(f"Gemini file:      {stem}.json")
    print(f"  affidavit_id:   {aff_id}")
    print(f"  identity.name:  {(ext.get('identity') or {}).get('name_in_english')!r}")
    print(f"  political:      {ext.get('political')!r}")
else:
    aff_id = ""
    print(f"✗ No Gemini file: {lx}")

# Manifest row
mf = ROOT / "data/eci/raw_pdfs/uttarpradesh-2022/manifest.jsonl"
mf_row = None
for line in mf.read_text().splitlines():
    try:
        r = json.loads(line)
    except:
        continue
    if str(r.get("affidavit_id","")) == aff_id:
        mf_row = r
        break
print(f"\nManifest row:")
if mf_row:
    print(f"  name:  {mf_row.get('name')!r}")
    print(f"  const: {mf_row.get('constituency')!r}")
    print(f"  party: {mf_row.get('party')!r}")
else:
    print(f"  (none — aff_id {aff_id} not in UP manifest)")

# SQLite queries
con = sqlite3.connect(str(ROOT / "lokvani.db"))
cur = con.cursor()

# Provisional
cur.execute("""
    SELECT candidate_name, constituency, party FROM eci_candidates_provisional
    WHERE state='Uttar Pradesh' AND election_year=2022 AND affidavit_id=?
""", (aff_id,))
r = cur.fetchone()
print(f"\nProvisional row:")
if r:
    print(f"  candidate_name: {r[0]!r}")
    print(f"  constituency:   {r[1]!r}")
    print(f"  party:          {r[2]!r}")
else:
    print(f"  (none)")

# All DB appearance rows matching this (name, const)
name = mf_row.get('name') if mf_row else ''
const = mf_row.get('constituency') if mf_row else ''
const_bare = re.sub(r"\s*\(S[CT]\)\s*$", "", const, flags=re.IGNORECASE).strip()
tokens = [t for t in name.split() if len(t) >= 3]
cur.execute("""
    SELECT ea.id, p.name, c.name, par.short_name, par.full_name, ea.total_assets_inr
    FROM election_appearances ea
    JOIN politicians p ON ea.politician_id = p.id
    JOIN constituencies c ON ea.constituency_id = c.id
    JOIN elections e ON ea.election_id = e.id
    JOIN states s ON c.state_id = s.id
    LEFT JOIN parties par ON ea.party_id = par.id
    WHERE s.name='Uttar Pradesh' AND e.year=2022
      AND LOWER(c.name) LIKE LOWER(?)
      AND (""" + " OR ".join(f"LOWER(p.name) LIKE LOWER(?)" for _ in tokens) + """)
""", [f"%{const_bare}%"] + [f"%{t}%" for t in tokens])

print(f"\nAll UP 2022 appearance rows matching const~'{const_bare}' + any-name-token:")
found_any = False
for row in cur.fetchall():
    found_any = True
    print(f"  id={row[0]}  name={row[1]!r}  const={row[2]!r}  short={row[3]!r}  full={row[4]!r}  wealth={row[5]}")
if not found_any:
    print(f"  (none)")
