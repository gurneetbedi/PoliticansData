"""NITI Aayog Multi-Dimensional Poverty Index → E05 for all states.

Source: RBI Handbook 2024-25 Table 10 (which republishes NITI Aayog's
"National MPI: A Progress Review 2023"). Page 42 of the Handbook PDF has all
36 States/UTs with 6 columns:

    NFHS-4 (2015-16)               NFHS-5 (2019-21)
    Headcount% | Intensity% | MPI  Headcount% | Intensity% | MPI

We ingest column 4 (NFHS-5 Headcount Ratio) — the % of population identified
as multidimensionally poor. Direction: lower_better.

Fiscal-year assignment: 2020 (NFHS-5 midpoint, matches how H06 is stored).
Later, when NFHS-6 is published, re-run this script pointed at the new
Handbook edition — new rows will land at FY2025 or FY2026 alongside the old
NFHS-5 baseline.

Usage:
    python scripts/gpi_ingest_niti_mpi.py \\
        --pdf "data/Statistics on Indian States/Statistics on Indian States.pdf"
    python scripts/gpi_ingest_niti_mpi.py --pdf ... --dry-run
"""
from __future__ import annotations
import argparse
import re
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


# Fiscal year we tag the NFHS-5 datapoint with. NFHS-5 fieldwork ran
# 2019-21 → midpoint 2020 → fiscal_year 2020 in our schema.
NFHS5_FY = 2020

# T10 header contains "TABLE 10" prefix — used to locate the page.
TABLE_MARKER = "TABLE 10"

# We want the 4th numeric column: NFHS-5 Headcount Ratio.
COL_INDEX = 3   # 0-indexed → 4th value in the row

# Same state-alias table as the RBI Handbook parser.
STATE_ALIASES = {
    "orissa":                             "Odisha",
    "odisha":                             "Odisha",
    "jammu & kashmir":                    "Jammu and Kashmir",
    "jammu and kashmir":                  "Jammu and Kashmir",
    "j&k":                                "Jammu and Kashmir",
    "nct of delhi":                       "Delhi",
    "delhi":                              "Delhi",
    "chattisgarh":                        "Chhattisgarh",
    "chhattisgarh":                       "Chhattisgarh",
    "uttaranchal":                        "Uttarakhand",
    "uttarakhand":                        "Uttarakhand",
    "pondicherry":                        "Puducherry",
    "puducherry":                         "Puducherry",
    # UTs we don't score in GPI (skip silently):
    "andaman & nicobar islands":          None,
    "chandigarh":                         None,
    "dadra & nagar haveli & daman & diu": None,
    "dadra & nagar haveli":               None,
    "daman & diu":                        None,
    "ladakh":                             None,
    "lakshadweep":                        None,
    "all india":                          None,
    "all-india":                          None,
    "india":                              None,
}


NUM_RE = re.compile(r"^-?\d+(?:\.\d+)?$")


def canonical_state(raw: str) -> str | None:
    """Return DB state name or None to skip."""
    key = raw.strip().lower()
    key = re.sub(r"\s+", " ", key)
    if key in STATE_ALIASES:
        return STATE_ALIASES[key]
    # Fallback: title-case, hope it matches DB
    return raw.strip().title()


def find_table_page(reader) -> int:
    """Return the 0-indexed page number that contains 'TABLE 10' at the top."""
    for i in range(min(len(reader.pages), 100)):
        text = reader.pages[i].extract_text() or ""
        head = "\n".join(text.split("\n")[:4])
        if TABLE_MARKER in head.upper() and "MULTI-DIMENSIONAL POVERTY" in text.upper():
            return i
    raise SystemExit(f"Could not find '{TABLE_MARKER}' page in PDF")


def parse_mpi_page(text: str) -> dict[str, float]:
    """Extract {state_name_raw: headcount_ratio_nfhs5} from T10 page text."""
    out: dict[str, float] = {}
    lines = text.split("\n")

    for line in lines:
        line = line.strip()
        if not line:
            continue

        tokens = line.split()

        # Collect trailing numeric tokens (from the right)
        nums_rev: list[str] = []
        take_idx = len(tokens) - 1
        while take_idx >= 0 and len(nums_rev) < 6:
            t = tokens[take_idx].replace(",", "")
            if NUM_RE.match(t):
                nums_rev.append(t)
                take_idx -= 1
            else:
                break
        nums = list(reversed(nums_rev))

        # Expect exactly 6 numeric columns; skip rows with fewer.
        if len(nums) != 6:
            continue

        state_raw = " ".join(tokens[: take_idx + 1]).strip()
        if not state_raw:
            continue

        # Skip footnotes / notes rows.
        if state_raw.lower().startswith(("notes", "note", "source")):
            continue

        headcount = float(nums[COL_INDEX])
        out[state_raw] = headcount

    return out


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                  formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--pdf", required=True,
                    help="Path to Statistics on Indian States.pdf")
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
    page_idx = find_table_page(reader)
    print(f"Found T10 at page {page_idx + 1}")

    text = reader.pages[page_idx].extract_text() or ""
    raw_values = parse_mpi_page(text)
    print(f"Parsed {len(raw_values)} rows from the page")

    # ── Persist ──
    from app.database import SessionLocal
    from app.gpi_models import GpiIndicator, GpiIndicatorValue
    from app.models import State

    session = SessionLocal()
    ind = session.query(GpiIndicator).filter_by(code="E05").one_or_none()
    if not ind:
        raise SystemExit("Indicator E05 not seeded — run gpi_seed.py")

    states_by_name = {s.name: s for s in session.query(State).all()}
    counts = {"inserted": 0, "updated": 0, "skipped": 0, "unresolved": []}

    for raw_name, val in raw_values.items():
        canon = canonical_state(raw_name)
        if canon is None:
            counts["skipped"] += 1
            continue

        st = states_by_name.get(canon)
        if not st:
            counts["unresolved"].append((raw_name, canon))
            continue

        existing = session.query(GpiIndicatorValue).filter_by(
            indicator_id=ind.id, state_id=st.id, fiscal_year=NFHS5_FY,
        ).one_or_none()

        payload = {
            "raw_value":         val,
            "source_url":        "https://www.niti.gov.in/national-multidimensional-poverty-index",
            "source_document":   ("NITI Aayog · National MPI: A Progress Review 2023 · "
                                    "via RBI Handbook 2024-25 T10 (NFHS-5 Headcount Ratio)"),
            "extraction_method": "pypdf_local",
            "staleness":         "current",
            "notes":             ("NFHS-5 fieldwork 2019-21. Headcount Ratio = % of "
                                    "population identified as multidimensionally poor."),
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
                indicator_id=ind.id, state_id=st.id, fiscal_year=NFHS5_FY,
                **payload,
            ))
            counts["inserted"] += 1

    if not args.dry_run:
        session.commit()

    print()
    print(f"E05 MPI (NFHS-5 Headcount Ratio) — FY{NFHS5_FY}")
    print(f"  Inserted:  {counts['inserted']}")
    print(f"  Updated:   {counts['updated']}")
    print(f"  Skipped:   {counts['skipped']} (UTs / all-India rows)")
    if counts["unresolved"]:
        print(f"  Unresolved names ({len(counts['unresolved'])}):")
        for raw, canon in counts["unresolved"]:
            print(f"    '{raw}'  → '{canon}'")

    # Show top 5 lowest / highest to sanity-check
    if not args.dry_run:
        print()
        print("Sample E05 values:")
        rows = (session.query(GpiIndicatorValue, State)
                .join(State, State.id == GpiIndicatorValue.state_id)
                .filter(GpiIndicatorValue.indicator_id == ind.id,
                        GpiIndicatorValue.fiscal_year == NFHS5_FY)
                .all())
        rows.sort(key=lambda r: r[0].raw_value)
        for v, st in rows[:3]:
            print(f"  best  {st.name:<25s} {v.raw_value:.2f}%")
        print("  ...")
        for v, st in rows[-3:]:
            print(f"  worst {st.name:<25s} {v.raw_value:.2f}%")

    session.close()
    if args.dry_run:
        print("\n(dry-run — no writes)")
    print("\nNext: python scripts/gpi_compute_scores.py")


if __name__ == "__main__":
    main()
