"""Per-state DB-coverage report. For each state cycle:
  - Allow: size of the top-N allowlist (target)
  - In-DB with wealth: DB rows for allowlist candidates that have
    total_assets_inr filled (i.e. apply succeeded and Gemini extracted assets)
  - Missing wealth: Allow - In-DB-with-wealth (the real coverage gap)
  - In-DB with education: same but for education field

Reads from the DB (Postgres via DATABASE_URL, or local SQLite fallback).
Cross-references against each state's top-N allowlist file to define scope.

Writes data/reports/state_coverage.csv + prints a summary table.

Usage:
    source secrets/.env
    python scripts/state_coverage_report.py
    python scripts/state_coverage_report.py --state "Uttar Pradesh"
"""
from __future__ import annotations
import argparse
import csv
import json
import os
import re
import sys
from pathlib import Path

import sqlalchemy
from sqlalchemy import text
from sqlalchemy.orm import sessionmaker

ROOT = Path(__file__).resolve().parent.parent
ALLOW_DIR = ROOT / "data" / "allowlists"
RAW_ROOT = ROOT / "data" / "eci" / "raw_pdfs"
OUT_CSV = ROOT / "data" / "reports" / "state_coverage.csv"


DB_URL = os.environ.get("DATABASE_URL",
                          f"sqlite:///{ROOT / 'lokvani.db'}")


def find_allowlist(slug_year: str) -> Path | None:
    p = ALLOW_DIR / f"{slug_year}.txt"
    if p.exists():
        return p
    matches = sorted(ALLOW_DIR.glob(f"{slug_year}_top*.txt"))
    return matches[0] if matches else None


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", (s or "").lower())


def audit(cycle_dir: Path, sess) -> dict | None:
    cycle_name = cycle_dir.name
    slug_year = cycle_name.replace("-", "_")

    # Try to derive display state + year from cycle name
    m = re.match(r"^(.+)-(\d{4})$", cycle_name)
    if not m:
        return None
    slug, year = m.group(1), int(m.group(2))

    # Map slug back to display name via a lookup table
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
    display = NAMES.get(slug, slug.title())

    # Load allowlist
    allow_path = find_allowlist(slug_year)
    if not allow_path:
        return None
    allowlist = [ln.strip() for ln in allow_path.read_text().splitlines()
                 if ln.strip() and not ln.startswith("#")]

    # Load manifest to map allowlist basenames → (name, constituency)
    mf_path = cycle_dir / "manifest.jsonl"
    if not mf_path.exists():
        return None
    mf_by_name = {}
    for line in mf_path.read_text().splitlines():
        try:
            r = json.loads(line)
        except Exception:
            continue
        p = r.get("pdf_path") or ""
        if p:
            mf_by_name[Path(p).name] = r

    # For each allowlist entry, check DB for wealth + education
    allow_keys = []   # (norm_name, norm_const)
    for name in allowlist:
        row = mf_by_name.get(name)
        if not row:
            continue
        allow_keys.append((_norm(row.get("name", "")),
                            _norm(row.get("constituency", ""))))

    # DB query — all appearances for (state, year) with their name+const
    q = text("""
      SELECT p.name, c.name,
             ea.total_assets_inr, ea.education
        FROM election_appearances ea
        JOIN politicians  p  ON ea.politician_id   = p.id
        JOIN elections    e  ON ea.election_id     = e.id
        JOIN constituencies c ON ea.constituency_id = c.id
        JOIN states       s  ON c.state_id         = s.id
       WHERE s.name = :state AND e.year = :year
    """)
    db_rows = sess.execute(q, {"state": display, "year": year}).fetchall()
    db_map = {}   # (norm_name, norm_const) → (wealth?, edu?)
    for r in db_rows:
        db_map[(_norm(r[0]), _norm(r[1]))] = (r[2] is not None, r[3] is not None)

    with_wealth = with_edu = matched = 0
    for key in allow_keys:
        hit = db_map.get(key)
        if hit is None:
            continue
        matched += 1
        if hit[0]:
            with_wealth += 1
        if hit[1]:
            with_edu += 1

    return {
        "cycle": cycle_name,
        "state": display,
        "year": year,
        "allowlist": len(allowlist),
        "in_db":  matched,
        "with_wealth": with_wealth,
        "with_edu":  with_edu,
        "missing_wealth": len(allowlist) - with_wealth,
        "missing_edu":    len(allowlist) - with_edu,
        "wealth_pct": (100 * with_wealth // len(allowlist)) if allowlist else 0,
    }


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                  formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--state", default="",
                    help="Restrict to one state display name")
    args = ap.parse_args()

    engine = sqlalchemy.create_engine(DB_URL)
    sess = sessionmaker(bind=engine)()

    cycle_dirs = sorted(d for d in RAW_ROOT.iterdir()
                        if d.is_dir() and (d / "manifest.jsonl").exists())

    rows = []
    for cd in cycle_dirs:
        r = audit(cd, sess)
        if r and (not args.state or r["state"] == args.state):
            rows.append(r)

    if not rows:
        sys.exit("No cycles matched.")

    # Print table
    print(f"{'State':<22s} {'Year':>4s}  {'Allow':>6s}  {'In DB':>6s}  "
          f"{'Wealth':>7s}  {'Miss W':>7s}  {'Edu':>5s}  {'%W':>4s}")
    print("-" * 76)
    tot = {"allowlist": 0, "in_db": 0, "with_wealth": 0, "with_edu": 0,
           "missing_wealth": 0, "missing_edu": 0}
    for r in sorted(rows, key=lambda x: -x["missing_wealth"]):
        print(f"{r['state']:<22s} {r['year']:>4d}  "
              f"{r['allowlist']:>6d}  {r['in_db']:>6d}  "
              f"{r['with_wealth']:>7d}  {r['missing_wealth']:>7d}  "
              f"{r['with_edu']:>5d}  {r['wealth_pct']:>3d}%")
        for k in tot:
            tot[k] += r[k]
    print("-" * 76)
    print(f"{'TOTAL':<22s} {'':>4s}  "
          f"{tot['allowlist']:>6d}  {tot['in_db']:>6d}  "
          f"{tot['with_wealth']:>7d}  {tot['missing_wealth']:>7d}  "
          f"{tot['with_edu']:>5d}")

    # CSV
    OUT_CSV.parent.mkdir(parents=True, exist_ok=True)
    with OUT_CSV.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)
    print(f"\nCSV: {OUT_CSV.relative_to(ROOT)}", file=sys.stderr)


if __name__ == "__main__":
    main()
