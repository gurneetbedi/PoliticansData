"""
Reconcile our newly-loaded ECI Delhi 2025 candidates against the existing
myneta-sourced election_appearances rows.

Answers four concrete questions:

  1. How many ECI candidates match a known myneta politician?
       (ECI confirms myneta — same person, same election)

  2. How many ECI candidates are NEW — i.e. exist in ECI but not myneta?
       (These are gains. myneta typically only covers winners + top
       runner-ups. The fringe Independents and small-party candidates
       myneta skips are the bulk of these.)

  3. How many myneta politicians for Delhi 2025 have NO matching ECI row?
       (These are losses. Likely name-collision misses or candidates whose
       affidavits weren't accepted/filed. Worth a manual look.)

  4. For the matches, what does the (myneta total assets) vs
     (ECI movable_self + movable_spouse, when we have them) look like?
       (Sanity check on the structured extractor. Most should agree
       within rounding when we have both. Big deltas → flag for review.)

USAGE
-----
    python scripts/reconcile_delhi_eci_vs_myneta.py

Read-only — does not modify any table. Pure analysis.
"""
from __future__ import annotations

import re
import sqlite3
import sys
from collections import defaultdict
from pathlib import Path

DB = Path(__file__).resolve().parent.parent / "politrack.db"


def _norm_name(s: str) -> str:
    """Aggressive name normalisation for fuzzy matching across sources."""
    s = (s or "").upper()
    s = re.sub(r"[^A-Z0-9 ]", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    # Drop common honorifics / titles that vary between sources
    for prefix in ("SH ", "SHRI ", "MS ", "SMT ", "DR ", "ADV ", "ADVOCATE ",
                    "CA ", "PROF ", "KU ", "KUMARI "):
        if s.startswith(prefix):
            s = s[len(prefix):]
    return s


def _norm_constituency(s: str) -> str:
    """Strip 'AC-N,' prefix and whitespace so 'AC-18, MODEL TOWN' matches 'MODEL TOWN'."""
    s = (s or "").upper()
    s = re.sub(r"^\s*AC[-\s]*\d+\s*,?\s*", "", s)
    s = re.sub(r",?\s*NCT OF DELHI\s*$", "", s)
    return s.strip()


def main():
    if not DB.exists():
        sys.exit(f"DB not found: {DB}")

    con = sqlite3.connect(str(DB))
    con.row_factory = sqlite3.Row
    cur = con.cursor()

    # ---------- ECI side ----------
    cur.execute("""
        SELECT candidate_name, constituency, party, movable_self,
               movable_spouse, fields_present_count, quality_status
        FROM eci_candidates_provisional
        WHERE state = 'Delhi' AND election_year = 2025
    """)
    eci_rows = cur.fetchall()
    eci_index: dict[str, list] = defaultdict(list)
    for r in eci_rows:
        key = _norm_name(r["candidate_name"])
        eci_index[key].append(r)

    # ---------- myneta side ----------
    cur.execute("""
        SELECT p.name, ea.total_assets_inr, ea.criminal_cases_count,
               ea.education, c.name AS constituency, party.short_name AS party,
               ea.won
        FROM politicians p
        JOIN election_appearances ea ON ea.politician_id = p.id
        JOIN elections e ON ea.election_id = e.id
        LEFT JOIN constituencies c ON ea.constituency_id = c.id
        LEFT JOIN parties party ON ea.party_id = party.id
        LEFT JOIN states s ON e.state_id = s.id
        WHERE e.year = 2025 AND s.name = 'Delhi'
    """)
    myneta_rows = cur.fetchall()
    myneta_index: dict[str, list] = defaultdict(list)
    for r in myneta_rows:
        key = _norm_name(r["name"])
        myneta_index[key].append(r)

    # ---------- Compare ----------
    eci_keys = set(eci_index.keys())
    myneta_keys = set(myneta_index.keys())

    matched_keys = eci_keys & myneta_keys
    eci_only_keys = eci_keys - myneta_keys
    myneta_only_keys = myneta_keys - eci_keys

    # Asset-delta sanity check for matched rows
    asset_diffs = []
    for k in matched_keys:
        for eci in eci_index[k]:
            for myn in myneta_index[k]:
                eci_total = (eci["movable_self"] or 0) + (eci["movable_spouse"] or 0)
                myn_total = myn["total_assets_inr"] or 0
                if eci_total > 0 and myn_total > 0:
                    asset_diffs.append({
                        "name": myn["name"],
                        "myneta_total": myn_total,
                        "eci_movable": eci_total,
                        "pct_diff": abs(eci_total - myn_total) / myn_total * 100,
                    })

    # ---------- Report ----------
    print("=" * 70)
    print("DELHI 2025 — ECI vs myneta reconciliation")
    print("=" * 70)
    print()
    print(f"ECI candidates (this pull):       {len(eci_rows):>4d}  "
          f"({len(eci_keys)} unique names)")
    print(f"myneta candidates (existing DB):  {len(myneta_rows):>4d}  "
          f"({len(myneta_keys)} unique names)")
    print()
    print(f"  ✓ MATCHED   (in both sources):  {len(matched_keys):>4d}")
    print(f"  + ECI-only  (myneta missed):    {len(eci_only_keys):>4d}")
    print(f"  − myneta-only (we missed):      {len(myneta_only_keys):>4d}")
    print()

    print("=== Sample MATCHED candidates ===")
    for k in sorted(matched_keys)[:8]:
        eci = eci_index[k][0]
        myn = myneta_index[k][0]
        print(f"  {myn['name'][:30]:30s}  myneta={myn['party']!r:18s}  "
              f"eci_quality={eci['quality_status']}  fields={eci['fields_present_count']}/20")

    print()
    print("=== Sample ECI-ONLY candidates (gains over myneta) ===")
    for k in sorted(eci_only_keys)[:15]:
        eci = eci_index[k][0]
        party = (eci["party"] or "?")[:25]
        print(f"  {eci['candidate_name'][:32]:32s}  party={party}")

    print()
    print("=== Sample myneta-ONLY candidates (likely name-match misses) ===")
    for k in sorted(myneta_only_keys)[:8]:
        myn = myneta_index[k][0]
        print(f"  {myn['name'][:30]:30s}  "
              f"party={myn['party']!r:12s}  won={myn['won']}")

    print()
    if asset_diffs:
        print("=== Asset reconciliation (matched candidates only) ===")
        ok = sum(1 for d in asset_diffs if d["pct_diff"] <= 10)
        close = sum(1 for d in asset_diffs if 10 < d["pct_diff"] <= 50)
        far = sum(1 for d in asset_diffs if d["pct_diff"] > 50)
        print(f"  Within 10% of myneta:  {ok:>3d}")
        print(f"  10-50% deviation:      {close:>3d}")
        print(f"  >50% deviation:        {far:>3d}")
        print()
        if far:
            print("  Top 5 largest deviations (worth a manual look):")
            for d in sorted(asset_diffs, key=lambda x: -x["pct_diff"])[:5]:
                print(f"    {d['name'][:25]:25s}  "
                      f"myneta=₹{d['myneta_total']:>11,}  "
                      f"eci=₹{d['eci_movable']:>11,}  Δ={d['pct_diff']:.0f}%")
    else:
        print("=== Asset reconciliation ===")
        print("  No matched candidates have both myneta total_assets and "
              "ECI movable totals populated. (Regex extractor missed "
              "movable_self/spouse for most rows — expected.)")

    print()
    print("=" * 70)


if __name__ == "__main__":
    main()
