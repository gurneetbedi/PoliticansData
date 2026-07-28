"""UDISE+ Flash Statistics PDF → GPI Education indicator values.

UDISE+ (Unified District Information System for Education Plus) is the MoE
GoI's official annual school-education statistics publication. Each year's
"Flash Statistics" PDF has ~40 tables covering enrolment, teachers, dropouts,
infrastructure, and school amenities.

State-wise data is presented in clean state × sub-column tables in Section 6
(Performance Indicators) and Section 8 (Amenities Summary), which the pypdf
right-to-left numeric parser handles reliably.

We use the Non-NEP (traditional school-structure) variant so "Secondary" =
Grades 9-10, consistent with our spec's ED02 definition. NEP variants (where
Secondary = Grades 9-12) exist too — use --structure nep to parse those.

Ingested indicators:
    ED02  GER Secondary (%)                 Table 6.1  · Secondary col 12/15
    ED03  Drop-out rate Secondary (%)       Table 6.13 · Secondary col 12/15
    ED04  Pupil-Teacher Ratio Secondary     Table 4.12 · Secondary col
    ED06  Schools with functional electricity (%)  Table 8.6 · All mgmt col

Usage:
    python scripts/gpi_ingest_udise.py \\
        --pdf "data/udise/2025-NonNep.pdf" --fiscal-year 2025

    # Preview extraction without DB writes
    python scripts/gpi_ingest_udise.py \\
        --pdf "data/udise/2025-NonNep.pdf" --fiscal-year 2025 --dry-run

    # One indicator only (debugging)
    python scripts/gpi_ingest_udise.py \\
        --pdf "data/udise/2025-NonNep.pdf" --fiscal-year 2025 --indicators ED02
"""
from __future__ import annotations
import argparse
import re
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


# ═══════════════════════════════════════════════════════════════════════════
# Per-indicator config
# ═══════════════════════════════════════════════════════════════════════════
# Column layout for tables with State + 5 levels × 3 (Boys/Girls/Total):
#   State Pri-B Pri-G Pri-T UP-B UP-G UP-T Elm-B Elm-G Elm-T Sec-B Sec-G Sec-T HS-B HS-G HS-T
#   Positions (1-indexed within numeric block): 1..15
#   Secondary Total = position 12
LEVEL_5_GENDER_3 = {
    "expected_columns": 15,
    "secondary_total_col_1idx": 12,
}

UDISE_TABLES = {
    "ED02": {
        # GER Table 6.1: State + 5 levels × 3 (B/G/T) = 15 numeric cols
        # Secondary Total = column 12
        "table_id": "6.1",
        "title_marker": "Gross Enrolment Ratio",
        "title_subtitle": "All Social Groups",
        "layout": {"expected_columns": 15},
        "value_col_1idx": 12,
        "unit": "%",
        "compute": "direct",
    },
    "ED03": {
        # Dropout Table 6.13: State + 3 levels × 3 (B/G/T) = 9 numeric cols
        # (Primary / Upper Primary / Secondary — no Elementary or HS in this table)
        # Secondary Total = column 9 (last)
        "table_id": "6.13",
        "title_marker": "Dropout Rate",
        "title_subtitle": None,
        "layout": {"expected_columns": 9},
        "value_col_1idx": 9,
        "unit": "%",
        "compute": "direct",
    },
    "ED04": {
        # PTR Table 4.12: State + 4 levels (single value each) = 4 numeric cols
        # Secondary (9-10) = column 3
        "table_id": "4.12",
        "title_marker": "Pupil Teacher Ratio",
        "title_subtitle": None,
        "layout": {"expected_columns": 4},
        "value_col_1idx": 3,
        "unit": "ratio",
        "compute": "direct",
    },
    "ED06": {
        # Amenities Table 8.6 (electricity): State + 9 mgmt-type cols
        # All Management (composite) = column 1
        "table_id": "8.6",
        "title_marker": "functional electricity",
        "title_subtitle": None,
        "layout": {"expected_columns": 9},
        "value_col_1idx": 1,
        "unit": "%",
        "compute": "direct",
    },
}


# ═══════════════════════════════════════════════════════════════════════════
# State-name normalization (same alias table as RBI Handbook)
# ═══════════════════════════════════════════════════════════════════════════
STATE_ALIASES = {
    "orissa":                                     "Odisha",
    "odisha":                                     "Odisha",
    "jammu & kashmir":                            "Jammu and Kashmir",
    "jammu and kashmir":                          "Jammu and Kashmir",
    "j&k":                                        "Jammu and Kashmir",
    "j & k":                                      "Jammu and Kashmir",
    "nct of delhi":                               "Delhi",
    "delhi":                                      "Delhi",
    "chattisgarh":                                "Chhattisgarh",
    "chhattisgarh":                               "Chhattisgarh",
    "uttaranchal":                                "Uttarakhand",
    "uttarakhand":                                "Uttarakhand",
    "pondicherry":                                "Puducherry",
    "puducherry":                                 "Puducherry",
    "a&n islands":                                "Andaman and Nicobar Islands",
    "andaman & nicobar islands":                  "Andaman and Nicobar Islands",
    "andaman and nicobar islands":                "Andaman and Nicobar Islands",
    # UDISE-specific: merged UT
    "daman & diu and dadra & nagar haveli":       None,
    "dadra & nagar haveli":                       None,
    "daman & diu":                                None,
    # Non-state totals
    "india":                                      None,
    "all india":                                  None,
    "total":                                      None,
}


def normalize_state_name(raw: str) -> str | None:
    if not raw:
        return None
    n = re.sub(r"[.*#$@]", "", str(raw)).strip().lower()
    n = re.sub(r"\s+", " ", n)
    if n in STATE_ALIASES:
        return STATE_ALIASES[n]
    return " ".join(w.capitalize() for w in n.split())


# ═══════════════════════════════════════════════════════════════════════════
# Academic-year detection — read the PDF, don't trust the filename
# ═══════════════════════════════════════════════════════════════════════════
# UDISE tables carry their academic year in the title:
#   "Table 6.1: Gross Enrolment Ratio ... 2025-26: All Social Groups"
# We scan the first 30 pages for such patterns and return the most-common
# academic year. The GPI fiscal_year is the ENDING calendar year of that
# academic year (2025-26 → 2026).
ACADEMIC_YEAR_RE = re.compile(r"\b(20\d{2})-(\d{2})\b")


def detect_academic_year(pdf_path: Path) -> tuple[str, int] | None:
    """Return (academic_year_label, fiscal_year_int) from PDF content, or None.
    Reads early pages, tallies '20XX-YY' patterns, picks most common."""
    from pypdf import PdfReader
    from collections import Counter

    reader = PdfReader(str(pdf_path))
    matches = []
    for i in range(min(30, len(reader.pages))):
        text = reader.pages[i].extract_text() or ""
        for m in ACADEMIC_YEAR_RE.finditer(text):
            start = int(m.group(1))
            end_short = int(m.group(2))
            # Reconstruct 4-digit end year (2025-26 → 2026, 2019-20 → 2020)
            end_full = (start // 100) * 100 + end_short
            if end_full <= start:            # rolls over (e.g. 2099-00)
                end_full += 100
            matches.append((f"{start}-{str(end_full)[-2:]}", end_full))

    if not matches:
        return None
    top = Counter(matches).most_common(1)[0][0]
    return top


# ═══════════════════════════════════════════════════════════════════════════
# Parser
# ═══════════════════════════════════════════════════════════════════════════
NUM_TOKEN_RE = re.compile(
    r"^-?[\d,]+(?:\.\d+)?\*?$"    # 12.5 / 1,234 / 9*
    r"|^-\*?$"                     # missing
)

TABLE_TITLE_RE = re.compile(r"Table\s+(\d+\.\d+)[:\s]", re.I)


def find_table_pages(reader, table_id: str, title_marker: str,
                       title_subtitle: str | None = None) -> list[int]:
    """Scan the PDF for pages whose TOP contains 'Table N.M: {title}'.
    Optional title_subtitle further narrows (e.g. 'All Social Groups' vs 'SC')."""
    hits = []
    for i, page in enumerate(reader.pages):
        head = page.extract_text()[:500]
        m = TABLE_TITLE_RE.search(head)
        if not m:
            continue
        if m.group(1) != table_id:
            continue
        if title_marker.lower() not in head.lower():
            continue
        if title_subtitle and title_subtitle.lower() not in head.lower():
            continue
        hits.append(i)
    return hits


def parse_state_row(line: str, expected_cols: int) -> tuple[str, list[float]] | None:
    """From a data row, return (state_name, [numeric values]).
    Uses right-to-left numeric extraction — grabs exactly expected_cols
    numeric tokens from the end; everything before is the state name."""
    tokens = line.strip().split()
    if len(tokens) < expected_cols + 1:
        return None

    # Walk from right, collecting numeric tokens
    values_r = []
    take_idx = len(tokens) - 1
    while take_idx >= 0 and len(values_r) < expected_cols:
        if NUM_TOKEN_RE.match(tokens[take_idx]):
            values_r.append(tokens[take_idx])
            take_idx -= 1
        else:
            break
    if len(values_r) != expected_cols:
        return None

    values_str = list(reversed(values_r))
    state_tokens = tokens[: take_idx + 1]
    if not state_tokens:
        return None

    # Skip if state_tokens is only a column-index like "(16)"
    joined = " ".join(state_tokens).strip()
    if joined.startswith("(") and joined.endswith(")"):
        return None

    # Parse values
    parsed = []
    for tok in values_str:
        cleaned = tok.replace(",", "").rstrip("*").strip()
        if cleaned in ("", "-"):
            parsed.append(None)
            continue
        try:
            parsed.append(float(cleaned))
        except ValueError:
            parsed.append(None)

    return joined, parsed


def extract_udise_indicator(pdf_path: Path, config: dict
                              ) -> tuple[dict[str, float], list[str], list[int]]:
    """Return ({state: value}, unresolved_state_names, found_pages)."""
    try:
        from pypdf import PdfReader
    except ImportError:
        raise SystemExit("pip install pypdf")

    reader = PdfReader(str(pdf_path))
    pages = find_table_pages(reader, config["table_id"], config["title_marker"],
                                config.get("title_subtitle"))
    if not pages:
        return {}, [], []

    expected = config["layout"]["expected_columns"]
    col_idx = config["value_col_1idx"] - 1   # 0-indexed

    results: dict[str, float] = {}
    unresolved = []

    for pg in pages:
        text = reader.pages[pg].extract_text()
        # Data rows are all lines below the column-header block.
        # Simplest approach: try to parse EVERY line as a state row and skip
        # rows that don't match the expected column count.
        for line in text.split("\n"):
            parsed = parse_state_row(line, expected)
            if not parsed:
                continue
            state_raw, values = parsed
            state = normalize_state_name(state_raw)
            if state is None:
                unresolved.append(state_raw)
                continue
            val = values[col_idx]
            if val is not None:
                results[state] = val

    return results, unresolved, [p + 1 for p in pages]


# ═══════════════════════════════════════════════════════════════════════════
# DB insert
# ═══════════════════════════════════════════════════════════════════════════
def insert_values(session, indicator_code: str, state_values: dict[str, float],
                    config: dict, fiscal_year: int, source_pdf: str,
                    dry_run: bool) -> dict:
    from app.gpi_models import GpiIndicator, GpiIndicatorValue
    from app.models import State

    ind = session.query(GpiIndicator).filter_by(code=indicator_code).one_or_none()
    if not ind:
        raise SystemExit(f"Indicator {indicator_code} not seeded — run gpi_seed.py")

    states_by_name = {s.name: s for s in session.query(State).all()}
    counts = {"inserted": 0, "updated": 0, "skipped_state": 0}

    for state_name, val in state_values.items():
        st = states_by_name.get(state_name)
        if not st:
            counts["skipped_state"] += 1
            continue

        existing = session.query(GpiIndicatorValue).filter_by(
            indicator_id=ind.id, state_id=st.id, fiscal_year=fiscal_year,
        ).one_or_none()

        payload = {
            "raw_value":         val,
            "source_url":        "https://udiseplus.gov.in/",
            "source_document":   f"UDISE+ Flash Statistics · Table {config['table_id']} "
                                    f"({config['title_marker']}) · via {source_pdf}",
            "extraction_method": "pypdf_local",
            "staleness":         "current",
            "notes":             f"pypdf right-to-left extract; "
                                    f"col {config['value_col_1idx']}/{config['layout']['expected_columns']} "
                                    f"(Secondary Total)",
            "extracted_at":      datetime.utcnow(),
        }

        if existing:
            for k, v in payload.items():
                setattr(existing, k, v)
            existing.normalized_value = None
            existing.national_rank = None
            counts["updated"] += 1
        else:
            session.add(GpiIndicatorValue(
                indicator_id=ind.id, state_id=st.id, fiscal_year=fiscal_year,
                **payload,
            ))
            counts["inserted"] += 1

    if not dry_run:
        session.commit()
    return counts


# ═══════════════════════════════════════════════════════════════════════════
# Main
# ═══════════════════════════════════════════════════════════════════════════
def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                  formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--pdf", required=True, help="Path to UDISE+ Flash Statistics PDF")
    ap.add_argument("--fiscal-year", type=int, default=None,
                    help="Override auto-detected fiscal year (ending year of AY, "
                          "e.g. 2026 for AY 2025-26). Default: auto-detected from PDF.")
    ap.add_argument("--indicators", default=None,
                    help="Comma-separated codes (e.g. ED02,ED03). Default: all")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    pdf_path = Path(args.pdf)
    if not pdf_path.exists():
        raise SystemExit(f"No such file: {pdf_path}")

    # Auto-detect academic year from PDF content
    detected = detect_academic_year(pdf_path)
    if detected:
        ay_label, ay_fy = detected
        print(f"UDISE+ PDF:      {pdf_path}")
        print(f"Detected AY:     {ay_label}  → fiscal_year = {ay_fy}")
        if args.fiscal_year is not None:
            if args.fiscal_year != ay_fy:
                print(f"⚠ Override:      user set --fiscal-year={args.fiscal_year}, "
                      f"but PDF says {ay_fy}. Using {args.fiscal_year}.")
            fiscal_year = args.fiscal_year
        else:
            fiscal_year = ay_fy
    else:
        if args.fiscal_year is None:
            raise SystemExit(
                "Could not detect academic year from PDF; provide --fiscal-year explicitly."
            )
        print(f"UDISE+ PDF:      {pdf_path}")
        print(f"Detected AY:     (none — using --fiscal-year={args.fiscal_year})")
        fiscal_year = args.fiscal_year

    targets = list(UDISE_TABLES.keys())
    if args.indicators:
        want = set(args.indicators.split(","))
        targets = [t for t in targets if t in want]

    print(f"Fiscal year:     {fiscal_year}")
    print(f"Indicators:      {targets}")
    print()

    from app.database import SessionLocal
    from app import models  # ensures gpi_models loaded
    session = SessionLocal()

    total = {"inserted": 0, "updated": 0, "skipped_state": 0}

    try:
        for code in targets:
            config = UDISE_TABLES[code]
            print(f"═══ {code} — Table {config['table_id']} ═══")
            print(f"    {config['title_marker']}")

            state_values, unresolved, found_pages = extract_udise_indicator(
                pdf_path, config)

            if not found_pages:
                print(f"    ✗ Table {config['table_id']} not found in PDF")
                continue
            print(f"    Pages found: {found_pages}")
            print(f"    ✓ Parsed {len(state_values)} states")
            if unresolved:
                print(f"    ⚠ Unresolved: {sorted(set(unresolved))[:5]}")

            # Sanity — show Punjab
            if "Punjab" in state_values:
                print(f"    Punjab: {state_values['Punjab']} {config['unit']}")

            counts = insert_values(session, code, state_values, config,
                                     fiscal_year, pdf_path.name, args.dry_run)
            print(f"    ↳ ins={counts['inserted']} upd={counts['updated']} "
                  f"skip_state={counts['skipped_state']}")
            for k in total:
                total[k] += counts[k]
            print()
    finally:
        session.close()

    print("═══════════════ Summary ═══════════════")
    for k, v in total.items():
        print(f"  {k:<20s} {v}")
    if args.dry_run:
        print("  (DRY RUN — no writes)")
    print()
    print("Next: python scripts/gpi_compute_scores.py")


if __name__ == "__main__":
    main()
