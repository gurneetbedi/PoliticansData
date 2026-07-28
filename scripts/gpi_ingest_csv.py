"""Load GPI indicator values from a CSV file.

This is the manual / bootstrap ingester — populate a CSV with rows for
one or more (indicator, state, year) tuples and this script inserts them
idempotently. Specialised ingesters (RBI, PLFS, UDISE+, NCRB, etc.) will
be added as separate scripts; this one is what you use in the meantime
when you have hand-collected or one-off numbers.

CSV columns (header row required, order flexible):
    indicator_code       "E01", "F02", "LO01"
    state_code           "PB", "MH", ...
    fiscal_year          2018 (= FY18 = 2017-18) through 2026
    raw_value            float, in the indicator's native unit
    source_url           direct URL where this value was found (optional)
    source_document      e.g. "RBI Handbook 2024, Table 1.3"
    extraction_method    "manual" | "scraped" | "llm_extracted" — default "manual"
    staleness            "current" | "carried_forward" | "interpolated" — default "current"
    notes                free text (optional)

Usage:
    python scripts/gpi_ingest_csv.py data/gpi/punjab_bootstrap.csv
    python scripts/gpi_ingest_csv.py data/gpi/punjab_bootstrap.csv --dry-run

A tiny sample file lives at data/gpi/samples/punjab_bootstrap_example.csv
"""
from __future__ import annotations
import argparse
import csv
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.database import SessionLocal
from app import models
from app.gpi_models import GpiIndicator, GpiIndicatorValue
from app.models import State


REQUIRED = {"indicator_code", "state_code", "fiscal_year", "raw_value"}


def load_csv(path: Path):
    rows = []
    with path.open() as f:
        reader = csv.DictReader(f)
        cols = set(reader.fieldnames or [])
        missing = REQUIRED - cols
        if missing:
            raise SystemExit(f"CSV missing required columns: {missing}")
        for i, r in enumerate(reader, 2):  # start at 2 (row 1 = header)
            try:
                r["fiscal_year"] = int(r["fiscal_year"])
                r["raw_value"] = float(r["raw_value"]) if r["raw_value"] else None
            except (ValueError, TypeError) as e:
                raise SystemExit(f"Row {i}: {e}")
            rows.append((i, r))
    return rows


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                  formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("csv_path", help="Path to CSV file with rows to load")
    ap.add_argument("--dry-run", action="store_true", help="Preview without writing")
    args = ap.parse_args()

    csv_path = Path(args.csv_path)
    if not csv_path.exists():
        raise SystemExit(f"No such file: {csv_path}")

    rows = load_csv(csv_path)
    print(f"Loaded {len(rows)} rows from {csv_path}")

    session = SessionLocal()
    try:
        # Build lookup caches so we don't hit DB per row
        indicators_by_code = {i.code: i for i in session.query(GpiIndicator).all()}
        states_by_code     = {s.code: s for s in session.query(State).all()}

        inserted = updated = skipped = 0
        for line_no, r in rows:
            ind = indicators_by_code.get(r["indicator_code"])
            if not ind:
                print(f"  ✗ row {line_no}: unknown indicator '{r['indicator_code']}'")
                skipped += 1
                continue
            st = states_by_code.get(r["state_code"])
            if not st:
                print(f"  ✗ row {line_no}: unknown state '{r['state_code']}'")
                skipped += 1
                continue

            existing = session.query(GpiIndicatorValue).filter_by(
                indicator_id=ind.id, state_id=st.id, fiscal_year=r["fiscal_year"]
            ).one_or_none()

            payload = {
                "raw_value":         r["raw_value"],
                "source_url":        r.get("source_url") or None,
                "source_document":   r.get("source_document") or None,
                "extraction_method": r.get("extraction_method") or "manual",
                "staleness":         r.get("staleness") or "current",
                "notes":             r.get("notes") or None,
                "extracted_at":      datetime.utcnow(),
            }

            if existing:
                for k, v in payload.items():
                    if v is not None:  # don't clobber existing fields with blanks
                        setattr(existing, k, v)
                # Clear cached normalized value — will be recomputed by scoring engine
                existing.normalized_value = None
                existing.national_rank = None
                updated += 1
            else:
                session.add(GpiIndicatorValue(
                    indicator_id=ind.id,
                    state_id=st.id,
                    fiscal_year=r["fiscal_year"],
                    **payload,
                ))
                inserted += 1

            print(f"  ✓ {r['state_code']} {r['fiscal_year']} {ind.code:<6s} "
                  f"= {r['raw_value']}  [{ 'upd' if existing else 'new'}]")

        if args.dry_run:
            print("\nDRY RUN — rolling back")
            session.rollback()
        else:
            session.commit()

        print()
        print(f"═══ CSV ingest summary ═══")
        print(f"  Inserted: {inserted}")
        print(f"  Updated:  {updated}")
        print(f"  Skipped:  {skipped}")
    finally:
        session.close()


if __name__ == "__main__":
    main()
