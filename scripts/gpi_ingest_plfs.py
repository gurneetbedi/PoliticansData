"""PLFS (Periodic Labour Force Survey) → GPI E04 Labour Force Participation Rate.

The PLFS Annual Report is a ~700-page MoSPI PDF that contains state-wise LFPR
in Appendix A tables (usually Statement/Table with columns for rural/urban
male/female + all-persons LFPR by state × age group).

Rather than a custom pypdf parser (very fragile — tables span many pages and
column widths shift), we send the PDF to Gemini with a strict schema, same
proven direct-PDF pattern used for ASER extractions.

Target:
    E04  Labour Force Participation Rate (LFPR) — usual status (ps+ss),
         age 15 years and above, ALL PERSONS (Rural+Urban combined).

Fiscal-year assignment: PLFS surveys are July-June. Report labelled
"2023-24" → fiscal_year 2024 in our schema (ending calendar year).

Usage:
    source secrets/.env

    # 1. Download the PLFS Annual Report locally, then:
    python scripts/gpi_ingest_plfs.py \\
        --pdf data/plfs/AnnualReport_PLFS2023-24L2.pdf

    # Dry-run
    python scripts/gpi_ingest_plfs.py --pdf ... --dry-run

Where to get the PDF:
    https://mospi.gov.in/publication/annual-report-periodic-labour-force-survey-plfs-july-2023-june-2024
    File name pattern: AnnualReport_PLFS<FY>L2.pdf  (~200-300 MB)
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
RAW_JSON_DIR = ROOT / "data" / "plfs" / "extractions"


# ═══════════════════════════════════════════════════════════════════════════
# Gemini prompt + response schema
# ═══════════════════════════════════════════════════════════════════════════
PROMPT = """You are analyzing an MoSPI Periodic Labour Force Survey (PLFS)
Annual Report PDF. This report has state-wise Labour Force Participation
Rate (LFPR) tables in Appendix A (Statement 22 / A.31 / similar).

Extract state-wise LFPR values with these definitions:

Target metric:
    Labour Force Participation Rate (LFPR) in USUAL STATUS (ps+ss),
    for persons of age 15 YEARS AND ABOVE,
    combined RURAL + URBAN + ALL PERSONS (both sexes).

Preferred source in the PDF (in priority order):
    1. A single "all persons, 15+, rural+urban" LFPR column in the state
        table if present.
    2. Combined "Total (Rural+Urban)" or "All-India" cross-classification.
    3. If only rural + urban splits are given, extract BOTH values and
        state that in the notes; we can compute a weighted mean downstream.

STRICT RULES:
  1. Only include values DIRECTLY STATED in the PDF's Appendix A statements.
     Do NOT compute, infer, or estimate.
  2. Use the LATEST reference period the report covers (i.e., if the report
     is "PLFS 2023-24", use the 2023-24 column, NOT older reference years
     shown for comparison).
  3. Cover all 28 States + 8 UTs listed. Skip small UTs (Chandigarh,
     Lakshadweep) if their sample size flag is present (".." or missing).
  4. If a value is only reported for males or only females, prefer PERSONS
     (both sexes combined) when available.
  5. Return LFPR as a percentage (e.g., 60.1, not 0.601).

Return JSON matching the response schema strictly.
"""

RESPONSE_SCHEMA = {
    "type": "object",
    "properties": {
        "survey_period": {
            "type": "string",
            "description": "Reference period as shown on the report cover (e.g., 'July 2023 - June 2024')."
        },
        "survey_end_year": {
            "type": "integer",
            "description": "Ending calendar year of the survey period (e.g., 2024 for July 2023 - June 2024)."
        },
        "state_data": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "state_name":     {"type": "string"},
                    "lfpr_all":       {"type": "number",
                                        "description": "LFPR persons 15+, rural+urban combined, % (preferred)"},
                    "lfpr_rural":     {"type": "number",
                                        "description": "LFPR persons 15+, rural only, % (if 'all' not given)"},
                    "lfpr_urban":     {"type": "number",
                                        "description": "LFPR persons 15+, urban only, % (if 'all' not given)"},
                    "source_statement": {"type": "string",
                                          "description": "Statement/Appendix table number (e.g., 'Statement 22' or 'A.31')"},
                },
                "required": ["state_name"],
            },
        },
        "extraction_notes": {
            "type": "string",
            "description": "Any caveats — which Statement was used, missing states, sample-size flags."
        },
    },
    "required": ["survey_end_year", "state_data"],
}


# ═══════════════════════════════════════════════════════════════════════════
# PDF slicing (PLFS Annual Report is 700+ pages, appendix ~pp.150-500)
# ═══════════════════════════════════════════════════════════════════════════
MAX_INLINE_MB = 30


def slice_pdf(pdf_path: Path) -> bytes:
    """Return the whole PDF unless oversized, in which case slice to the
    Appendix A range where state-wise LFPR tables live.

    Empirically, in the PLFS 2023-24 L2 report, Appendix A tables run from
    ~p.150 (Statements) to ~p.500. We take pages 100-500 to be safe."""
    size_mb = pdf_path.stat().st_size / 1_048_576
    if size_mb <= MAX_INLINE_MB:
        return pdf_path.read_bytes()

    import io
    from pypdf import PdfReader, PdfWriter
    reader = PdfReader(str(pdf_path))
    writer = PdfWriter()
    total = len(reader.pages)
    # Appendix A range for PLFS 2023-24 L2 report; adjust if MoSPI changes layout
    start = min(100, total // 4)
    end = min(500, total)
    for i in range(start, end):
        writer.add_page(reader.pages[i])
    buf = io.BytesIO()
    writer.write(buf)
    print(f"    ✂ PDF {size_mb:.0f}MB > {MAX_INLINE_MB}MB, sliced pp.{start+1}-{end} "
          f"of {total}", flush=True)
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
# State-name normalization
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
    "all-india":                None,
    "total":                    None,
    # UTs we don't score
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


def combined_lfpr(row: dict) -> float | None:
    """Prefer lfpr_all; else average rural+urban (unweighted — best we can do
    without population weights). Returns None if no LFPR field."""
    v = row.get("lfpr_all")
    if v is not None:
        return v
    r, u = row.get("lfpr_rural"), row.get("lfpr_urban")
    if r is not None and u is not None:
        # Unweighted average — the report also gives population weights in
        # separate tables, but for a coarse LFPR estimate this is adequate.
        return (r + u) / 2
    return r if r is not None else u


# ═══════════════════════════════════════════════════════════════════════════
# DB insert
# ═══════════════════════════════════════════════════════════════════════════
def insert_values(session, state_data: list[dict], fiscal_year: int,
                    source_pdf: str, dry_run: bool) -> dict:
    from app.gpi_models import GpiIndicator, GpiIndicatorValue
    from app.models import State

    ind = session.query(GpiIndicator).filter_by(code="E04").one_or_none()
    if not ind:
        raise SystemExit("Indicator E04 not seeded — run gpi_seed.py")

    states_by_name = {s.name: s for s in session.query(State).all()}
    counts = {"inserted": 0, "updated": 0, "skipped_state": 0, "no_value": 0}

    for row in state_data:
        state_raw = row.get("state_name")
        val = combined_lfpr(row)
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

        r, u = row.get("lfpr_rural"), row.get("lfpr_urban")
        src_stmt = row.get("source_statement", "unknown")
        notes_parts = [f"src={src_stmt}"]
        if r is not None: notes_parts.append(f"rural={r}")
        if u is not None: notes_parts.append(f"urban={u}")
        if row.get("lfpr_all") is None:
            notes_parts.append("(rural+urban unweighted avg)")

        existing = session.query(GpiIndicatorValue).filter_by(
            indicator_id=ind.id, state_id=st.id, fiscal_year=fiscal_year,
        ).one_or_none()

        payload = {
            "raw_value":         val,
            "source_url":        "https://mospi.gov.in/publication/annual-report-periodic-labour-force-survey-plfs-july-2023-june-2024",
            "source_document":   f"MoSPI PLFS Annual Report · {source_pdf}",
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
    ap.add_argument("--pdf", required=True, help="Path to PLFS Annual Report PDF")
    ap.add_argument("--year", type=int, default=None,
                    help="Override fiscal year (default: from extraction survey_end_year)")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--force", action="store_true",
                    help="Re-call Gemini even if cached JSON exists")
    args = ap.parse_args()

    pdf_path = Path(args.pdf)
    if not pdf_path.exists():
        raise SystemExit(f"No such file: {pdf_path}")

    RAW_JSON_DIR.mkdir(parents=True, exist_ok=True)

    print(f"PLFS PDF: {pdf_path}")
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
    survey_end = extraction.get("survey_end_year")
    fiscal_year = args.year or survey_end

    if not fiscal_year:
        raise SystemExit("Could not determine survey year — pass --year explicitly")

    print(f"  Survey period: {extraction.get('survey_period')}")
    print(f"  Fiscal year:   {fiscal_year}")
    print(f"  Rows returned: {len(state_data)}")
    if extraction.get("extraction_notes"):
        print(f"  Notes: {extraction['extraction_notes'][:200]}")
    print()

    # ── Persist ────────────────────────────────────────────────────────
    from app.database import SessionLocal
    session = SessionLocal()
    counts = insert_values(session, state_data, fiscal_year,
                             pdf_path.name, args.dry_run)
    session.close()

    print(f"E04 LFPR — FY{fiscal_year}")
    print(f"  Inserted:      {counts['inserted']}")
    print(f"  Updated:       {counts['updated']}")
    print(f"  No value:      {counts['no_value']}")
    print(f"  Skipped state: {counts['skipped_state']}")
    if args.dry_run:
        print("\n(dry-run — no writes)")
    print("\nNext: python scripts/gpi_compute_scores.py")


if __name__ == "__main__":
    main()
