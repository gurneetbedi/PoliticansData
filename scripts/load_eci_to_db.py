"""
Load the structured CSV from extract_structured.py into politrack.db's
`eci_candidates_provisional` table.

Each row carries a `quality_status`:
  CLEAN          — every extractable field has a value
  NEEDS_REVIEW   — one or more fields missing; the regex extractor
                   couldn't pull them. Comes back later, either via
                   manual review, a tighter regex pass, or (when we
                   decide to) the LLM-on-text pipeline.

Idempotent: keyed on affidavit_id, so re-running with an updated CSV
overwrites existing rows in place. The provisional table lives alongside
the existing myneta-sourced tables — it never touches `politicians`,
`election_appearances`, or any other production table.

USAGE
-----
    python scripts/load_eci_to_db.py
        --csv data/eci/for_ai/extracted/delhi_2025_structured.csv
        --state Delhi --election-year 2025 --election-type Assembly
        --db politrack.db
"""
from __future__ import annotations

import argparse
import csv
import sqlite3
import sys
from datetime import datetime
from pathlib import Path


# ---------------------------------------------------------------------------
# Schema definition
# ---------------------------------------------------------------------------

# Fields the regex extractor is *supposed* to capture. Used to compute the
# quality_status and missing_fields columns. Drop here if a field is
# inherently optional / not always present in the affidavit.
EXTRACTABLE_FIELDS = [
    "estamp_cert", "estamp_date", "candidate_name", "father_or_husband", "age",
    "address", "phone", "email", "party", "constituency",
    "education", "profession_self", "self_pan", "spouse_pan",
    "pending_cases", "convictions",
    "movable_self", "movable_spouse",
    "liabilities_bank", "liabilities_disputed",
]


SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS eci_candidates_provisional (
    id                    INTEGER PRIMARY KEY AUTOINCREMENT,
    -- Provenance
    -- IMPORTANT: affidavit_id is NOT globally unique. The ECI portal
    -- recycles small integers across election cycles (e.g. affidavit_id=2
    -- exists in both Delhi 2020 and Delhi 2025 for different candidates).
    -- Uniqueness is enforced at the composite level by the index below.
    affidavit_id          VARCHAR(20)  NOT NULL,
    source_pdf            VARCHAR(255) NOT NULL,
    state                 VARCHAR(50)  NOT NULL,
    election_year         INTEGER      NOT NULL,
    election_type         VARCHAR(20)  NOT NULL,

    -- Identity
    candidate_name        VARCHAR(255) NOT NULL,
    father_or_husband     VARCHAR(255),
    relationship          VARCHAR(20),
    age                   INTEGER,
    address               TEXT,
    phone                 VARCHAR(50),
    email                 VARCHAR(255),

    -- Election
    party                 VARCHAR(255),
    constituency          VARCHAR(255),

    -- Education + Profession
    education             TEXT,
    profession_self       VARCHAR(255),

    -- Tax
    self_pan              VARCHAR(20),
    spouse_pan            VARCHAR(20),

    -- Criminal
    pending_cases         INTEGER,
    convictions           INTEGER,

    -- Assets
    movable_self          BIGINT,
    movable_spouse        BIGINT,
    liabilities_bank      BIGINT,
    liabilities_disputed  BIGINT,

    -- eStamp metadata
    estamp_cert           VARCHAR(50),
    estamp_date           VARCHAR(50),
    estamp_purchaser      VARCHAR(255),
    stamp_duty_rs         INTEGER,

    -- Quality + provenance
    quality_status        VARCHAR(20)  NOT NULL,
    fields_present_count  INTEGER      NOT NULL,
    fields_missing_count  INTEGER      NOT NULL,
    missing_fields        TEXT,
    extraction_notes      TEXT,
    loaded_at             DATETIME     DEFAULT CURRENT_TIMESTAMP,
    -- Acceptance status from the ECI listing scrape — never from OCR.
    -- One of: Accepted / Rejected / Withdrawn / Contesting / "" (unknown).
    -- Used by downstream migration to filter out non-contesting candidates.
    affidavit_status      VARCHAR(20)
);
"""

INDEX_SQL = [
    "CREATE INDEX IF NOT EXISTS idx_eci_prov_state_year "
    "ON eci_candidates_provisional (state, election_year);",
    "CREATE INDEX IF NOT EXISTS idx_eci_prov_quality "
    "ON eci_candidates_provisional (quality_status);",
    "CREATE INDEX IF NOT EXISTS idx_eci_prov_name "
    "ON eci_candidates_provisional (candidate_name);",
    # Composite uniqueness — replaces the old column-level UNIQUE on
    # affidavit_id, since ECI re-uses the same affidavit_id across
    # election cycles. The (state, election_year, affidavit_id) tuple is
    # globally unique.
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_eci_prov_state_year_aff "
    "ON eci_candidates_provisional (state, election_year, affidavit_id);",
]


# ---------------------------------------------------------------------------
# Quality computation
# ---------------------------------------------------------------------------

def _is_present(value: str | None) -> bool:
    if value is None:
        return False
    return bool(str(value).strip())


def compute_quality(row: dict) -> tuple[str, list[str]]:
    """Return (quality_status, missing_fields). CLEAN iff every
    EXTRACTABLE_FIELDS column has a non-empty value."""
    missing = [f for f in EXTRACTABLE_FIELDS if not _is_present(row.get(f))]
    return ("CLEAN" if not missing else "NEEDS_REVIEW"), missing


def _to_int_or_none(s: str | None) -> int | None:
    """Parse a string to int, NULL-ing OCR garbage that overflows SQLite.

    SQLite's INTEGER is 64-bit signed (max 9.22 × 10^18). Cloud Vision
    occasionally reads strings of digits like bank account numbers,
    vehicle chassis numbers, or PAN-like sequences as standalone
    numbers, which silently become 20+ digit Python ints and crash
    the INSERT. We treat anything above SQLite's max as a NULL —
    real Indian wealth/age/case-count values never come close to that.
    """
    if s is None:
        return None
    s = str(s).strip()
    if not s:
        return None
    try:
        v = int(s)
    except ValueError:
        return None
    # SQLite INTEGER is INT64 (2^63 - 1). Cap at 10^15 — well past any
    # plausible Indian-rupee wealth (Mukesh Ambani-class is ~10^13),
    # well under SQLite's limit. Anything bigger is OCR garbage.
    if abs(v) > 10**15:
        return None
    return v


def _to_str_or_none(s: str | None) -> str | None:
    if s is None:
        return None
    s = str(s).strip()
    return s if s else None


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                  formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--csv", default="data/eci/for_ai/extracted/delhi_2025_structured.csv",
                    help="Path to the structured CSV from extract_structured.py")
    ap.add_argument("--db", default="politrack.db", help="SQLite DB path")
    ap.add_argument("--state", default="Delhi",
                    help="State name (must match states.name in the DB)")
    ap.add_argument("--election-year", type=int, default=2025)
    ap.add_argument("--election-type", default="Assembly",
                    help="Assembly / Lok Sabha / Rajya Sabha")
    ap.add_argument("--drop-table", action="store_true",
                    help="Drop and recreate the table (destroys existing rows)")
    ap.add_argument("--manifest", default="",
                    help="Path to the fetcher's manifest.jsonl. When provided, "
                         "the loader uses the manifest's name / party / "
                         "constituency / status fields (which come from the "
                         "100%%-reliable listing-page scrape) to OVERRIDE the "
                         "OCR-extracted equivalents in the CSV. Strongly "
                         "recommended for cycles where the source PDFs are "
                         "image-scanned (no text layer) — OCR for these "
                         "fields can be as low as 50%% accurate.")
    args = ap.parse_args()

    # Normalize --state to TitleCase so downstream joins are case-consistent.
    # Every consumer (loader script's STATE_NAME, apply_llm_extraction's
    # JOIN on states.name, LLM JSON's state field) expects TitleCase.
    # Special case multi-word state names.
    if args.state:
        _SPECIAL = {
            "jammu and kashmir": "Jammu and Kashmir",
            "andhra pradesh":    "Andhra Pradesh",
            "arunachal pradesh": "Arunachal Pradesh",
            "himachal pradesh":  "Himachal Pradesh",
            "madhya pradesh":    "Madhya Pradesh",
            "tamil nadu":        "Tamil Nadu",
            "uttar pradesh":     "Uttar Pradesh",
            "west bengal":       "West Bengal",
            "jk":                "Jammu and Kashmir",
        }
        lc = args.state.strip().lower()
        args.state = _SPECIAL.get(lc, args.state.strip().title())

    # ── Optional manifest enrichment ──────────────────────────────────────
    # Build {affidavit_id: {name, party, constituency, status}} dict from
    # the fetcher's manifest.jsonl. These values come from the listing-
    # page HTML, so they're authoritative for these four fields.
    manifest_by_aff: dict[str, dict] = {}
    if args.manifest:
        import json as _json
        mpath = Path(args.manifest)
        if not mpath.is_absolute():
            mpath = Path.cwd() / mpath
        if not mpath.exists():
            sys.exit(f"--manifest path not found: {mpath}")
        with mpath.open() as _f:
            for _line in _f:
                if not _line.strip():
                    continue
                try:
                    _r = _json.loads(_line)
                except _json.JSONDecodeError:
                    continue
                _aff = str(_r.get("affidavit_id") or "").strip()
                if not _aff:
                    continue
                # Last-write-wins — multiple manifest entries per affidavit_id
                # happen on resumed scrapes; the values are identical, so
                # overwriting is safe.
                manifest_by_aff[_aff] = {
                    "name":         (_r.get("name") or "").strip(),
                    "party":        (_r.get("party") or "").strip(),
                    "constituency": (_r.get("constituency") or "").strip(),
                    "status":       (_r.get("status") or "").strip(),
                }
        print(f"Loaded {len(manifest_by_aff)} manifest entries for enrichment",
              file=sys.stderr)

    csv_path = Path(args.csv).resolve()
    if not csv_path.exists():
        sys.exit(f"CSV not found: {csv_path}")

    db_path = Path(args.db).resolve()
    if not db_path.exists():
        sys.exit(f"DB not found: {db_path}. Run from project root or pass --db.")

    con = sqlite3.connect(str(db_path))
    cur = con.cursor()

    if args.drop_table:
        cur.execute("DROP TABLE IF EXISTS eci_candidates_provisional;")
        print("Dropped existing eci_candidates_provisional table.",
              file=sys.stderr)

    cur.execute(SCHEMA_SQL)
    for ix in INDEX_SQL:
        cur.execute(ix)
    con.commit()

    # Load rows
    rows_in = list(csv.DictReader(csv_path.open()))
    if not rows_in:
        sys.exit(f"CSV is empty: {csv_path}")

    inserted = updated = clean_n = review_n = 0
    review_examples: list[tuple[str, list[str]]] = []

    enriched_n = 0
    affidavit_status_by_aff: dict[str, str] = {}
    for r in rows_in:
        affidavit_id = (r.get("affidavit_id") or "").strip()
        if not affidavit_id:
            continue

        # Manifest enrichment: override OCR-extracted name/party/constituency
        # with the authoritative listing-scrape values. Capture the
        # affidavit_status too so the downstream migration can filter on it.
        if affidavit_id in manifest_by_aff:
            _m = manifest_by_aff[affidavit_id]
            if _m["name"]:
                r["candidate_name"] = _m["name"]
            if _m["party"]:
                r["party"] = _m["party"]
            if _m["constituency"]:
                r["constituency"] = _m["constituency"]
            if _m["status"]:
                affidavit_status_by_aff[affidavit_id] = _m["status"]
            enriched_n += 1

        quality_status, missing_fields = compute_quality(r)
        fields_present = len(EXTRACTABLE_FIELDS) - len(missing_fields)
        fields_missing = len(missing_fields)

        if quality_status == "CLEAN":
            clean_n += 1
        else:
            review_n += 1
            if len(review_examples) < 10:
                review_examples.append((r["candidate_name"], missing_fields))

        # Check if already in table — match by COMPOSITE key
        # (state, election_year, affidavit_id) since affidavit_id is
        # re-used across cycles.
        cur.execute(
            "SELECT id FROM eci_candidates_provisional "
            "WHERE state = ? AND election_year = ? AND affidavit_id = ?",
            (args.state, args.election_year, affidavit_id),
        )
        row = cur.fetchone()
        existing_id = row[0] if row else None

        payload = {
            "affidavit_id": affidavit_id,
            "source_pdf": r.get("source_pdf"),
            "state": args.state,
            "election_year": args.election_year,
            "election_type": args.election_type,
            # Identity
            "candidate_name": r.get("candidate_name"),
            "father_or_husband": _to_str_or_none(r.get("father_or_husband")),
            "relationship": _to_str_or_none(r.get("relationship")),
            "age": _to_int_or_none(r.get("age")),
            "address": _to_str_or_none(r.get("address")),
            "phone": _to_str_or_none(r.get("phone")),
            "email": _to_str_or_none(r.get("email")),
            "party": _to_str_or_none(r.get("party")),
            "constituency": _to_str_or_none(r.get("constituency")),
            # Education + profession
            "education": _to_str_or_none(r.get("education")),
            "profession_self": _to_str_or_none(r.get("profession_self")),
            # Tax
            "self_pan": _to_str_or_none(r.get("self_pan")),
            "spouse_pan": _to_str_or_none(r.get("spouse_pan")),
            # Criminal
            "pending_cases": _to_int_or_none(r.get("pending_cases")),
            "convictions": _to_int_or_none(r.get("convictions")),
            # Assets
            "movable_self": _to_int_or_none(r.get("movable_self")),
            "movable_spouse": _to_int_or_none(r.get("movable_spouse")),
            "liabilities_bank": _to_int_or_none(r.get("liabilities_bank")),
            "liabilities_disputed": _to_int_or_none(r.get("liabilities_disputed")),
            # eStamp
            "estamp_cert": _to_str_or_none(r.get("estamp_cert")),
            "estamp_date": _to_str_or_none(r.get("estamp_date")),
            "estamp_purchaser": _to_str_or_none(r.get("estamp_purchaser")),
            "stamp_duty_rs": _to_int_or_none(r.get("stamp_duty_rs")),
            # Quality
            "quality_status": quality_status,
            "fields_present_count": fields_present,
            "fields_missing_count": fields_missing,
            "missing_fields": ",".join(missing_fields) if missing_fields else None,
            "extraction_notes": _to_str_or_none(r.get("notes")),
            # Affidavit acceptance status (from manifest enrichment, falls
            # back to whatever the CSV row had, or empty string).
            "affidavit_status": affidavit_status_by_aff.get(
                affidavit_id, _to_str_or_none(r.get("affidavit_status")) or ""
            ),
        }

        cols = list(payload.keys())
        placeholders = ",".join(["?"] * len(cols))
        col_list = ",".join(cols)

        if existing_id is None:
            cur.execute(
                f"INSERT INTO eci_candidates_provisional ({col_list}) "
                f"VALUES ({placeholders})",
                list(payload.values()),
            )
            inserted += 1
        else:
            # Update by the composite primary key (state, election_year,
            # affidavit_id) so we don't accidentally overwrite a different
            # cycle's row that happens to share the same affidavit_id.
            _skip_cols = {"affidavit_id", "state", "election_year"}
            set_clause = ",".join(f"{c}=?" for c in cols if c not in _skip_cols)
            update_vals = [payload[c] for c in cols if c not in _skip_cols]
            cur.execute(
                f"UPDATE eci_candidates_provisional SET {set_clause} "
                f"WHERE state = ? AND election_year = ? AND affidavit_id = ?",
                update_vals + [args.state, args.election_year, affidavit_id],
            )
            updated += 1

    con.commit()
    con.close()

    # Summary
    print(f"\n========== LOAD SUMMARY ==========", file=sys.stderr)
    print(f"  Table: eci_candidates_provisional in {db_path}", file=sys.stderr)
    print(f"  Election: {args.state} {args.election_year} {args.election_type}",
          file=sys.stderr)
    print(f"  Rows inserted: {inserted}", file=sys.stderr)
    print(f"  Rows updated:  {updated}", file=sys.stderr)
    print(f"  CLEAN:         {clean_n}", file=sys.stderr)
    print(f"  NEEDS_REVIEW:  {review_n}  (revisit these later)", file=sys.stderr)
    if manifest_by_aff:
        print(f"  Manifest enrichment: {enriched_n} rows had "
              f"name/party/constituency overridden from listing scrape",
              file=sys.stderr)
    print(file=sys.stderr)
    print(f"Sample of NEEDS_REVIEW candidates (first 10):", file=sys.stderr)
    for name, missing in review_examples:
        print(f"  {name[:35]:35s}  missing {len(missing):>2d} field(s): "
              f"{', '.join(missing[:4])}{'...' if len(missing) > 4 else ''}",
              file=sys.stderr)

    print(f"\nQuery the table:", file=sys.stderr)
    print(f"  sqlite3 {db_path} \"SELECT quality_status, COUNT(*) "
          f"FROM eci_candidates_provisional GROUP BY quality_status;\"",
          file=sys.stderr)
    print(f"  sqlite3 {db_path} \"SELECT candidate_name, missing_fields "
          f"FROM eci_candidates_provisional WHERE quality_status='NEEDS_REVIEW' "
          f"LIMIT 10;\"", file=sys.stderr)


if __name__ == "__main__":
    main()
