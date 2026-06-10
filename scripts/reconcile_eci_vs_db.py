"""
Reconcile a folder of ECI affidavit PDFs against the live politrack.db.

For each candidate identified by dedup_affidavits.py, this script:

  1. Parses the candidate's canonical PDF with parse_eci_affidavit.
  2. Finds the matching politicians row in the DB by name fuzzy match,
     constrained to the same election year and state.
  3. Computes (total_assets, criminal_cases) deltas.
  4. Tags every candidate into one of:
       MATCH        — within 5% on assets AND same case count
       CLOSE        — 5-10% asset delta OR case count off by 1
       FLAG         — >10% asset delta OR cases off by 2+
       NO_DB_ROW    — DB has no matching politician for this election
       PARSE_FAIL   — ECI parse produced no usable total
  5. Writes a JSONL report (one row per candidate) plus a summary.

USAGE
-----
First run dedup_affidavits.py to produce dedup.json:

    python scripts/dedup_affidavits.py data/eci/raw_pdfs/delhi-2025/raw_pdfs/ \\
        --out data/eci/dedup_delhi2025.json

Then run this reconciliation:

    python scripts/reconcile_eci_vs_db.py \\
        --dedup data/eci/dedup_delhi2025.json \\
        --db politrack.db \\
        --election-year 2025 \\
        --state-name "NCT of Delhi" \\
        --out data/eci/reconcile_delhi2025.jsonl

Manual-review queue:

    jq 'select(.status == "FLAG")' data/eci/reconcile_delhi2025.jsonl
"""
from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
from dataclasses import dataclass, asdict, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from parse_eci_affidavit import parse_pdf, MissingDependencyError, _check_dependencies  # noqa: E402


# ---------------------------------------------------------------------------
# Thresholds — tune as we learn more about parser variance
# ---------------------------------------------------------------------------

ASSET_MATCH_PCT = 5.0      # <= this is MATCH
ASSET_CLOSE_PCT = 10.0     # <= this is CLOSE (above CLOSE_PCT is FLAG)
CASES_MATCH_DELTA = 0      # exact match required for MATCH on cases
CASES_CLOSE_DELTA = 1      # off by 1 is CLOSE


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass
class ReconRow:
    candidate_name: str
    party: str
    constituency: str
    canonical_pdf: str
    estamp_cert: str = ""
    # DB side
    db_politician_id: int | None = None
    db_appearance_id: int | None = None
    db_total_assets: int | None = None
    db_criminal_cases: int | None = None
    db_education: str = ""
    # ECI side
    eci_total_assets: int | None = None
    eci_movable_total: int | None = None
    eci_immovable_total: int | None = None
    eci_criminal_cases: int | None = None
    eci_name: str = ""
    eci_age: int | None = None
    # Comparison
    asset_delta_pct: float | None = None
    cases_delta: int | None = None
    status: str = ""
    notes: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# DB lookup
# ---------------------------------------------------------------------------

def _norm_name(s: str) -> str:
    """Normalise a name for fuzzy match: upper, alnum-only, single-spaced."""
    s = (s or "").upper()
    s = re.sub(r"[^A-Z0-9 ]", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s


def find_db_row(con: sqlite3.Connection, candidate_name: str,
                  election_year: int, state_name: str) -> dict | None:
    """Look up the matching politician for this election. Returns dict or None.

    Name match is strict (after normalisation) — Indian names have enough
    variance that fuzzy matching would risk false positives. If you find
    real mismatches that have alternate spellings, add them to the
    `manual_review` table separately rather than letting this match heuristically.
    """
    norm_target = _norm_name(candidate_name)
    if not norm_target:
        return None
    cur = con.cursor()
    cur.execute("""
        SELECT p.id AS pid, p.name, ea.id AS aid,
               ea.total_assets_inr, ea.total_liabilities_inr,
               ea.criminal_cases_count, ea.education,
               c.name AS constituency, party.short_name AS party,
               s.name AS state
        FROM politicians p
        JOIN election_appearances ea ON ea.politician_id = p.id
        JOIN elections e ON ea.election_id = e.id
        LEFT JOIN constituencies c ON ea.constituency_id = c.id
        LEFT JOIN parties party ON ea.party_id = party.id
        LEFT JOIN states s ON e.state_id = s.id
        WHERE e.year = ?
          AND (s.name LIKE ? OR s.name LIKE ?)
    """, (election_year, f"%{state_name}%", f"%{state_name.replace(' ', '%')}%"))
    rows = cur.fetchall()
    for row in rows:
        if _norm_name(row[1]) == norm_target:
            return {
                "pid": row[0], "name": row[1], "aid": row[2],
                "total_assets_inr": row[3], "total_liabilities_inr": row[4],
                "criminal_cases_count": row[5], "education": row[6],
                "constituency": row[7], "party": row[8], "state": row[9],
            }
    return None


# ---------------------------------------------------------------------------
# Comparison
# ---------------------------------------------------------------------------

def _sum_or_none(*xs):
    xs = [x for x in xs if x is not None]
    return sum(xs) if xs else None


def classify(row: ReconRow) -> str:
    """Tag MATCH / CLOSE / FLAG / NO_DB_ROW / PARSE_FAIL."""
    if row.db_total_assets is None:
        return "NO_DB_ROW"
    if row.eci_total_assets is None:
        return "PARSE_FAIL"

    # Asset delta
    if row.db_total_assets == 0:
        asset_pct = 0.0 if row.eci_total_assets == 0 else 100.0
    else:
        asset_pct = abs(row.eci_total_assets - row.db_total_assets) / row.db_total_assets * 100
    row.asset_delta_pct = round(asset_pct, 2)

    # Criminal cases delta
    if row.eci_criminal_cases is not None and row.db_criminal_cases is not None:
        row.cases_delta = row.eci_criminal_cases - row.db_criminal_cases
        cases_abs = abs(row.cases_delta)
    else:
        cases_abs = 0   # don't penalize if either side missing

    # Tag
    if asset_pct <= ASSET_MATCH_PCT and cases_abs <= CASES_MATCH_DELTA:
        return "MATCH"
    if asset_pct <= ASSET_CLOSE_PCT and cases_abs <= CASES_CLOSE_DELTA:
        return "CLOSE"
    return "FLAG"


def parse_and_compare(candidate: dict, con: sqlite3.Connection,
                        election_year: int, state_name: str) -> ReconRow:
    """Parse one candidate's canonical PDF and compare to DB."""
    row = ReconRow(
        candidate_name=candidate.get("name", "")
                       or Path(candidate["canonical_pdf"]).stem.rsplit("__", 1)[0].replace("_", " "),
        party=candidate.get("party", ""),
        constituency=candidate.get("constituency", ""),
        canonical_pdf=candidate["canonical_pdf"],
        estamp_cert=candidate.get("canonical_filing_cert", ""),
    )

    # DB lookup
    db = find_db_row(con, row.candidate_name, election_year, state_name)
    if db:
        row.db_politician_id = db["pid"]
        row.db_appearance_id = db["aid"]
        row.db_total_assets = db["total_assets_inr"]
        row.db_criminal_cases = db["criminal_cases_count"]
        row.db_education = db["education"] or ""

    # Parse the canonical PDF. Missing deps should bubble up — we don't
    # want to spend an hour producing 600 silent PARSE_FAIL rows because
    # pdftoppm isn't installed.
    try:
        parsed = parse_pdf(Path(row.canonical_pdf))
        row.eci_name = parsed.name
        row.eci_age = parsed.age
        row.eci_criminal_cases = parsed.total_pending_criminal_cases
        row.eci_movable_total = _sum_or_none(parsed.movable_assets_self_inr,
                                              parsed.movable_assets_spouse_inr)
        row.eci_immovable_total = _sum_or_none(parsed.immovable_assets_self_inr,
                                                 parsed.immovable_assets_spouse_inr)
        row.eci_total_assets = _sum_or_none(row.eci_movable_total,
                                              row.eci_immovable_total)
    except MissingDependencyError:
        raise   # bubble up — caught in main() and fails the whole run
    except Exception as e:
        row.notes.append(f"parse_error: {type(e).__name__}: {e}")

    row.status = classify(row)
    return row


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dedup", required=True, help="Path to dedup_affidavits.py output JSON")
    ap.add_argument("--db", default="politrack.db", help="SQLite DB path")
    ap.add_argument("--election-year", type=int, required=True)
    ap.add_argument("--state-name", required=True,
                    help="State name as it appears in states.name (e.g. 'NCT of Delhi')")
    ap.add_argument("--out", required=True, help="JSONL output path")
    ap.add_argument("--limit", type=int, default=0,
                    help="Stop after N candidates (smoke test)")
    args = ap.parse_args()

    # Fail loudly upfront if pdftoppm/tesseract aren't installed — otherwise
    # we'd waste an hour producing all-PARSE_FAIL output.
    try:
        _check_dependencies()
    except MissingDependencyError as e:
        sys.exit(f"\nERROR: {e}\n")

    dedup = json.load(open(args.dedup))
    candidates = dedup["candidates"]
    if args.limit:
        candidates = candidates[:args.limit]

    con = sqlite3.connect(args.db)

    counts = {"MATCH": 0, "CLOSE": 0, "FLAG": 0, "NO_DB_ROW": 0, "PARSE_FAIL": 0}
    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w") as f:
        for i, cand in enumerate(candidates, 1):
            print(f"[{i}/{len(candidates)}] {cand.get('name') or cand['canonical_pdf'].split('/')[-1]}",
                  file=sys.stderr)
            row = parse_and_compare(cand, con, args.election_year, args.state_name)
            counts[row.status] = counts.get(row.status, 0) + 1
            f.write(json.dumps(asdict(row), ensure_ascii=False) + "\n")
            f.flush()
            print(f"  → {row.status}  asset_Δ={row.asset_delta_pct}%  cases_Δ={row.cases_delta}",
                  file=sys.stderr)

    print(f"\n========== RECONCILIATION SUMMARY ==========", file=sys.stderr)
    total = sum(counts.values())
    for status in ("MATCH", "CLOSE", "FLAG", "NO_DB_ROW", "PARSE_FAIL"):
        pct = (counts[status] / total * 100) if total else 0
        print(f"  {status:12s}  {counts[status]:4d}  ({pct:.1f}%)", file=sys.stderr)
    print(f"  ----------  ----", file=sys.stderr)
    print(f"  TOTAL       {total:4d}", file=sys.stderr)

    # Write summary alongside
    summary_path = out_path.with_suffix(".summary.json")
    summary_path.write_text(json.dumps({"counts": counts, "total": total}, indent=2))
    print(f"\nWrote {out_path} and {summary_path}", file=sys.stderr)

    # Exit code: nonzero if FLAG ratio > 10% — alerts CI / downstream caller
    if total and counts["FLAG"] / total > 0.10:
        sys.exit(2)


if __name__ == "__main__":
    main()
