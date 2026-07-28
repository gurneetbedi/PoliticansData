"""BPRD Data on Police Organisations → GPI LO05 Police strength per 100k.

The Bureau of Police Research & Development (MHA) publishes an annual "Data on
Police Organisations" (DoPO) report — a ~600-page PDF with state-wise police
strength, infrastructure, and organizational data.

Target metric for LO05:
    Actual (In-position) civil police + Armed Reserve personnel per 100,000
    population, by State/UT. Reported in DoPO Table 1.1 or 1.4.

Because DoPO tables are dense and column layout shifts across editions, we
send the PDF to Gemini with a strict schema. Same pattern as ASER/PLFS.

Usage:
    source secrets/.env

    # 1. Download DoPO PDF locally (~10-50 MB):
    #    https://bprd.nic.in/  →  Publications → Data on Police Organisations
    # 2. Run the extractor:
    python scripts/gpi_ingest_bprd.py \\
        --pdf data/bprd/DoPO_2023.pdf

    # Dry-run
    python scripts/gpi_ingest_bprd.py --pdf ... --dry-run

Where to get the PDF:
    https://bprd.nic.in/index1.aspx?lsid=1176&lev=2&lid=1075&langid=1
    File name pattern: DoPO_<YEAR>.pdf
"""
from __future__ import annotations
import argparse
import json
import os
import re
import sys
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

MODEL_NAME = "gemini-2.5-flash"
RAW_JSON_DIR = ROOT / "data" / "bprd" / "extractions"


PROMPT = """You are analyzing a BPRD (Bureau of Police Research & Development)
"Data on Police Organisations" PDF, published annually by the Ministry of
Home Affairs. The report contains state-wise police strength tables
(usually Table 1.1, 1.2, or 1.4 in Chapter 1: Strength of Police Forces).

Target metric for GPI LO05:
    Actual (In-position) Civil Police personnel per 100,000 population,
    by State/UT.

Preferred source in the PDF (in priority order):
    1. A pre-computed "Police per 1,00,000 population" or "Police-Population
        Ratio" column in Table 1.1 / 1.4 that combines Civil Police +
        Armed Reserve (or "Total Actual").
    2. If only "Total Sanctioned" AND "Total Actual" are given without a
        rate, extract the ACTUAL strength value AND the population number
        used, and we'll compute the rate downstream.
    3. Fall back to State Armed Police + Civil Police totals if disaggregated.

STRICT RULES:
  1. Only include values DIRECTLY STATED in the DoPO tables.
     Do NOT compute, infer, or estimate.
  2. Use the LATEST reference year the report covers (e.g., DoPO 2023 →
     data as of 01.01.2023 or similar). Do not extract historical trend
     values.
  3. Cover all 28 states + 8 UTs. If a state's row shows only sanctioned
     (no actual), skip it.
  4. Return the RATE (per 100k population) when explicit.
     If only absolute strength is given, return actual_strength AND
     population — leave rate_per_100k null.

Return JSON matching the response schema strictly.
"""

RESPONSE_SCHEMA = {
    "type": "object",
    "properties": {
        "reference_period": {
            "type": "string",
            "description": "Snapshot date as shown on report cover (e.g., 'As on 01.01.2023')."
        },
        "reference_end_year": {
            "type": "integer",
            "description": "Calendar year the DoPO edition reports on (e.g., 2023 for DoPO 2023)."
        },
        "state_data": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "state_name":       {"type": "string"},
                    "rate_per_100k":    {"type": "number",
                                          "description": "Police strength per 1,00,000 population (preferred)"},
                    "actual_strength":  {"type": "number",
                                          "description": "Total actual (in-position) police personnel"},
                    "population":       {"type": "number",
                                          "description": "Population figure used by BPRD to compute the rate (in absolute numbers)"},
                    "source_table":     {"type": "string",
                                          "description": "BPRD table number (e.g., '1.1', '1.4')"},
                },
                "required": ["state_name"],
            },
        },
        "extraction_notes": {
            "type": "string",
            "description": "Any caveats — which table was used, missing states, sample-size flags."
        },
    },
    "required": ["reference_end_year", "state_data"],
}


MAX_INLINE_MB = 30


def slice_pdf(pdf_path: Path) -> bytes:
    """Chapter 1 (Strength) is at the start of DoPO — take first 60 pages if
    the PDF is oversize."""
    size_mb = pdf_path.stat().st_size / 1_048_576
    if size_mb <= MAX_INLINE_MB:
        return pdf_path.read_bytes()

    import io
    from pypdf import PdfReader, PdfWriter
    reader = PdfReader(str(pdf_path))
    writer = PdfWriter()
    total = len(reader.pages)
    take = min(60, total)
    for i in range(take):
        writer.add_page(reader.pages[i])
    buf = io.BytesIO()
    writer.write(buf)
    print(f"    ✂ PDF {size_mb:.0f}MB > {MAX_INLINE_MB}MB, sliced to first {take}/{total} "
          f"pages (Chapter 1)", flush=True)
    return buf.getvalue()


def call_gemini(pdf_path: Path) -> dict:
    try:
        from google import genai
        from google.genai import types
    except ImportError:
        raise SystemExit("pip install google-genai")

    project = os.environ.get("GCP_PROJECT")
    if not project:
        raise SystemExit("GCP_PROJECT not set — source secrets/.env")

    client = genai.Client(vertexai=True, project=project, location="us-central1")
    pdf_bytes = slice_pdf(pdf_path)
    print(f"    → Gemini call ({len(pdf_bytes) // 1_048_576}MB PDF)", flush=True)

    resp = client.models.generate_content(
        model=MODEL_NAME,
        contents=[
            types.Part.from_bytes(data=pdf_bytes, mime_type="application/pdf"),
            PROMPT,
        ],
        config=types.GenerateContentConfig(
            temperature=0.0,
            response_mime_type="application/json",
            response_schema=RESPONSE_SCHEMA,
            max_output_tokens=16384,
        ),
    )
    return json.loads(resp.text or "{}")


# ═══════════════════════════════════════════════════════════════════════════
# State-name normalization (same alias table)
# ═══════════════════════════════════════════════════════════════════════════
STATE_ALIASES = {
    "orissa":                   "Odisha",
    "odisha":                   "Odisha",
    "jammu & kashmir":          "Jammu and Kashmir",
    "jammu and kashmir":        "Jammu and Kashmir",
    "j&k":                      "Jammu and Kashmir",
    "nct of delhi":             "Delhi",
    "delhi":                    "Delhi",
    "chhattisgarh":             "Chhattisgarh",
    "chattisgarh":              "Chhattisgarh",
    "uttarakhand":              "Uttarakhand",
    "puducherry":               "Puducherry",
    "pondicherry":              "Puducherry",
    "india":                    None,
    "all india":                None,
    "total":                    None,
    "chandigarh":               None,
    "lakshadweep":              None,
    "ladakh":                   None,
    "andaman and nicobar islands": None,
    "andaman & nicobar islands":   None,
    "a&n islands":              None,
    "dadra & nagar haveli":     None,
    "daman & diu":              None,
    "d&n haveli and daman & diu": None,
}


def normalize_state_name(raw: str) -> str | None:
    if not raw:
        return None
    n = re.sub(r"[.*#$@]", "", str(raw)).strip().lower()
    n = re.sub(r"\s+", " ", n)
    if n in STATE_ALIASES:
        return STATE_ALIASES[n]
    return " ".join(w.capitalize() for w in n.split())


def compute_rate(row: dict) -> float | None:
    """Prefer explicit rate. Fall back to actual_strength / population × 100k."""
    r = row.get("rate_per_100k")
    if r is not None:
        return r
    a, p = row.get("actual_strength"), row.get("population")
    if a is not None and p and p > 0:
        return round(a / p * 100_000, 1)
    return None


# ═══════════════════════════════════════════════════════════════════════════
# DB insert
# ═══════════════════════════════════════════════════════════════════════════
def insert_values(session, state_data: list[dict], fiscal_year: int,
                    source_pdf: str, dry_run: bool) -> dict:
    from app.gpi_models import GpiIndicator, GpiIndicatorValue
    from app.models import State

    ind = session.query(GpiIndicator).filter_by(code="LO05").one_or_none()
    if not ind:
        raise SystemExit("Indicator LO05 not seeded — run gpi_seed.py")

    states_by_name = {s.name: s for s in session.query(State).all()}
    counts = {"inserted": 0, "updated": 0, "skipped_state": 0, "no_value": 0}

    for row in state_data:
        state_raw = row.get("state_name")
        val = compute_rate(row)
        if val is None:
            counts["no_value"] += 1
            continue

        state = normalize_state_name(state_raw)
        if state is None:
            counts["skipped_state"] += 1
            continue
        st = states_by_name.get(state)
        if not st:
            counts["skipped_state"] += 1
            continue

        notes_parts = [f"table={row.get('source_table', '?')}"]
        a, p = row.get("actual_strength"), row.get("population")
        if a is not None: notes_parts.append(f"strength={a:.0f}")
        if p is not None: notes_parts.append(f"pop={p:.0f}")
        if row.get("rate_per_100k") is None:
            notes_parts.append("(rate computed)")

        existing = session.query(GpiIndicatorValue).filter_by(
            indicator_id=ind.id, state_id=st.id, fiscal_year=fiscal_year,
        ).one_or_none()

        payload = {
            "raw_value":         val,
            "source_url":        "https://bprd.nic.in/",
            "source_document":   f"BPRD Data on Police Organisations · {source_pdf}",
            "extraction_method": "llm_extracted",
            "staleness":         "current",
            "notes":             " · ".join(notes_parts),
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
    ap.add_argument("--pdf", required=True, help="Path to BPRD DoPO report PDF")
    ap.add_argument("--year", type=int, default=None,
                    help="Override fiscal year (default: extraction reference_end_year)")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--force", action="store_true",
                    help="Re-call Gemini even if cached JSON exists")
    args = ap.parse_args()

    pdf_path = Path(args.pdf)
    if not pdf_path.exists():
        raise SystemExit(f"No such file: {pdf_path}")

    RAW_JSON_DIR.mkdir(parents=True, exist_ok=True)

    print(f"BPRD PDF: {pdf_path}")
    print(f"Model:    {MODEL_NAME}")
    print()

    cache_path = RAW_JSON_DIR / (pdf_path.stem + ".gemini.json")
    if cache_path.exists() and not args.force:
        extraction = json.loads(cache_path.read_text())
        print(f"  ✓ using cached extraction: {cache_path.relative_to(ROOT)}")
    else:
        t0 = time.time()
        try:
            extraction = call_gemini(pdf_path)
        except Exception as e:
            print(f"  ✗ Gemini error: {type(e).__name__}: {e}")
            sys.exit(1)
        dt = time.time() - t0
        cache_path.write_text(json.dumps(extraction, indent=2, ensure_ascii=False))
        print(f"  ✓ extracted in {dt:.0f}s → {cache_path.relative_to(ROOT)}")

    state_data = extraction.get("state_data", [])
    ref_year = extraction.get("reference_end_year")
    fiscal_year = args.year or ref_year

    if not fiscal_year:
        raise SystemExit("Could not determine reference year — pass --year explicitly")

    print(f"  Reference: {extraction.get('reference_period')}")
    print(f"  Fiscal year: {fiscal_year}")
    print(f"  Rows returned: {len(state_data)}")
    if extraction.get("extraction_notes"):
        print(f"  Notes: {extraction['extraction_notes'][:200]}")
    print()

    from app.database import SessionLocal
    session = SessionLocal()
    counts = insert_values(session, state_data, fiscal_year,
                             pdf_path.name, args.dry_run)
    session.close()

    print(f"LO05 Police strength per 100k — FY{fiscal_year}")
    print(f"  Inserted:      {counts['inserted']}")
    print(f"  Updated:       {counts['updated']}")
    print(f"  No value:      {counts['no_value']}")
    print(f"  Skipped state: {counts['skipped_state']}")
    if args.dry_run:
        print("\n(dry-run — no writes)")
    print("\nNext: python scripts/gpi_compute_scores.py")


if __name__ == "__main__":
    main()
