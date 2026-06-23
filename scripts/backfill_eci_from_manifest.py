"""
Backfill eci_candidates_provisional with the high-quality fields that the
ECI listing card published directly (and that we already scraped into
manifest.jsonl). Source = ECI; no myneta data touched.

WHY THIS IS THE RIGHT FIX
-------------------------
The regex-based structured extractor reads from preprocessed OCR text
where party / constituency are noisy (24-42% hit rate). But the fetcher's
listing-page scrape captured the same fields directly from ECI's HTML
listing card — at 100% coverage. We just never piped those into the DB
because the structured pipeline was designed to flow PDF → text → fields.

This script reads the manifest, joins on affidavit_id, and writes the
listing-card party + constituency + accepted_status into the DB. Pure
ECI provenance throughout.

IDEMPOTENT
----------
Re-running just refreshes the rows from manifest. Safe to run anytime.

USAGE
-----
    python scripts/backfill_eci_from_manifest.py
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DB_DEFAULT = ROOT / "politrack.db"
MANIFEST = ROOT / "data/eci/raw_pdfs/delhi-2025/manifest.jsonl"


def _norm_const(s: str) -> str:
    """ECI listing constituency comes as 'SEEMAPURI'; normalise whitespace."""
    if not s:
        return ""
    return " ".join(s.split()).upper()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=str(DB_DEFAULT),
                    help="SQLite DB path (default: politrack.db at repo root)")
    args = ap.parse_args()
    DB = Path(args.db)

    if not DB.exists():
        sys.exit(f"DB not found: {DB}")
    if not MANIFEST.exists():
        sys.exit(f"Manifest not found: {MANIFEST}")

    # ---- Make sure the table has the columns we want to write ------------
    con = sqlite3.connect(str(DB))
    cur = con.cursor()
    cur.execute("PRAGMA table_info(eci_candidates_provisional)")
    existing_cols = {row[1] for row in cur.fetchall()}

    if "affidavit_status" not in existing_cols:
        cur.execute("ALTER TABLE eci_candidates_provisional "
                    "ADD COLUMN affidavit_status VARCHAR(20)")
        con.commit()
        print("Added column: affidavit_status", file=sys.stderr)

    # ---- Load manifest and pick the best entry per affidavit_id ----------
    # A manifest may have multiple rows per affidavit_id if the fetcher
    # was retried. Keep the most recent download_succeeded row.
    manifest_by_id: dict[str, dict] = {}
    with MANIFEST.open() as f:
        for line in f:
            if not line.strip():
                continue
            row = json.loads(line)
            aid = (row.get("affidavit_id") or "").strip()
            if not aid:
                continue
            existing = manifest_by_id.get(aid)
            if existing is None or (row.get("download_succeeded")
                                     and not existing.get("download_succeeded")):
                manifest_by_id[aid] = row

    print(f"Manifest entries (unique by affidavit_id): {len(manifest_by_id)}",
          file=sys.stderr)

    # ---- Walk existing DB rows and backfill ------------------------------
    cur.execute(
        "SELECT affidavit_id, party, constituency, affidavit_status "
        "FROM eci_candidates_provisional"
    )
    db_rows = cur.fetchall()
    print(f"DB rows: {len(db_rows)}", file=sys.stderr)

    updated_party = updated_const = updated_status = 0
    no_manifest_match = 0
    set_clauses = ("party = ?", "constituency = ?", "affidavit_status = ?")

    for db_aid, db_party, db_const, db_status in db_rows:
        m = manifest_by_id.get(db_aid)
        if m is None:
            no_manifest_match += 1
            continue

        new_party = m.get("party") or db_party
        new_const = _norm_const(m.get("constituency") or "") or db_const
        new_status = m.get("status") or db_status

        # Only count "updated" if the value actually changes
        if new_party != db_party:
            updated_party += 1
        if new_const != db_const:
            updated_const += 1
        if new_status != db_status:
            updated_status += 1

        cur.execute(
            "UPDATE eci_candidates_provisional "
            "SET party = ?, constituency = ?, affidavit_status = ? "
            "WHERE affidavit_id = ?",
            (new_party, new_const, new_status, db_aid),
        )

    con.commit()
    con.close()

    # ---- Report -----------------------------------------------------------
    print(f"\n========== MANIFEST BACKFILL SUMMARY ==========", file=sys.stderr)
    print(f"  Rows updated for party:        {updated_party}", file=sys.stderr)
    print(f"  Rows updated for constituency: {updated_const}", file=sys.stderr)
    print(f"  Rows updated for status:       {updated_status}", file=sys.stderr)
    print(f"  Rows with no manifest match:   {no_manifest_match}", file=sys.stderr)
    print(file=sys.stderr)

    # Show new coverage
    con = sqlite3.connect(str(DB))
    cur = con.cursor()
    for col in ("party", "constituency", "affidavit_status"):
        cur.execute(f"SELECT COUNT(*) FROM eci_candidates_provisional "
                    f"WHERE {col} IS NOT NULL AND {col} != ''")
        n = cur.fetchone()[0]
        cur.execute("SELECT COUNT(*) FROM eci_candidates_provisional")
        total = cur.fetchone()[0]
        bar = "█" * int(n * 30 / total) if total else ""
        print(f"  {col:25s} {n}/{total}  ({100 * n / total:.1f}%)  {bar}",
              file=sys.stderr)

    # Sample
    print(f"\nSample of backfilled rows:", file=sys.stderr)
    cur.execute("""
        SELECT candidate_name, party, constituency, affidavit_status,
               quality_status, fields_present_count
        FROM eci_candidates_provisional
        ORDER BY candidate_name LIMIT 6
    """)
    for r in cur.fetchall():
        print(f"  {r[0][:25]:25s}  {r[1][:25]:25s}  "
              f"{r[2][:18]:18s}  status={r[3]}", file=sys.stderr)


if __name__ == "__main__":
    main()
