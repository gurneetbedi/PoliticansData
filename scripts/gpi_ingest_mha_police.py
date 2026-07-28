"""MHA Lok Sabha Q246 → GPI LO05 Police strength per 100k population.

Data source:
    Lok Sabha Unstarred Question No. 246 (4 February 2025) — Police-Population
    Ratio. A single-page Annexure with state/UT × year (2019-2023) police per
    lakh population, sourced by MHA from BPRD's "Data on Police Organisations".

    This is our preferred source because:
      - BPRD site (bprd.gov.in) is frequently unavailable
      - MHA hosts the same underlying data in a compact 1-page table
      - Response is official Parliament record — high authoritativeness
      - Covers all 36 states/UTs with 5-year time series

Target indicator:
    LO05 — Police strength per 100k population. Direction: higher_better.

We ingest the LATEST column (as on 01.01.2023) and tag it fiscal_year=2023.
Historical years (2019-2022) are also written so the state page can show a
5-year trend if desired.

Usage:
    # Download the PDF (small — ~50KB, single page):
    mkdir -p data/mha
    curl -L -o data/mha/LS_Q246_04022025_police_pop_ratio.pdf \\
        "https://www.mha.gov.in/MHA1/Par2017/pdfs/par2025-pdfs/LS04022025/246.pdf"

    # Ingest:
    python scripts/gpi_ingest_mha_police.py \\
        --pdf data/mha/LS_Q246_04022025_police_pop_ratio.pdf

    # Dry-run
    python scripts/gpi_ingest_mha_police.py --pdf ... --dry-run
"""
from __future__ import annotations
import argparse
import re
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


# The Annexure header lists 5 year columns:
#   As on 01.01.2019  01.01.2020  01.01.2021  01.01.2022  01.01.2023
# We map each column to a fiscal_year for our schema (ending calendar year).
YEAR_COLUMNS = [2019, 2020, 2021, 2022, 2023]

# Only the latest snapshot drives LO05 pillar scoring. Older years still
# get written so the UI can show trend, but they'll be superseded when
# next year's BPRD/MHA data is released.
LATEST_YEAR = 2023

SOURCE_URL = "https://www.mha.gov.in/MHA1/Par2017/pdfs/par2025-pdfs/LS04022025/246.pdf"

# ═══════════════════════════════════════════════════════════════════════════
# State-name normalization
# ═══════════════════════════════════════════════════════════════════════════
STATE_ALIASES = {
    "orissa":                       "Odisha",
    "odisha":                       "Odisha",
    "jammu & kashmir":              "Jammu and Kashmir",
    "jammu and kashmir":            "Jammu and Kashmir",
    "j&k":                          "Jammu and Kashmir",
    "nct of delhi":                 "Delhi",
    "delhi":                        "Delhi",
    "chhattisgarh":                 "Chhattisgarh",
    "chattisgarh":                  "Chhattisgarh",
    "uttarakhand":                  "Uttarakhand",
    "puducherry":                   "Puducherry",
    "pondicherry":                  "Puducherry",
    # UTs we don't score in GPI
    "a & n islands":                None,
    "andaman & nicobar islands":    None,
    "andaman and nicobar islands":  None,
    "chandigarh":                   None,
    "lakshadweep":                  None,
    "ladakh":                       None,
    "dadra and nagar haveli and daman & diu": None,
    "dadra and nagar haveli":       None,
    "daman & diu":                  None,
    "all india":                    None,
    "india":                        None,
}


def normalize_state_name(raw: str) -> str | None:
    if not raw:
        return None
    # Strip trailing "#", "@", "*", ".", commas, footnote markers
    n = re.sub(r"[.*#$@,]", "", str(raw)).strip().lower()
    n = re.sub(r"\s+", " ", n)
    if n in STATE_ALIASES:
        return STATE_ALIASES[n]
    return " ".join(w.capitalize() for w in n.split())


# ═══════════════════════════════════════════════════════════════════════════
# Parser
# ═══════════════════════════════════════════════════════════════════════════
# Data rows in the Annexure look like:
#   "20. Punjab 273.89 286.50 246.59 237.12 241.02"
#   "1. Andhra Pradesh - 113.68 115.35 167.67 165.89"
#   "34. Ladakh   569.05 563.51 822.82 873.67"
# Serial number (1-36), state name (1+ words, may contain "&"), then 4 or 5
# numeric columns. "-", "--", "..", missing entries left-align, so we allow
# a variable number of numeric tokens per row (we right-align to YEAR_COLUMNS).
DATA_ROW_RE = re.compile(r"^(\d{1,2})\.\s+(.+)$")
NUM_TOKEN_RE = re.compile(r"^-?\d+(?:\.\d+)?$")
# A placeholder token = the row's slot for that year is empty. We keep the
# slot to preserve column alignment but store None. Common in the MHA PDF for
# states that hadn't reported yet (e.g., Andhra Pradesh 2019 = "-", Ladakh 2019
# blank, Telangana 2019 = "--", D&NH+D&D 2019 = "#" footnote-only).
PLACEHOLDER_TOKENS = {"-", "--", "...", "..", ".", "—", "NA", "N.A.", "N/A", "*", "@", "#"}


def parse_annexure(text: str) -> dict[str, dict[int, float]]:
    """Return {state_name_raw: {year: value}} from Annexure text."""
    out: dict[str, dict[int, float]] = {}
    lines = text.split("\n")

    buffer_state: str | None = None
    buffer_data_cells: list[str] = []

    def flush():
        nonlocal buffer_state, buffer_data_cells
        if buffer_state and buffer_data_cells:
            # buffer_data_cells is a list of str tokens (numbers or placeholders),
            # length = number of year columns in the row.
            # Right-align: last cell → 2023, prior → 2022, etc.
            years_slice = YEAR_COLUMNS[-len(buffer_data_cells):] if len(buffer_data_cells) <= len(YEAR_COLUMNS) else YEAR_COLUMNS
            cells_slice = buffer_data_cells[-len(YEAR_COLUMNS):] if len(buffer_data_cells) > len(YEAR_COLUMNS) else buffer_data_cells
            parsed = {}
            for y, tok in zip(years_slice, cells_slice):
                if tok in PLACEHOLDER_TOKENS:
                    continue     # skip empty slots
                try:
                    parsed[y] = float(tok)
                except ValueError:
                    continue
            if parsed:
                out[buffer_state] = parsed
        buffer_state = None
        buffer_data_cells = []

    for raw in lines:
        line = raw.strip()
        if not line:
            continue

        # Try match "N. State name numbers…"
        m = DATA_ROW_RE.match(line)
        if m:
            flush()
            body = m.group(2).strip()
            tokens = body.split()
            # State-name prefix ends at the first token that is either a
            # numeric value OR a data placeholder ("-", "--", etc.). This
            # correctly handles rows like "Andhra Pradesh - 113.68 115.35 …"
            # where the state's 2019 value is a dash.
            first_data_idx = None
            for i, t in enumerate(tokens):
                if NUM_TOKEN_RE.match(t) or t in PLACEHOLDER_TOKENS:
                    first_data_idx = i
                    break
            if first_data_idx is None:
                continue    # no data on this line — probably a wrapped heading
            buffer_state = " ".join(tokens[:first_data_idx]).strip()
            buffer_data_cells = [t for t in tokens[first_data_idx:]
                                    if NUM_TOKEN_RE.match(t) or t in PLACEHOLDER_TOKENS]
            continue

        # No "N." prefix — could be a continuation line (wrapped values for
        # a state whose row overflowed). Only treat as continuation if we're
        # buffering AND the line is purely data tokens.
        tokens = line.split()
        purely_data = tokens and all(NUM_TOKEN_RE.match(t) or t in PLACEHOLDER_TOKENS for t in tokens)
        if buffer_state and purely_data:
            buffer_data_cells.extend(tokens)

    flush()
    return out


# ═══════════════════════════════════════════════════════════════════════════
# DB insert
# ═══════════════════════════════════════════════════════════════════════════
def insert_values(session, state_values: dict[str, dict[int, float]],
                    source_pdf: str, dry_run: bool) -> dict:
    from app.gpi_models import GpiIndicator, GpiIndicatorValue
    from app.models import State

    ind = session.query(GpiIndicator).filter_by(code="LO05").one_or_none()
    if not ind:
        raise SystemExit("Indicator LO05 not seeded — run gpi_seed.py")

    states_by_name = {s.name: s for s in session.query(State).all()}
    counts = {"inserted": 0, "updated": 0, "skipped": 0, "unresolved": []}

    for raw, years in state_values.items():
        canon = normalize_state_name(raw)
        if canon is None:
            counts["skipped"] += 1
            continue

        st = states_by_name.get(canon)
        if not st:
            counts["unresolved"].append((raw, canon))
            continue

        for year, val in years.items():
            existing = session.query(GpiIndicatorValue).filter_by(
                indicator_id=ind.id, state_id=st.id, fiscal_year=year,
            ).one_or_none()

            payload = {
                "raw_value":         val,
                "source_url":        SOURCE_URL,
                "source_document":   ("MHA Lok Sabha Unstarred Q246 (04-Feb-2025) · "
                                        f"Annexure Police-Pop Ratio · {source_pdf}"),
                "extraction_method": "pypdf_local",
                "staleness":         "current" if year == LATEST_YEAR else "historical",
                "notes":             (f"As on 01.01.{year}. Source: BPRD DoPO via "
                                        f"MHA Parliament Q246. Total police per lakh population."),
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
                    indicator_id=ind.id, state_id=st.id, fiscal_year=year,
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
    ap.add_argument("--pdf", required=True,
                    help="Path to MHA Lok Sabha Q246 PDF (single-page Annexure)")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    try:
        from pypdf import PdfReader
    except ImportError:
        raise SystemExit("pip install pypdf")

    pdf_path = Path(args.pdf)
    if not pdf_path.exists():
        raise SystemExit(f"No such file: {pdf_path}")

    reader = PdfReader(str(pdf_path))
    # Data is on the Annexure page (usually page 2). Concatenate all pages
    # and parse — the parser only picks up rows matching the "N. State ..."
    # pattern, so cover pages don't produce false matches.
    text = ""
    for page in reader.pages:
        text += (page.extract_text() or "") + "\n"

    raw_values = parse_annexure(text)
    print(f"Parsed {len(raw_values)} row(s) from PDF")

    if not raw_values:
        raise SystemExit("No data rows found — PDF format may have changed. "
                          "Inspect the PDF and adjust parser.")

    from app.database import SessionLocal
    session = SessionLocal()
    counts = insert_values(session, raw_values, pdf_path.name, args.dry_run)

    print()
    print(f"LO05 Police strength per 100k — writes for years {min(YEAR_COLUMNS)}-{max(YEAR_COLUMNS)}")
    print(f"  Inserted:  {counts['inserted']}")
    print(f"  Updated:   {counts['updated']}")
    print(f"  Skipped:   {counts['skipped']} (UTs/all-India rows)")
    if counts["unresolved"]:
        print(f"  Unresolved names ({len(counts['unresolved'])}):")
        for raw, canon in counts["unresolved"]:
            print(f"    '{raw}' → '{canon}'")

    # Sanity summary
    if not args.dry_run:
        from app.gpi_models import GpiIndicator, GpiIndicatorValue
        from app.models import State
        ind = session.query(GpiIndicator).filter_by(code="LO05").one()
        rows = (session.query(GpiIndicatorValue, State)
                .join(State, State.id == GpiIndicatorValue.state_id)
                .filter(GpiIndicatorValue.indicator_id == ind.id,
                        GpiIndicatorValue.fiscal_year == LATEST_YEAR)
                .all())
        rows.sort(key=lambda r: r[0].raw_value, reverse=True)
        print()
        print(f"Top 5 police per 100k (FY{LATEST_YEAR}):")
        for v, st in rows[:5]:
            print(f"  {st.name:<25s} {v.raw_value:>7.2f}")
        print("  ...")
        print(f"Bottom 5:")
        for v, st in rows[-5:]:
            print(f"  {st.name:<25s} {v.raw_value:>7.2f}")

    session.close()
    if args.dry_run:
        print("\n(dry-run — no writes)")
    print("\nNext: python scripts/gpi_compute_scores.py")


if __name__ == "__main__":
    main()
