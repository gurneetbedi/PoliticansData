"""
For each state in the DB, report:
  - expected members  (sum of assembly seat counts × loaded cycles)
  - actual winners    (rows where election_appearances.won = 1)
  - missing           (expected - actual)

Run:
    python scripts/audit_missing_members.py

Also shows which states have 0 winners flagged (loader hasn't run) —
those are your top priority to fix.
"""
from __future__ import annotations

import sqlite3
import sys
from pathlib import Path


# Assembly seat counts. Sourced from ECI / statutory strength.
ASSEMBLY_SEATS = {
    "Andhra Pradesh":     175,
    "Arunachal Pradesh":   60,
    "Assam":              126,
    "Bihar":              243,
    "Chhattisgarh":        90,
    "Delhi":               70,
    "Goa":                 40,
    "Gujarat":            182,
    "Haryana":             90,
    "Himachal Pradesh":    68,
    "Jammu and Kashmir":   90,
    "Jharkhand":           81,
    "Karnataka":          224,
    "Kerala":             140,
    "Madhya Pradesh":     230,
    "Maharashtra":        288,
    "Manipur":             60,
    "Meghalaya":           60,
    "Mizoram":             40,
    "Nagaland":            60,
    "Odisha":             147,
    "Puducherry":          30,
    "Punjab":             117,
    "Rajasthan":          200,
    "Sikkim":              32,
    "Tamil Nadu":         234,
    "Telangana":          119,
    "Tripura":             60,
    "Uttar Pradesh":      403,
    "Uttarakhand":         70,
    "West Bengal":        294,
}


def main():
    root = Path(__file__).resolve().parent.parent
    db_path = root / "lokvani.db"
    if not db_path.exists():
        sys.exit(f"DB not found: {db_path}")

    con = sqlite3.connect(db_path)
    cur = con.cursor()

    # Per state, get the list of distinct Assembly cycles loaded + winner count.
    rows = cur.execute("""
        SELECT s.name,
               COUNT(DISTINCT e.year)                      AS cycles,
               GROUP_CONCAT(DISTINCT e.year)                AS years,
               SUM(CASE WHEN ea.won = 1 THEN 1 ELSE 0 END) AS winners,
               COUNT(*)                                     AS total_apps
        FROM   states s
        JOIN   elections e ON e.state_id = s.id
        JOIN   election_appearances ea ON ea.election_id = e.id
        WHERE  e.house = 'Assembly'
        GROUP BY s.name
        ORDER BY s.name
    """).fetchall()

    print(f"{'State':22s}  {'Cycles':>7s}  {'Expected':>9s}  {'Winners':>8s}  "
          f"{'Missing':>8s}  Notes")
    print("-" * 100)

    total_expected = 0
    total_winners  = 0
    total_missing  = 0
    zero_states    = []

    for name, cycles, years, winners, total_apps in rows:
        seats = ASSEMBLY_SEATS.get(name)
        if seats is None:
            expected = None
            expected_str = "?"
            missing_str  = "?"
        else:
            expected = seats * cycles
            missing = expected - (winners or 0)
            expected_str = str(expected)
            missing_str  = str(missing)
            total_expected += expected
            total_winners  += (winners or 0)
            total_missing  += missing

        note = ""
        if not winners:
            note = "⚠ loader not run (winners = 0)"
            zero_states.append(name)
        elif expected and (winners < expected):
            gap_pct = 100 * (expected - winners) / expected
            note = f"{gap_pct:.1f}% gap"

        year_str = str(years or "").replace(",", "/")
        print(f"{name:22s}  {cycles:>7d}  {expected_str:>9s}  "
              f"{winners or 0:>8d}  {missing_str:>8s}  {note} [{year_str}]")

    print("-" * 100)
    print(f"{'TOTAL':22s}  {'':>7s}  {total_expected:>9d}  "
          f"{total_winners:>8d}  {total_missing:>8d}")

    if zero_states:
        print(f"\n⚠ {len(zero_states)} states have 0 winners flagged. Fix these first:")
        for s in zero_states:
            print(f"    {s}")

    con.close()


if __name__ == "__main__":
    main()
