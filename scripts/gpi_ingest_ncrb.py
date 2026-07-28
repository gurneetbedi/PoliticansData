"""NCRB Crime in India 2024 → GPI Law & Order indicator values.

NCRB tables use a different layout than RBI Handbook:
    • Table label sits at the BOTTOM of each page (as a signature) — not the top.
    • Data rows start with a serial number [1..36], then state/UT name, then
      numeric columns. Number of numeric columns varies per table (Table 1A.1
      has 8, Table 3A.1 has 6, etc.).
    • RATES (per 100k population) are pre-computed by NCRB and sit as the
      second-to-last column across all state-wise crime-rate tables — so we
      always extract value_position = -2 for the rate.
    • Each NCRB edition covers a single reporting year (calendar year, not
      fiscal). Crime in India 2024 → fiscal_year 2024 in our schema
      (schema aligns to ending calendar year of the fiscal year window).

Ingested indicators (from Crime in India 2024):
    LO01  IPC crime rate            V1 · Table 1A.1  · Rate per 100k pop
    LO02  IPC conviction rate       V3 · Table 18A.2 · Page 4 of 4 · %
    LO03  Crime against women rate  V1 · Table 3A.1  · Rate per 100k women
    LO04  Cybercrime rate           V2 · Table 9A.1  · Rate per 100k pop

Usage:
    # Ingest all 4 Law & Order indicators
    python scripts/gpi_ingest_ncrb.py \\
        --pdf-dir "data/ncrb/crime-in-india-2024"

    # Dry-run to preview
    python scripts/gpi_ingest_ncrb.py \\
        --pdf-dir "data/ncrb/crime-in-india-2024" --dry-run

    # One indicator only
    python scripts/gpi_ingest_ncrb.py \\
        --pdf-dir "data/ncrb/crime-in-india-2024" --indicators LO01
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
# Config — one entry per LO indicator we source from NCRB
# ═══════════════════════════════════════════════════════════════════════════
VOLUME_FILES = {
    "V1": "11CrimeinIndia2024-VolumeI.pdf",
    "V2": "2CrimeinIndia2024-VolumeII.pdf",
    "V3": "3CrimeinIndia2024-VolumeIII1.pdf",
}

NCRB_TABLES = {
    "LO01": {
        "volume": "V1",
        "table_id": "1A.1",
        "title": "IPC/BNS Crimes (State/UT-wise) - 2022-2024",
        "value_year": 2024,
        "value_position": -2,   # Rate is 2nd from right
        "unit": "per 100k",
        "notes": "Rate of cognizable crimes (IPC + BNS) per lakh population, 2024.",
    },
    "LO03": {
        "volume": "V1",
        "table_id": "3A.1",
        "title": "Crime against Women (State/UT-wise) - 2022-2024",
        "value_year": 2024,
        "value_position": -2,
        "unit": "per 100k women",
        "notes": "Crime against women rate per lakh female population, 2024.",
    },
    "LO04": {
        "volume": "V2",
        "table_id": "9A.1",
        "title": "Cyber Crimes (State/UT-wise) - 2022-2024",
        "value_year": 2024,
        "value_position": -2,
        "unit": "per 100k",
        "notes": "Cybercrime rate per lakh population, 2024.",
    },
    "LO02": {
        "volume": "V3",
        "table_id": "18A.2",
        "title": "Court Disposal of IPC/BNS Crime Cases (Concluded page)",
        # Table 18A.2 spans 4 pages; the Conviction Rate column is only on
        # the "Concluded" (4th) page. page_marker narrows to that page.
        "page_marker": "Conviction Rate",
        "value_year": 2024,
        "value_position": -2,
        "unit": "%",
        "notes": "IPC/BNS Conviction Rate = convicted / trials completed * 100.",
    },
    "LO06": {
        "volume": "V3",
        "table_id": "18A.2",
        "title": "Court Disposal of IPC/BNS Crime Cases — Pendency Percentage",
        # Same page as LO02 (the 4th "Concluded" page of Table 18A.2), but the
        # Pendency % column is the LAST column (position -1), whereas LO02's
        # Conviction Rate sits at position -2.
        "page_marker": "Pendency Percentage",
        "value_year": 2024,
        "value_position": -1,
        "unit": "%",
        "notes": ("IPC/BNS Pendency Percentage = cases pending trial at year end / "
                    "total cases for trial × 100. Higher = slower justice."),
    },
}


# ═══════════════════════════════════════════════════════════════════════════
# State-name normalization (same alias table as RBI Handbook)
# ═══════════════════════════════════════════════════════════════════════════
STATE_ALIASES = {
    "orissa":                     "Odisha",
    "odisha":                     "Odisha",
    "jammu & kashmir":            "Jammu and Kashmir",
    "jammu and kashmir":          "Jammu and Kashmir",
    "j&k":                        "Jammu and Kashmir",
    "j & k":                      "Jammu and Kashmir",
    "nct of delhi":               "Delhi",
    "delhi":                      "Delhi",
    "chattisgarh":                "Chhattisgarh",
    "chhattisgarh":               "Chhattisgarh",
    "uttaranchal":                "Uttarakhand",
    "uttarakhand":                "Uttarakhand",
    "pondicherry":                "Puducherry",
    "puducherry":                 "Puducherry",
    "a&n islands":                "Andaman and Nicobar Islands",
    "andaman & nicobar islands":  "Andaman and Nicobar Islands",
    "andaman and nicobar islands":"Andaman and Nicobar Islands",
    "d&n haveli and daman & diu": None,   # merged UT we don't track separately
    "dadra & nagar haveli":       None,
    "daman & diu":                None,
    "all india":                  None,
    "total state(s)":             None,
    "total ut(s)":                None,
    "total all india":            None,
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
# Parser
# ═══════════════════════════════════════════════════════════════════════════
NUM_TOKEN_RE = re.compile(
    r"^-?[\d,]+(?:\.\d+)?\*?$"   # 1,23,456 / 12.5 / 9*
    r"|^-\*?$"                    # missing
)
TABLE_SIG_RE = re.compile(r"\bTABLE\s+([\w.]+)\b")


def find_table_pages(reader, table_id: str, page_marker: str | None = None
                       ) -> list[int]:
    """Return 0-indexed pages where TABLE {table_id} appears. If page_marker
    is set, further filter to pages containing that substring — useful for
    multi-page NCRB tables where only one page has our target column."""
    hits = []
    for i, page in enumerate(reader.pages):
        text = page.extract_text()
        for m in TABLE_SIG_RE.finditer(text):
            if m.group(1) == table_id:
                if page_marker and page_marker not in text:
                    continue
                hits.append(i)
                break
    return hits


def parse_ncrb_page(text: str, value_position: int
                      ) -> tuple[dict[str, float], list[str]]:
    """Extract {state_name: value_at_position} from an NCRB page.

    Data-row shape:  SL_number  state_name (1+ words)  num1 num2 ... numN
    We identify data rows by: first token is a serial number (1-40 range),
    followed by state-name tokens, followed by a contiguous numeric block.
    """
    results = {}
    unresolved = []
    lines = text.split("\n")
    for raw in lines:
        line = raw.strip()
        tokens = line.split()
        if not tokens:
            continue
        # SL must be a small integer (1-40 covers 28 states + 8 UTs).
        if not re.match(r"^\d{1,3}$", tokens[0]):
            continue
        try:
            sl = int(tokens[0])
        except ValueError:
            continue
        if sl < 1 or sl > 40:
            continue

        # Walk right → find the contiguous numeric block that ends the row.
        num_end = len(tokens)
        num_start = num_end
        while num_start > 1 and NUM_TOKEN_RE.match(tokens[num_start - 1]):
            num_start -= 1
        if num_start >= num_end or num_start <= 1:
            continue

        numbers = tokens[num_start:num_end]
        state_tokens = tokens[1:num_start]
        if not state_tokens:
            continue
        state_name_raw = " ".join(state_tokens)

        if len(numbers) < abs(value_position):
            continue
        cleaned = numbers[value_position].replace(",", "").rstrip("*").strip()
        if cleaned in ("", "-"):
            continue
        try:
            val = float(cleaned)
        except ValueError:
            continue

        canonical = normalize_state_name(state_name_raw)
        if canonical is None:
            unresolved.append(state_name_raw)
            continue
        results[canonical] = val

    return results, unresolved


# ═══════════════════════════════════════════════════════════════════════════
# DB insert
# ═══════════════════════════════════════════════════════════════════════════
def insert_values(session, indicator_code: str, state_values: dict[str, float],
                    config: dict, source_pdf: str, dry_run: bool) -> dict:
    from app.gpi_models import GpiIndicator, GpiIndicatorValue
    from app.models import State

    ind = session.query(GpiIndicator).filter_by(code=indicator_code).one_or_none()
    if not ind:
        raise SystemExit(f"Indicator {indicator_code} not seeded — run gpi_seed.py")

    states_by_name = {s.name: s for s in session.query(State).all()}
    counts = {"inserted": 0, "updated": 0, "skipped_state": 0}

    year = config["value_year"]
    for state_name, val in state_values.items():
        st = states_by_name.get(state_name)
        if not st:
            counts["skipped_state"] += 1
            continue

        existing = session.query(GpiIndicatorValue).filter_by(
            indicator_id=ind.id, state_id=st.id, fiscal_year=year,
        ).one_or_none()

        payload = {
            "raw_value":         val,
            "source_url":        "https://www.ncrb.gov.in/crime-in-india.html",
            "source_document":   f"NCRB Crime in India 2024 · {config['title']} · via {source_pdf}",
            "extraction_method": "pypdf_local",
            "staleness":         "current",
            "notes":             config["notes"],
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


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                  formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--pdf-dir", required=True,
                    help="Directory containing the 3 NCRB volume PDFs")
    ap.add_argument("--indicators", default=None,
                    help="Comma-separated LO codes (default: all)")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    try:
        from pypdf import PdfReader
    except ImportError:
        raise SystemExit("pip install pypdf")

    pdf_dir = Path(args.pdf_dir)
    if not pdf_dir.is_dir():
        raise SystemExit(f"Not a directory: {pdf_dir}")

    targets = list(NCRB_TABLES.keys())
    if args.indicators:
        want = set(args.indicators.split(","))
        targets = [t for t in targets if t in want]

    print(f"NCRB PDF dir: {pdf_dir}")
    print(f"Indicators:   {targets}\n")

    from app.database import SessionLocal
    from app import models  # ensures gpi_models loaded
    session = SessionLocal()

    # Cache PdfReader per volume to avoid re-opening the same file 4x
    reader_cache: dict[str, "PdfReader"] = {}
    total = {"inserted": 0, "updated": 0, "skipped_state": 0}

    try:
        for code in targets:
            config = NCRB_TABLES[code]
            print(f"═══ {code} — Table {config['table_id']} ({config['volume']}) ═══")
            print(f"    {config['title']}")

            vol = config["volume"]
            if vol not in reader_cache:
                vol_path = pdf_dir / VOLUME_FILES[vol]
                if not vol_path.exists():
                    print(f"    ✗ Missing volume file: {vol_path}")
                    continue
                reader_cache[vol] = PdfReader(str(vol_path))
            reader = reader_cache[vol]

            # 1. Locate pages
            pages = find_table_pages(reader, config["table_id"],
                                       page_marker=config.get("page_marker"))
            if not pages:
                print(f"    ✗ Table {config['table_id']} not found in {vol}")
                continue
            print(f"    Pages found (0-indexed): {pages}")

            # 2. Parse
            state_values: dict[str, float] = {}
            unresolved_all = []
            for p in pages:
                text = reader.pages[p].extract_text()
                page_result, unresolved = parse_ncrb_page(text, config["value_position"])
                state_values.update(page_result)
                unresolved_all.extend(unresolved)

            print(f"    ✓ Parsed {len(state_values)} states")
            if unresolved_all:
                print(f"    ⚠ Unresolved rows: {sorted(set(unresolved_all))[:5]}")

            # Sanity-check Punjab
            if "Punjab" in state_values:
                print(f"    Punjab: {state_values['Punjab']} {config['unit']}")

            # 3. Insert
            counts = insert_values(session, code, state_values, config,
                                     VOLUME_FILES[vol], args.dry_run)
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
