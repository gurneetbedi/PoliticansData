"""Query the LOCAL SQLite lokvani.db (ignores DATABASE_URL) so we see
exactly what the running site sees.

Prints: per-state coverage — allowlist size, in-DB rows, rows with wealth,
rows with education. Sorted by biggest gap first.
"""
import re
import sqlite3
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DB_PATH = ROOT / "lokvani.db"
if not DB_PATH.exists():
    raise SystemExit(f"No SQLite at {DB_PATH}")

con = sqlite3.connect(str(DB_PATH))
cur = con.cursor()


def find_allowlist(slug_year: str):
    d = ROOT / "data/allowlists"
    p = d / f"{slug_year}.txt"
    if p.exists():
        return p
    m = sorted(d.glob(f"{slug_year}_top*.txt"))
    return m[0] if m else None


def _norm(s):
    """Name normalizer — for candidate + party names."""
    return re.sub(r"[^a-z0-9]+", "", (s or "").lower())


def _norm_const(name: str) -> str:
    """Constituency normalizer — MUST match apply_llm_extraction.py's
    _normalize_constituency so the counts align with what apply landed.
    Strips (SC)/(ST) suffixes before non-alnum stripping."""
    if not name:
        return ""
    s = name.upper().strip()
    for suf in ("(SC)", "(ST)", " SC", " ST"):
        if s.endswith(suf):
            s = s[: -len(suf)].strip()
    s = re.sub(r"[^A-Z0-9]+", "", s)
    _ALIASES = {"NARELA": "NERELA"}
    return _ALIASES.get(s, s).lower()


NAMES = {
    "andhrapradesh": "Andhra Pradesh", "arunachal": "Arunachal Pradesh",
    "assam": "Assam", "bihar": "Bihar", "chhattisgarh": "Chhattisgarh",
    "delhi": "Delhi", "goa": "Goa", "gujarat": "Gujarat",
    "haryana": "Haryana", "himachal": "Himachal Pradesh",
    "jharkhand": "Jharkhand", "jk": "Jammu and Kashmir",
    "karnataka": "Karnataka", "kerala": "Kerala",
    "madhyapradesh": "Madhya Pradesh", "maharashtra": "Maharashtra",
    "manipur": "Manipur", "meghalaya": "Meghalaya",
    "mizoram": "Mizoram", "nagaland": "Nagaland", "odisha": "Odisha",
    "puducherry": "Puducherry", "punjab": "Punjab",
    "rajasthan": "Rajasthan", "sikkim": "Sikkim",
    "tamilnadu": "Tamil Nadu", "telangana": "Telangana",
    "tripura": "Tripura", "uttarakhand": "Uttarakhand",
    "uttarpradesh": "Uttar Pradesh", "westbengal": "West Bengal",
}

# All state cycles from raw_pdfs/
RAW_ROOT = ROOT / "data/eci/raw_pdfs"
cycles = sorted(d.name for d in RAW_ROOT.iterdir()
                if d.is_dir() and (d / "manifest.jsonl").exists())

print(f"SQLite: {DB_PATH.name} ({DB_PATH.stat().st_size / 1024 / 1024:.1f} MB)\n")

print(f"{'State':<22s} {'Year':>4s}  {'Allow':>6s}  {'In DB':>6s}  "
      f"{'Wealth':>7s}  {'Miss W':>7s}  {'%W':>4s}")
print("-" * 68)

tot = {"allow": 0, "in_db": 0, "wealth": 0, "miss": 0}
for cycle in cycles:
    m = re.match(r"^(.+)-(\d{4})$", cycle)
    if not m:
        continue
    slug, year = m.group(1), int(m.group(2))
    state = NAMES.get(slug)
    if not state:
        continue

    slug_year = cycle.replace("-", "_")
    allow_path = find_allowlist(slug_year)
    if not allow_path:
        continue
    allow = [ln.strip() for ln in allow_path.read_text().splitlines()
             if ln.strip() and not ln.startswith("#")]

    # Load manifest for name↔pdf mapping
    import json
    mf_path = RAW_ROOT / cycle / "manifest.jsonl"
    mf_by_name = {}
    for line in mf_path.read_text().splitlines():
        try:
            r = json.loads(line)
        except:
            continue
        p = r.get("pdf_path") or ""
        if p:
            mf_by_name[Path(p).name] = r

    allow_keys = set()
    for name in allow:
        row = mf_by_name.get(name)
        if row:
            allow_keys.add((_norm(row.get("name", "")),
                            _norm_const(row.get("constituency", ""))))

    # Query SQLite
    cur.execute("""
        SELECT p.name, c.name, ea.total_assets_inr, ea.education
        FROM election_appearances ea
        JOIN politicians p ON ea.politician_id = p.id
        JOIN elections e ON ea.election_id = e.id
        JOIN constituencies c ON ea.constituency_id = c.id
        JOIN states s ON c.state_id = s.id
        WHERE s.name = ? AND e.year = ?
    """, (state, year))
    matched = with_wealth = 0
    for row in cur.fetchall():
        key = (_norm(row[0]), _norm_const(row[1]))
        if key in allow_keys:
            matched += 1
            if row[2] is not None:
                with_wealth += 1

    missing = len(allow) - with_wealth
    pct = (100 * with_wealth // len(allow)) if allow else 0
    print(f"{state:<22s} {year:>4d}  {len(allow):>6d}  {matched:>6d}  "
          f"{with_wealth:>7d}  {missing:>7d}  {pct:>3d}%")
    tot["allow"] += len(allow)
    tot["in_db"] += matched
    tot["wealth"] += with_wealth
    tot["miss"]   += missing

print("-" * 68)
print(f"{'TOTAL':<22s} {'':>4s}  {tot['allow']:>6d}  {tot['in_db']:>6d}  "
      f"{tot['wealth']:>7d}  {tot['miss']:>7d}")

# Also count total rows in main tables
print(f"\n=== Raw table counts (all states) ===")
for tbl in ("states", "elections", "constituencies", "politicians",
            "election_appearances"):
    n = cur.execute(f"SELECT COUNT(*) FROM {tbl}").fetchone()[0]
    print(f"  {tbl:<25s} {n:>7d}")
