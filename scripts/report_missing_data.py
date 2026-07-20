"""Report candidates in the DB that are missing wealth or education
data, grouped by state. Also writes a CSV per state so you can walk
down the list and manually fetch each missing affidavit.

A candidate counts as "missing data" if their ElectionAppearance row
has NULL total_assets_inr AND NULL education. (i.e. Gemini extraction
never got to them, or the source PDF was too corrupt to parse.)

For each row we include:
  - candidate name
  - constituency
  - party
  - election year
  - affidavit_id  (if we know it from the manifest)
  - profile_url   (if known — the exact ECI page to visit)
  - expected_pdf_name  (the filename the pipeline would save)

Usage:
    python scripts/report_missing_data.py                    # summary + CSVs
    python scripts/report_missing_data.py --state "Uttar Pradesh"
"""
from __future__ import annotations
import argparse
import csv
import json
import os
import sys
from collections import defaultdict
from pathlib import Path

import sqlalchemy
from sqlalchemy import text
from sqlalchemy.orm import sessionmaker

# --- Environment ------------------------------------------------------
ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DB = f"sqlite:///{ROOT / 'lokvani.db'}"
DB_URL = os.environ.get("DATABASE_URL", DEFAULT_DB)

OUT_DIR = ROOT / "data" / "reports" / "missing_data"
MANIFEST_ROOT = ROOT / "data" / "eci" / "raw_pdfs"


def cycle_dir_for(state: str, year: int) -> Path | None:
    """State display name -> cycle directory (e.g. 'Uttar Pradesh', 2022 ->
    'uttarpradesh-2022'). Returns first match if manifest exists."""
    slug = state.lower().replace(" ", "").replace("&", "").replace("_", "")
    for d in sorted(MANIFEST_ROOT.iterdir()):
        name = d.name.lower().replace("-", "")
        if name.startswith(slug) and name.endswith(str(year)):
            if (d / "manifest.jsonl").exists():
                return d
    return None


def load_manifest_index(cycle_dir: Path) -> dict[tuple[str, str, str], dict]:
    """Return {(norm_name, norm_const, norm_party): manifest_row} for lookups.

    Normalizes: name uppercased+trimmed; const/party lowercased+stripped
    to non-alnum for fuzzy join tolerance.
    """
    import re
    def _norm_text(s: str) -> str:
        return re.sub(r"[^a-z0-9]+", "", (s or "").lower())
    def _norm_name(s: str) -> str:
        return re.sub(r"[^a-z0-9]+", "", (s or "").lower())

    idx: dict[tuple[str, str, str], dict] = {}
    mf = cycle_dir / "manifest.jsonl"
    for line in mf.read_text().splitlines():
        try:
            r = json.loads(line)
        except json.JSONDecodeError:
            continue
        key = (_norm_name(r.get("name", "")),
               _norm_text(r.get("constituency", "")),
               _norm_text(r.get("party", "")))
        idx[key] = r
    return idx


def enrich_from_manifest(row: dict, mf_idx: dict) -> dict:
    """Try to fill profile_url + expected_pdf_name via manifest join."""
    import re
    def _norm_text(s: str) -> str:
        return re.sub(r"[^a-z0-9]+", "", (s or "").lower())
    key_full = (_norm_text(row["candidate"]),
                _norm_text(row["constituency"]),
                _norm_text(row["party"]))
    hit = mf_idx.get(key_full)
    if not hit:
        # Fallback: match on (name, constituency) only, first hit
        for (n, c, p), r in mf_idx.items():
            if n == _norm_text(row["candidate"]) and c == _norm_text(row["constituency"]):
                hit = r
                break
    if hit:
        row["affidavit_id"] = hit.get("affidavit_id") or ""
        row["profile_url"] = hit.get("profile_url") or ""
        pdf = hit.get("pdf_path") or ""
        row["expected_pdf_name"] = Path(pdf).name if pdf else ""
    else:
        row["affidavit_id"] = ""
        row["profile_url"] = ""
        row["expected_pdf_name"] = ""
    return row


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                  formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--state", default="",
                    help="Restrict to a single state (display name)")
    ap.add_argument("--year", type=int, default=0,
                    help="Restrict to a single year")
    args = ap.parse_args()

    print(f"DB: {DB_URL}", file=sys.stderr)
    engine = sqlalchemy.create_engine(DB_URL)
    Session = sessionmaker(bind=engine)
    sess = Session()

    # Any ElectionAppearance where BOTH total_assets_inr IS NULL AND
    # education IS NULL is treated as unprocessed / no data.
    where = ["ea.total_assets_inr IS NULL", "ea.education IS NULL"]
    params: dict = {}
    if args.state:
        where.append("s.name = :state")
        params["state"] = args.state
    if args.year:
        where.append("e.year = :year")
        params["year"] = args.year

    q = text(f"""
      SELECT s.name                                  AS state,
             e.year                                  AS year,
             p.name                                  AS candidate,
             c.name                                  AS constituency,
             COALESCE(pa.full_name, pa.short_name)   AS party,
             ea.won                                  AS won
        FROM election_appearances ea
        JOIN politicians  p  ON ea.politician_id   = p.id
        JOIN elections    e  ON ea.election_id     = e.id
        JOIN constituencies c ON ea.constituency_id = c.id
        JOIN states       s  ON c.state_id         = s.id
        LEFT JOIN parties pa ON ea.party_id        = pa.id
       WHERE {' AND '.join(where)}
       ORDER BY s.name, e.year, c.name, p.name
    """)
    rows = [dict(r._mapping) for r in sess.execute(q, params).fetchall()]

    if not rows:
        print("\nNo missing-data candidates found for the given filters.",
              file=sys.stderr)
        return

    # Group by (state, year) and enrich with manifest info per group
    by_group: dict[tuple[str, int], list[dict]] = defaultdict(list)
    for r in rows:
        by_group[(r["state"], r["year"])].append(r)

    OUT_DIR.mkdir(parents=True, exist_ok=True)

    print(f"\n{'State':30s}  {'Year':>4s}  {'Missing':>8s}   CSV path",
          file=sys.stderr)
    print(f"{'-'*30}  {'-'*4}  {'-'*8}   {'-'*40}", file=sys.stderr)
    total = 0
    for (state, year), group_rows in sorted(by_group.items()):
        # Try to enrich from manifest
        cyc = cycle_dir_for(state, year)
        if cyc:
            mf_idx = load_manifest_index(cyc)
            for row in group_rows:
                enrich_from_manifest(row, mf_idx)
        else:
            for row in group_rows:
                row["affidavit_id"] = ""
                row["profile_url"] = ""
                row["expected_pdf_name"] = ""

        slug = state.lower().replace(" ", "_")
        out_path = OUT_DIR / f"missing_{slug}_{year}.csv"
        cols = ["state", "year", "candidate", "constituency", "party",
                "won", "affidavit_id", "expected_pdf_name", "profile_url"]
        with out_path.open("w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore")
            w.writeheader()
            for row in group_rows:
                w.writerow(row)
        total += len(group_rows)
        print(f"{state:30s}  {year:>4d}  {len(group_rows):>8,d}   "
              f"{out_path.relative_to(ROOT)}", file=sys.stderr)

    print(f"{'-'*30}  {'-'*4}  {'-'*8}", file=sys.stderr)
    print(f"{'TOTAL':30s}  {'':>4s}  {total:>8,d}", file=sys.stderr)
    print(f"\nCSVs written to: {OUT_DIR.relative_to(ROOT)}", file=sys.stderr)


if __name__ == "__main__":
    main()
