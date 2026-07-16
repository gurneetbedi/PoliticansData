"""
One-time backfill: replace the generic `https://affidavit.eci.gov.in/`
placeholder on ECI-loaded election_appearances with the per-candidate
`profile_url` from each state's manifest.jsonl.

Walks every `data/eci/raw_pdfs/<state>-<year>/manifest.jsonl`, builds
{affidavit_id: profile_url}, joins to `eci_candidates_provisional`, and
UPDATEs the matching `election_appearances.source_url`.

Idempotent — safe to re-run. Skips rows that already have a specific
profile URL (i.e. any source_url that isn't the generic homepage).

Usage:
    python scripts/backfill_eci_profile_urls.py
    python scripts/backfill_eci_profile_urls.py --dry-run
    python scripts/backfill_eci_profile_urls.py --cycle rajasthan-2023
"""
from __future__ import annotations
import argparse
import json
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DB   = ROOT / "lokvani.db"
GENERIC = "https://affidavit.eci.gov.in/"


def state_name_from_slug(slug: str) -> str:
    """Turn 'jammu_and_kashmir' / 'madhyapradesh' / 'westbengal' into
    a canonical DB name. Best-effort — DB has the canonical strings
    already so we look them up rather than guessing."""
    _SPECIAL = {
        "jammuandkashmir":  "Jammu and Kashmir",
        "jammu_and_kashmir":"Jammu and Kashmir",
        "jk":               "Jammu and Kashmir",
        "andhrapradesh":    "Andhra Pradesh",
        "arunachalpradesh": "Arunachal Pradesh",
        "arunachal":        "Arunachal Pradesh",
        "himachalpradesh":  "Himachal Pradesh",
        "himachal":         "Himachal Pradesh",
        "madhyapradesh":    "Madhya Pradesh",
        "tamilnadu":        "Tamil Nadu",
        "uttarpradesh":     "Uttar Pradesh",
        "westbengal":       "West Bengal",
    }
    return _SPECIAL.get(slug.lower(), slug.strip().title())


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                  formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--cycle", default="",
                    help="Restrict to one cycle folder name, e.g. rajasthan-2023")
    args = ap.parse_args()

    con = sqlite3.connect(str(DB))
    cur = con.cursor()

    raw_pdfs = ROOT / "data" / "eci" / "raw_pdfs"
    cycles = sorted(p for p in raw_pdfs.iterdir()
                    if p.is_dir() and (p / "manifest.jsonl").exists())
    if args.cycle:
        cycles = [c for c in cycles if c.name == args.cycle]

    print(f"Cycles to process: {len(cycles)}", file=sys.stderr)
    total_updated = 0
    total_skipped = 0
    total_missing = 0

    for cyc_dir in cycles:
        cycle = cyc_dir.name                # e.g. "rajasthan-2023"
        state_slug, _, year_str = cycle.rpartition("-")
        try:
            year = int(year_str)
        except ValueError:
            print(f"  {cycle}: bad year in folder name, skipping", file=sys.stderr)
            continue

        state_name = state_name_from_slug(state_slug)
        # Confirm state exists in DB (canonical spelling)
        row = cur.execute(
            "SELECT COUNT(*) FROM states WHERE lower(name) = lower(?)",
            (state_name,)).fetchone()
        if not row[0]:
            print(f"  {cycle}: no DB row for state {state_name!r}, skipping",
                  file=sys.stderr)
            continue

        # Load manifest → {affidavit_id: profile_url}
        aff_to_url: dict[str, str] = {}
        with (cyc_dir / "manifest.jsonl").open() as f:
            for line in f:
                if not line.strip():
                    continue
                try:
                    r = json.loads(line)
                except json.JSONDecodeError:
                    continue
                aid = str(r.get("affidavit_id") or "").strip()
                url = (r.get("profile_url") or "").strip()
                if aid and url:
                    aff_to_url[aid] = url

        if not aff_to_url:
            print(f"  {cycle}: manifest has no profile_urls, skipping",
                  file=sys.stderr)
            continue

        # Find each ECI-loaded appearance for this state+year, join to
        # provisional to get affidavit_id, then update source_url.
        prov_rows = cur.execute("""
            SELECT p.affidavit_id, ea.id, ea.source_url
            FROM eci_candidates_provisional p
            JOIN politicians pol   ON lower(pol.name) = lower(p.candidate_name)
            JOIN election_appearances ea ON ea.politician_id = pol.id
            JOIN elections e       ON e.id = ea.election_id
            JOIN states s          ON s.id = e.state_id
            WHERE lower(s.name) = lower(?) AND e.year = ?
              AND p.state = ? AND p.election_year = ?
        """, (state_name, year, state_name, year)).fetchall()

        updated = 0
        skipped = 0
        missing = 0
        for aff_id, ea_id, current_url in prov_rows:
            new_url = aff_to_url.get(str(aff_id))
            if not new_url:
                missing += 1
                continue
            # Skip if already has a non-generic URL
            if current_url and current_url != GENERIC:
                skipped += 1
                continue
            if not args.dry_run:
                cur.execute(
                    "UPDATE election_appearances SET source_url = ? WHERE id = ?",
                    (new_url, ea_id))
            updated += 1

        print(f"  {cycle:30s} → updated={updated:5d}  already-set={skipped:5d}  no-manifest-url={missing:4d}",
              file=sys.stderr)
        total_updated += updated
        total_skipped += skipped
        total_missing += missing

    if not args.dry_run:
        con.commit()

    print(file=sys.stderr)
    print(f"========== BACKFILL SUMMARY ==========", file=sys.stderr)
    print(f"  Rows updated:              {total_updated}", file=sys.stderr)
    print(f"  Rows already had URL:      {total_skipped}", file=sys.stderr)
    print(f"  Rows with no manifest URL: {total_missing}", file=sys.stderr)
    if args.dry_run:
        print(f"  (DRY RUN — no writes committed)", file=sys.stderr)
    con.close()


if __name__ == "__main__":
    main()
