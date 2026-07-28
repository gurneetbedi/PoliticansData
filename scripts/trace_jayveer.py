"""Show all DB election_appearances that could match JAYVEER SINGH /
Mainpuri (aff_id 4579), plus provisional rows, plus Gemini extraction
side-by-side to see where the mismatch is."""
import sqlite3
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
con = sqlite3.connect(str(ROOT / "lokvani.db"))
cur = con.cursor()

# 1. Provisional row(s) for aff_id 4579
print("=== Provisional (aff_id 4579) ===")
cur.execute("""
    SELECT affidavit_id, candidate_name, constituency, party
    FROM eci_candidates_provisional
    WHERE state='Uttar Pradesh' AND election_year=2022 AND affidavit_id='4579'
""")
for r in cur.fetchall():
    print(f"  aff={r[0]} name={r[1]!r} const={r[2]!r} party={r[3]!r}")

# 2. All appearance rows with 'jayveer' or 'jayaveer' in name in Mainpuri
print("\n=== DB appearances with 'jay' + Mainpuri ===")
cur.execute("""
    SELECT ea.id, p.name, c.name, par.short_name, par.full_name, ea.total_assets_inr
    FROM election_appearances ea
    JOIN politicians p ON ea.politician_id = p.id
    JOIN constituencies c ON ea.constituency_id = c.id
    JOIN elections e ON ea.election_id = e.id
    JOIN states s ON c.state_id = s.id
    LEFT JOIN parties par ON ea.party_id = par.id
    WHERE s.name='Uttar Pradesh' AND e.year=2022
      AND LOWER(c.name) LIKE '%mainpuri%'
      AND (LOWER(p.name) LIKE '%jayveer%' OR LOWER(p.name) LIKE '%jayaveer%'
           OR LOWER(p.name) LIKE '%jaiveer%' OR LOWER(p.name) LIKE '%jai veer%')
""")
rows = cur.fetchall()
for r in rows:
    print(f"  id={r[0]}  name={r[1]!r}  const={r[2]!r}  short={r[3]!r}  full={r[4]!r}  wealth={r[5]}")

if not rows:
    # Fallback — anyone with 'singh' in Mainpuri
    print("\n  (no jay* match; showing all Mainpuri rows to see what's actually there)")
    cur.execute("""
        SELECT ea.id, p.name, c.name, par.short_name, ea.total_assets_inr
        FROM election_appearances ea
        JOIN politicians p ON ea.politician_id = p.id
        JOIN constituencies c ON ea.constituency_id = c.id
        JOIN elections e ON ea.election_id = e.id
        JOIN states s ON c.state_id = s.id
        LEFT JOIN parties par ON ea.party_id = par.id
        WHERE s.name='Uttar Pradesh' AND e.year=2022
          AND LOWER(c.name) LIKE '%mainpuri%'
    """)
    for r in cur.fetchall():
        print(f"    id={r[0]}  name={r[1]!r}  const={r[2]!r}  short={r[3]!r}  wealth={r[4]}")

# 3. Also check by aff_id 4579 in manifest
print("\n=== Manifest row for aff_id 4579 ===")
mf = ROOT / "data/eci/raw_pdfs/uttarpradesh-2022/manifest.jsonl"
for line in mf.read_text().splitlines():
    try:
        r = json.loads(line)
    except:
        continue
    if str(r.get('affidavit_id','')) == '4579':
        print(f"  name={r.get('name')!r} const={r.get('constituency')!r} party={r.get('party')!r}")
        break
