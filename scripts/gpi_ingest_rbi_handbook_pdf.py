"""RBI Handbook of Statistics on Indian States (full PDF) → GPI indicator values.

The individual per-table XLSX files RBI publishes only contain the FIRST page
of each multi-page table — the (Concld.) continuation with recent years
(2017-18 through 2024-25) is only in the consolidated PDF.

Parses the PDF locally with pypdf (no external API — free, fast, deterministic).
The RBI Handbook tables are laid out cleanly enough that pypdf's text output
looks like:

    State/Union Territory
    Base: 2011-12
    2011-12 2012-13 2013-14 2014-15 2015-16 2016-17
    Andaman & Nicobar Islands 3,97,843 4,15,648 4,48,839 4,74,163 5,09,208 5,75,196
    Andhra Pradesh 3,79,40,203 3,80,62,901 4,07,11,475 ...
    ...

Parser strategy: right-to-left numeric-token extraction. We consume N number-
shaped tokens from the end of each data row (N = number of year headers found),
then everything remaining on the left is the state name. This handles multi-
word state names ("Jammu & Kashmir", "Andaman & Nicobar Islands", etc.) without
special-casing each one.

Usage:
    # Ingest all configured indicators from the RBI Handbook
    python scripts/gpi_ingest_rbi_handbook_pdf.py \\
        --pdf "data/Statistics on Indian States/Statistics on Indian States.pdf"

    # One indicator only (useful for debugging)
    python scripts/gpi_ingest_rbi_handbook_pdf.py \\
        --pdf "data/Statistics on Indian States/Statistics on Indian States.pdf" \\
        --indicators E01

    # Dry-run — parse and preview without touching the DB
    python scripts/gpi_ingest_rbi_handbook_pdf.py \\
        --pdf "data/Statistics on Indian States/Statistics on Indian States.pdf" \\
        --dry-run
"""
from __future__ import annotations
import argparse
import json
import re
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


# ═══════════════════════════════════════════════════════════════════════════
# Per-indicator configuration
#
# `pages` is a tuple of (start, end) PDF-page numbers (1-indexed, inclusive).
# Includes both the main page and any (Concld.) continuation so we capture
# the full year range in one shot.
#
# `compute` decides post-processing:
#   "direct"      — cell value = indicator value
#   "yoy_growth"  — cell values are absolute levels; compute year-on-year %
# ═══════════════════════════════════════════════════════════════════════════
HANDBOOK_TABLES = {
    # ── Economy pillar ──────────────────────────────────────────────────────
    "E01": {
        "table_num": 22,
        "title": "Gross State Domestic Product (Constant Prices)",
        "unit_in_pdf": "₹ Lakh (absolute)",
        "compute": "yoy_growth",
        "notes": "Constant prices, base 2011-12; compute real GSDP growth %.",
    },
    "E02": {
        "table_num": 19,
        "title": "Per Capita Net State Domestic Product (Current Prices)",
        "unit_in_pdf": "₹ per capita",
        "compute": "direct",
        "notes": "Per-capita income proxy.",
    },
    "E03": {
        "table_num": 9,
        "title": "State-wise Unemployment Rate (Usual Status, Urban Overall)",
        "unit_in_pdf": "per 1,000 (converted to %)",
        # Table 9 spans 3 sub-tables: Male / Female / Overall. We want only
        # Overall — pick it by matching the sub-title of the page. The generic
        # find_table_pages() locates all pages of Table 9; page_filter narrows to
        # the "(Concld.)" one which is Overall.
        "page_filter": "(Concld.)",
        "compute": "direct",
        "scale_factor": 0.1,  # per 1,000 → percentage
        "notes": "Urban Overall from Table 9 Concld. Rural is Table 8; blend later.",
    },
    # E05 (MPI) deliberately not in this config — Table 10 has a 2-group × 3-metric
    # (Headcount, Intensity, MPI × NFHS-4 & NFHS-5) layout that the generic parser
    # can't disambiguate. Add via a custom parser in a follow-up.

    # ── Public Finance pillar ──────────────────────────────────────────────
    "F03": {
        "table_num": 168,
        "title": "State-wise Own Tax Revenue",
        "unit_in_pdf": "₹ Crore (absolute)",
        "compute": "yoy_growth",
        "notes": "YoY % growth of own tax revenue.",
    },
    # F01/F02/F05/F06 need GSDP or Revenue Receipts as denominators to express
    # as % of GSDP. Deferred to a follow-up ratio-compute step once Table 21
    # (GSDP Current Prices) is also ingested.

    # ── Healthcare pillar ──────────────────────────────────────────────────
    "H01": {
        "table_num": 4,
        "title": "State-wise Infant Mortality Rate",
        "unit_in_pdf": "per 1,000 live births",
        "compute": "direct",
        "notes": "SRS annual IMR.",
    },

    # ── Infrastructure pillar ──────────────────────────────────────────────
    "I01": {
        "table_num": 144,
        "title": "State-wise Length of National Highways",
        "unit_in_pdf": "km",
        "compute": "direct",
        "notes": "NH length only. Proxy — replace when MoRTH Basic Road Statistics "
                  "(all-road density) is ingested.",
    },
    "I02": {
        "table_num": 138,
        "title": "State-wise Per Capita Availability of Power",
        "unit_in_pdf": "kWh/person",
        "compute": "direct",
        "notes": "Per-capita electricity supply. Proxy for I02.",
    },
    "I04": {
        "table_num": 149,
        "title": "State-wise Telephones per 100 Population",
        "unit_in_pdf": "per 100",
        "compute": "direct",
        "notes": "Total telecom (fixed + mobile) per 100. Proxy for broadband.",
    },
    "I06": {
        "table_num": 150,
        "title": "State-wise Road Constructed under PMGSY",
        "unit_in_pdf": "km cumulative",
        "compute": "direct",
        "notes": "Cumulative PMGSY-built rural road length.",
    },

    # ── Public Finance ratios (numerator ÷ GSDP × 100) ─────────────────────
    # These use compute="ratio" — the parser extracts BOTH the numerator table
    # AND Table 21 (GSDP Current Prices), converts units (Crore vs Lakh), and
    # writes the resulting %-of-GSDP as the indicator's raw_value.
    "F01": {
        "table_num": 164,
        "title": "Gross Fiscal Deficit as % of GSDP",
        "unit_in_pdf": "₹ Crore (converted to % of GSDP)",
        "compute": "ratio",
        "denominator_table_num": 21,            # GSDP Current Prices
        "numerator_unit": "crore",
        "denominator_unit": "lakh",
        "notes": "Table 164 (₹ Crore) ÷ Table 21 (₹ Lakh, unit-converted) × 100.",
    },
    "F05": {
        "table_num": 165,
        "title": "Revenue Deficit as % of GSDP",
        "unit_in_pdf": "₹ Crore (converted to % of GSDP)",
        "compute": "ratio",
        "denominator_table_num": 21,
        "numerator_unit": "crore",
        "denominator_unit": "lakh",
        "notes": "Table 165 (₹ Crore) ÷ Table 21 (₹ Lakh, unit-converted) × 100. "
                  "Negative values indicate a revenue surplus.",
    },
    "F04": {
        "table_num": 174,     # Capital Outlay (numerator)
        "title": "Capital Outlay as % of Total Expenditure",
        "unit_in_pdf": "% of total expenditure",
        "compute": "ratio",
        # Denominator = Revenue Expenditure (166) + Capital Expenditure (173)
        # We use Cap OUTLAY as numerator (productive asset creation only),
        # not Cap EXPENDITURE which includes debt discharge / loan repayment.
        # The denominator stays as full Total Exp (Rev + Cap) per standard
        # budget accounting.
        "denominator_tables": [166, 173],
        "numerator_unit": "crore",
        "denominator_unit": "crore",
        "notes": "Cap Outlay (T174) / Total Expenditure (T166 + T173) × 100. "
                  "Higher = more investment in creating productive assets. Excludes "
                  "debt discharge and loan repayments from the numerator.",
    },

    # ── Healthcare — sporadic NFHS-based ────────────────────────────────────
    "H06": {
        "table_num": 15,
        "title": "Pregnant Women Aged 15-49 Years who are Anaemic (NFHS-5)",
        "unit_in_pdf": "%",
        "compute": "direct",
        "notes": "NFHS-5 (2019-21) survey. Only 1 data point per state (fiscal_year "
                  "≈ 2020). Values in parens (unreliable-sample flag) are extracted "
                  "but confidence is lower.",
    },
}


# ═══════════════════════════════════════════════════════════════════════════
# State-name normalization
# ═══════════════════════════════════════════════════════════════════════════
STATE_ALIASES = {
    "orissa":                 "Odisha",
    "odisha":                 "Odisha",
    "jammu & kashmir":        "Jammu and Kashmir",
    "jammu and kashmir":      "Jammu and Kashmir",
    "jammu & kashmir (ut)":   "Jammu and Kashmir",
    "j&k":                    "Jammu and Kashmir",
    "j & k":                  "Jammu and Kashmir",
    "nct of delhi":           "Delhi",
    "delhi (ut)":             "Delhi",
    "delhi":                  "Delhi",
    "chattisgarh":            "Chhattisgarh",
    "chhattisgarh":           "Chhattisgarh",
    "uttaranchal":            "Uttarakhand",
    "uttarakhand":            "Uttarakhand",
    "pondicherry":            "Puducherry",
    "puducherry":             "Puducherry",
    "all-india":              None,
    "all india":              None,
    "india":                  None,
    "average":                None,
    "total":                  None,
}


def normalize_state_name(raw: str) -> str | None:
    """Return canonical DB state name, or None to skip."""
    if not raw:
        return None
    # Strip footnote markers like "Jammu & Kashmir*"
    n = re.sub(r"[*#$@]+$", "", str(raw)).strip()
    n = re.sub(r"[.]", "", n).strip().lower()
    n = re.sub(r"\s+", " ", n)
    if n in STATE_ALIASES:
        return STATE_ALIASES[n]
    return " ".join(w.capitalize() for w in n.split())


# ═══════════════════════════════════════════════════════════════════════════
# Table parsing — the core of the local parser
# ═══════════════════════════════════════════════════════════════════════════

# A value token: Indian-formatted number (with commas) or plain float, plus
# common "missing value" sentinels. Also accepts parenthesised values like
# "(53.7)" which RBI/NFHS use to flag statistically-unreliable samples —
# the number is still extracted; the paren markers are stripped when parsing.
NUM_TOKEN_RE = re.compile(
    r"^\(?-?[\d,]+(?:\.\d+)?\*?\)?$"   # 1,23,456 / -12.5 / 0.7 / 9* / (53.7)
    r"|^-\*?$"                          # single dash = missing
)

# Year in fiscal format (2015-16, 2015-2016) — ends in the LATER calendar year.
FISCAL_YEAR_RE = re.compile(r"\b(\d{4})-(\d{2,4})\b")
# Year as a plain 4-digit calendar year (2015, 2020, etc.). We only treat these
# as year headers when 3+ appear near each other in the header block, otherwise
# they may just be footnote references.
CAL_YEAR_RE = re.compile(r"(?<![\d\-])(19\d{2}|20\d{2})(?![\d\-])")

# A "state-name-like" line start: capitalised word(s), possibly with & and dots,
# followed by whitespace and then a number/dash — the shape of every RBI data row.
STATE_ROW_START_RE = re.compile(
    r"^([A-Z][A-Za-z]*(?:[ &.\-][A-Za-z][A-Za-z]*)*\*?)\s+[\d,\-]"
)


def extract_years_from_header_block(lines: list[str]) -> tuple[list[int], int]:
    """Return (years, data_start_line_idx).

    Walks lines top-down until it hits the first data row (state-name pattern),
    collecting year tokens along the way. Handles multi-line headers where
    annotations like '(A) / (RE) / (BE)' split year labels across lines.
    """
    year_ints: list[int] = []
    year_source: str | None = None  # "fiscal" | "calendar"

    for i, line in enumerate(lines):
        stripped = line.strip()
        if not stripped:
            continue

        # If this line starts like a data row AND we already have some year
        # tokens, the header block is over.
        if STATE_ROW_START_RE.match(stripped) and year_ints:
            return year_ints, i

        # Prefer fiscal-year format when present. Once we've locked source
        # as fiscal, ignore any bare 4-digit numbers on later header lines.
        fiscal_matches = FISCAL_YEAR_RE.findall(line)
        if fiscal_matches:
            if year_source is None:
                year_source = "fiscal"
            if year_source == "fiscal":
                year_ints.extend(int(m[0]) + 1 for m in fiscal_matches)
            continue

        # No fiscal on this line — try calendar years, but only if we haven't
        # already committed to fiscal (mixing formats within one table is rare).
        if year_source in (None, "calendar"):
            cal_matches = [int(y) for y in CAL_YEAR_RE.findall(line)]
            # Need at least 3 plausible year values on one line to trust it
            # (avoids false positives from footnote refs like "in 2020, ...").
            plausible = [y for y in cal_matches if 1990 <= y <= 2100]
            if len(plausible) >= 3:
                if year_source is None:
                    year_source = "calendar"
                year_ints.extend(plausible)

    return year_ints, len(lines)


def parse_pdf_page_text(text: str) -> tuple[list[int], list[dict]]:
    """Return (years_in_header, list_of_state_rows).

    Each state row is {"state_name_raw": str, "values": {year: float}}.
    """
    lines = text.split("\n")

    years, data_start_idx = extract_years_from_header_block(lines)
    if not years:
        return [], []

    n_years = len(years)

    # ── Walk data rows below the header ─────────────────────────────────────
    rows = []
    for line in lines[data_start_idx:]:
        line = line.strip()
        if not line:
            continue

        # Stop conditions — footnotes / all-india row / notes
        low = line.lower()
        if any(low.startswith(prefix) for prefix in (
            "all india", "all-india", "notes:", "note:", "source:", "sources:",
            "^:", "*:", "#:", "-:", ".:",
        )):
            # Don't break — RBI sometimes puts footnotes between tables in
            # a multi-table page. Just skip the line.
            continue

        tokens = line.split()

        # From the right, grab exactly n_years numeric tokens.
        values_reversed = []
        take_idx = len(tokens) - 1
        while take_idx >= 0 and len(values_reversed) < n_years:
            t = tokens[take_idx]
            if NUM_TOKEN_RE.match(t):
                values_reversed.append(t)
                take_idx -= 1
            else:
                break

        values = list(reversed(values_reversed))

        # Skip if we didn't collect at least a few numbers — probably a
        # section-header or blank data row.
        if len(values) < 3:
            continue

        # State name is whatever tokens remain on the left.
        state_name = " ".join(tokens[: take_idx + 1]).strip()
        if not state_name:
            continue

        # Some rows are "State (Concld.)"-style continuations without state
        # names above. Guard.
        if "concld" in state_name.lower():
            continue

        # Values might be shorter than years (blanks trimmed from the right).
        # Right-align: last extracted value corresponds to last year in header.
        year_slice = years[-len(values):] if len(values) < n_years else years

        parsed_values: dict[int, float] = {}
        for year, tok in zip(year_slice, values):
            # RBI tables mark provisional / footnote-referenced values with
            # trailing "*" (e.g. "9*", "-*"); NFHS uses "(53.7)" for unreliable
            # samples. Strip all these before float-parsing.
            cleaned = tok.replace(",", "").rstrip("*").strip("()").strip()
            if cleaned in ("", "-"):
                continue
            try:
                num = float(cleaned)
            except ValueError:
                continue
            parsed_values[year] = num

        if parsed_values:
            rows.append({
                "state_name_raw": state_name,
                "values": parsed_values,
            })

    return years, rows


TABLE_TITLE_RE = re.compile(r"TABLE\s+(\d+):", re.IGNORECASE)


def find_table_pages(reader, table_num: int, page_filter: str | None = None
                       ) -> list[int]:
    """Scan the PDF for pages whose header says 'TABLE {N}:'.

    Every RBI Handbook page carries its table number in the header — this
    lets us locate a table without hardcoding page numbers. Some tables
    span multiple sub-tables (e.g. Table 9: Male / Female / Concld.);
    pass page_filter to narrow to pages containing that substring in their
    header (e.g. '(Concld.)' to pick only the Overall sub-table).

    Returns 0-indexed page numbers.
    """
    hits = []
    for i, page in enumerate(reader.pages):
        # Only need the first ~300 chars — table title lives at the top
        head = page.extract_text()[:300]
        m = TABLE_TITLE_RE.search(head)
        if not m:
            continue
        if int(m.group(1)) != table_num:
            continue
        if page_filter and page_filter not in head:
            continue
        hits.append(i)
    return hits


def extract_table_from_pdf(pdf_path: Path, table_num: int,
                             page_filter: str | None = None
                             ) -> tuple[dict[str, dict[int, float]], list[str], list[int]]:
    """Return ({state_name: {year: value}}, unresolved_raw_names, page_numbers).

    Auto-locates the table's pages by title, then parses each. Multiple pages
    (main + Concld.) get merged per state — later pages' year values append
    to earlier ones (typical layout: earlier years on p1, newer on p2).
    """
    try:
        from pypdf import PdfReader
    except ImportError:
        raise SystemExit("pip install pypdf")

    reader = PdfReader(str(pdf_path))
    page_indices = find_table_pages(reader, table_num, page_filter)
    if not page_indices:
        return {}, [], []

    combined: dict[str, dict[int, float]] = {}
    unresolved = []

    for page_idx in page_indices:
        text = reader.pages[page_idx].extract_text()
        years, rows = parse_pdf_page_text(text)
        for row in rows:
            canonical = normalize_state_name(row["state_name_raw"])
            if canonical is None:
                unresolved.append(row["state_name_raw"])
                continue
            combined.setdefault(canonical, {}).update(row["values"])

    return combined, unresolved, [p + 1 for p in page_indices]  # 1-indexed for display


# ═══════════════════════════════════════════════════════════════════════════
# Post-processing
# ═══════════════════════════════════════════════════════════════════════════
def compute_yoy_growth(state_values: dict[int, float]) -> dict[int, float]:
    years = sorted(state_values.keys())
    growth = {}
    for i in range(1, len(years)):
        prev, curr = years[i-1], years[i]
        if curr - prev != 1:
            continue
        v_prev, v_curr = state_values[prev], state_values[curr]
        if v_prev == 0:
            continue
        growth[curr] = round(100.0 * (v_curr - v_prev) / v_prev, 2)
    return growth


# Multiplier to convert a value expressed in `unit` to ₹ Lakh
# (the common base unit we use to align numerator/denominator).
_UNIT_TO_LAKH = {
    "lakh":  1.0,
    "crore": 100.0,    # 1 Crore = 100 Lakh
}


def compute_ratio(numerator: dict[int, float], denominator: dict[int, float],
                   num_unit: str, den_unit: str) -> dict[int, float]:
    """Return {year: numerator_pct_of_denominator} for years present in both.

    Handles unit alignment — RBI mixes ₹ Crore (fiscal tables) and ₹ Lakh
    (GSDP tables). Both get normalized to Lakh before dividing.
    """
    num_scale = _UNIT_TO_LAKH.get(num_unit.lower(), 1.0)
    den_scale = _UNIT_TO_LAKH.get(den_unit.lower(), 1.0)
    out = {}
    for year, num_val in numerator.items():
        den_val = denominator.get(year)
        if den_val in (None, 0):
            continue
        num_lakh = num_val * num_scale
        den_lakh = den_val * den_scale
        out[year] = round(100.0 * num_lakh / den_lakh, 2)
    return out


# ═══════════════════════════════════════════════════════════════════════════
# DB insert
# ═══════════════════════════════════════════════════════════════════════════
def insert_matrix(session, indicator_code: str, state_year_values: dict,
                    config: dict, pdf_name: str, dry_run: bool) -> dict:
    from app.gpi_models import GpiIndicator, GpiIndicatorValue
    from app.models import State

    ind = session.query(GpiIndicator).filter_by(code=indicator_code).one_or_none()
    if not ind:
        raise SystemExit(f"Indicator {indicator_code} not in DB — run gpi_seed.py first")

    states_by_name = {s.name: s for s in session.query(State).all()}
    counts = {"inserted": 0, "updated": 0, "skipped_state": 0, "skipped_year": 0}

    for state_name, year_values in state_year_values.items():
        st = states_by_name.get(state_name)
        if not st:
            counts["skipped_state"] += 1
            continue

        # Apply unit conversion FIRST (before YoY math, so growth rates
        # are unaffected by rescaling — the ratio is unit-invariant anyway).
        scale = config.get("scale_factor")
        if scale:
            year_values = {y: v * scale for y, v in year_values.items()}

        if config["compute"] == "yoy_growth":
            final = compute_yoy_growth(year_values)
        else:
            final = year_values

        for year, value in final.items():
            if year < 2018 or year > 2026:
                counts["skipped_year"] += 1
                continue

            existing = session.query(GpiIndicatorValue).filter_by(
                indicator_id=ind.id, state_id=st.id, fiscal_year=year
            ).one_or_none()

            payload = {
                "raw_value":         value,
                "source_url":        "https://www.rbi.org.in/scripts/AnnualPublications.aspx?head=Handbook%20of%20Statistics%20on%20Indian%20States",
                "source_document":   f"RBI Handbook 2024-25 · Table {config['table_num']}: {config['title']} · via {pdf_name}",
                "extraction_method": "pypdf_local",
                "staleness":         "current",
                "notes":             f"pypdf text-extraction; compute={config['compute']}",
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
    ap.add_argument("--pdf", required=True, help="Path to the RBI Handbook PDF")
    ap.add_argument("--indicators", default=None,
                    help="Comma-separated codes (e.g. E01,E02). Default: all configured")
    ap.add_argument("--dry-run", action="store_true", help="Parse but don't write to DB")
    args = ap.parse_args()

    pdf_path = Path(args.pdf)
    if not pdf_path.exists():
        raise SystemExit(f"No such file: {pdf_path}")

    targets = list(HANDBOOK_TABLES.keys())
    if args.indicators:
        want = set(args.indicators.split(","))
        targets = [t for t in targets if t in want]
    if not targets:
        raise SystemExit("No matching indicators in HANDBOOK_TABLES")

    print(f"RBI Handbook: {pdf_path.name}")
    print(f"Indicators:   {targets}")
    print(f"Extraction:   pypdf (local, no API)")
    print()

    from app.database import SessionLocal
    from app import models  # ensures gpi_models loaded
    session = SessionLocal()

    total = {"inserted": 0, "updated": 0, "skipped_state": 0, "skipped_year": 0}
    try:
        # Small cache so we don't re-parse Table 21 for every fiscal-ratio indicator
        denominator_cache: dict[int, dict] = {}

        for code in targets:
            config = HANDBOOK_TABLES[code]
            print(f"═══ {code} — Table {config['table_num']} ═══")
            print(f"    {config['title']}")

            # 1. Auto-locate + parse the primary (numerator) table
            state_year, unresolved, found_pages = extract_table_from_pdf(
                pdf_path,
                config["table_num"],
                page_filter=config.get("page_filter"),
            )
            if not found_pages:
                print(f"    ✗ Table {config['table_num']} not found in PDF")
                continue
            print(f"    Pages found: {found_pages}"
                  f"{' (filter: ' + config['page_filter'] + ')' if config.get('page_filter') else ''}")
            print(f"    ✓ Parsed {len(state_year)} states")

            # 2. Ratio mode: parse denominator table(s) — one or more, summed
            if config.get("compute") == "ratio":
                # Support both forms: single `denominator_table_num` (e.g. F01,
                # F05 → Table 21) or `denominator_tables` list (e.g. F04 →
                # Rev Exp + Cap Exp = Total Expenditure).
                den_tables = config.get(
                    "denominator_tables",
                    [config["denominator_table_num"]] if "denominator_table_num" in config else [],
                )
                if not den_tables:
                    print(f"    ✗ Ratio mode but no denominator tables configured")
                    continue

                # Load each denominator table (cached across indicators).
                den_data_list = []
                for dt in den_tables:
                    if dt not in denominator_cache:
                        print(f"    ↳ Loading denominator Table {dt} ...")
                        data, _, _ = extract_table_from_pdf(pdf_path, dt)
                        denominator_cache[dt] = data
                        print(f"    ↳ Denominator Table {dt}: {len(data)} states")
                    den_data_list.append(denominator_cache[dt])

                # Sum the denominators per (state, year) — used for F04 where
                # Total Expenditure = Revenue Exp + Capital Exp.
                combined_den: dict[str, dict[int, float]] = {}
                for data in den_data_list:
                    for state, year_vals in data.items():
                        combined_den.setdefault(state, {})
                        for yr, val in year_vals.items():
                            combined_den[state][yr] = combined_den[state].get(yr, 0.0) + val

                # Compute per-state ratios
                new_state_year = {}
                for state, num_by_year in state_year.items():
                    den_by_year = combined_den.get(state)
                    if not den_by_year:
                        continue
                    ratios = compute_ratio(
                        num_by_year, den_by_year,
                        config["numerator_unit"], config["denominator_unit"],
                    )
                    if ratios:
                        new_state_year[state] = ratios
                state_year = new_state_year
                print(f"    ↳ Ratios computed for {len(state_year)} states "
                      f"(denominator = {'+'.join(f'T{t}' for t in den_tables)})")
            if unresolved:
                sample = sorted(set(unresolved))[:5]
                print(f"    ⚠ Unresolved state rows: {sample}"
                      f"{' ...' if len(set(unresolved)) > 5 else ''}")

            # Punjab sanity check
            if "Punjab" in state_year:
                yrs = sorted(state_year["Punjab"].keys())
                sample = [(y, state_year["Punjab"][y]) for y in yrs[-4:]]
                print(f"    Punjab last 4: {sample}")
                if config["compute"] == "yoy_growth":
                    growth = compute_yoy_growth(state_year["Punjab"])
                    growth_sample = sorted(growth.items())[-4:]
                    print(f"    Punjab growth: {growth_sample}")

            # 2. Insert
            counts = insert_matrix(session, code, state_year, config,
                                     pdf_path.name, args.dry_run)
            print(f"    ↳ ins={counts['inserted']} upd={counts['updated']}  "
                  f"skip_state={counts['skipped_state']} skip_year={counts['skipped_year']}")

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
