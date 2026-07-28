"""ASER (Annual Status of Education Report) → GPI ED01 (learning outcomes).

ASER PDFs are published by Pratham/ASER Centre — annual (mostly), rural-focused
household + child learning survey. Reports contain state-level tables of
learning-outcome indicators, but the layout varies year-to-year, mixes
narrative with tables, and has data scattered across 300+ pages.

Rather than a custom pypdf parser (fragile against layout changes), we send
the full PDF to Gemini with a strict response schema. Same direct-PDF pattern
proven on ECI affidavits and CAG SFARs. Cost ~$0.05 per report; total ~$0.15
for the 3 reports (ASER 2018 + 2022 + 2024).

Target indicator:
    ED01  % Grade 5 (Std V) rural children who can read a Std II text
          (from the Reading section of ASER's state-level tables)

Bonus fields we also capture (stored in gpi_indicator_values.notes):
    - Grade 5 rural children who can do division (arithmetic)
    - Grade 3 rural children who can read a Std II text

Usage:
    source secrets/.env

    # Auto-detects survey year from PDF; writes to ED01
    python scripts/gpi_ingest_aser.py --pdf data/aser/ASER_2024_Final-Report_13_2_24-1.pdf

    # Preview extraction without DB writes
    python scripts/gpi_ingest_aser.py \\
        --pdf data/aser/ASER-report_2022-1.pdf --dry-run

    # Override survey year (e.g. auto-detect misidentifies)
    python scripts/gpi_ingest_aser.py --pdf ... --year 2024
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
RAW_JSON_DIR = ROOT / "data" / "aser" / "extractions"


# ═══════════════════════════════════════════════════════════════════════════
# Gemini prompt + response schema
# ═══════════════════════════════════════════════════════════════════════════
PROMPT = """You are analyzing an ASER (Annual Status of Education Report) PDF
published by ASER Centre / Pratham. The report contains state-level tables
showing learning outcomes for rural children in Std III, V, and VIII.

Extract the following per-state values for the SURVEY YEAR this report covers
(usually stated on the cover, e.g. "ASER 2024"). Use the ALL-INDIA table with
government + private schools combined (Govt+Pvt) when available; fall back to
"All Schools" if that's the presentation format.

Target indicators (all as percentages):
  1. grade_5_reading_std2_pct  — % of Std V rural children who can read a
     Std II level text (the marquee ASER reading proficiency indicator)
  2. grade_5_division_pct       — % of Std V rural children who can do a
     division problem (marquee arithmetic indicator)
  3. grade_3_reading_std2_pct   — % of Std III rural children who can read a
     Std II level text (foundational literacy)

STRICT RULES:
  1. Only include values DIRECTLY STATED in tables or figures in the PDF.
     Do NOT compute, infer, or estimate.
  2. Use the ALL-INDIA (both govt + private schools combined) numbers when
     present. This is often labeled "Govt+Pvt" or "All schools".
  3. Use the LATEST survey year the report covers (ASER 2024 → survey 2024).
     Do not report historical trend values, only the current survey.
  4. Cover all 28 states + UTs listed in the report. Some smaller UTs may be
     omitted from ASER — skip if not present.
  5. If a value can't be found for a state, OMIT the state entirely (don't
     include with null values).

Return JSON matching the response schema strictly.
"""

RESPONSE_SCHEMA = {
    "type": "object",
    "properties": {
        "survey_year": {
            "type": "integer",
            "description": "Calendar year of the ASER survey (e.g., 2024)."
        },
        "state_data": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "state_name":                {"type": "string"},
                    "grade_5_reading_std2_pct":  {"type": "number"},
                    "grade_5_division_pct":      {"type": "number"},
                    "grade_3_reading_std2_pct":  {"type": "number"},
                    "school_type":               {"type": "string",
                                                   "description": "govt|govt_pvt|all_schools"},
                },
                "required": ["state_name", "grade_5_reading_std2_pct"],
            },
        },
        "extraction_notes": {
            "type": "string",
            "description": "Any caveats about the extraction — which section values came from, unusual formatting.",
        },
    },
    "required": ["survey_year", "state_data"],
}


# ═══════════════════════════════════════════════════════════════════════════
# PDF slicing (large ASER PDFs)
# ═══════════════════════════════════════════════════════════════════════════
MAX_INLINE_MB = 30


def slice_pdf_all_pages(pdf_path: Path) -> bytes:
    """Return the whole PDF unless it's over the size limit, in which case
    slice to first 250 pages (ASER state-level tables live in the middle
    and end of the report, so we take more pages than for CAG SFARs)."""
    size_mb = pdf_path.stat().st_size / 1_048_576
    if size_mb <= MAX_INLINE_MB:
        return pdf_path.read_bytes()

    # For oversized PDFs, take a chunk that spans the state-tables section
    import io
    from pypdf import PdfReader, PdfWriter
    reader = PdfReader(str(pdf_path))
    writer = PdfWriter()
    total = len(reader.pages)
    take = min(300, total)
    for i in range(take):
        writer.add_page(reader.pages[i])
    buf = io.BytesIO()
    writer.write(buf)
    print(f"    ✂ PDF {size_mb:.0f}MB > {MAX_INLINE_MB}MB, sliced to first {take}/{total} pages",
          flush=True)
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

    pdf_bytes = slice_pdf_all_pages(pdf_path)
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
# Year detection from filename (fallback if Gemini's survey_year is wrong)
# ═══════════════════════════════════════════════════════════════════════════
def year_from_filename(pdf_path: Path) -> int | None:
    m = re.search(r"(20\d{2})", pdf_path.name)
    return int(m.group(1)) if m else None


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
    "andaman and nicobar islands": "Andaman and Nicobar Islands",
    "a&n islands":              "Andaman and Nicobar Islands",
    "india":                    None,
    "all india":                None,
    "total":                    None,
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
# DB insert
# ═══════════════════════════════════════════════════════════════════════════
def insert_values(session, state_data: list[dict], fiscal_year: int,
                    source_pdf: str, dry_run: bool) -> dict:
    from app.gpi_models import GpiIndicator, GpiIndicatorValue
    from app.models import State

    ind = session.query(GpiIndicator).filter_by(code="ED01").one_or_none()
    if not ind:
        raise SystemExit("Indicator ED01 not seeded — run gpi_seed.py")

    states_by_name = {s.name: s for s in session.query(State).all()}
    counts = {"inserted": 0, "updated": 0, "skipped_state": 0}

    for row in state_data:
        state_raw = row.get("state_name")
        val = row.get("grade_5_reading_std2_pct")
        if val is None:
            continue

        state = normalize_state_name(state_raw)
        if state is None:
            counts["skipped_state"] += 1
            continue
        st = states_by_name.get(state)
        if not st:
            counts["skipped_state"] += 1
            continue

        # Compose a rich notes field with the bonus indicators
        div = row.get("grade_5_division_pct")
        rd3 = row.get("grade_3_reading_std2_pct")
        st_type = row.get("school_type") or "unknown"
        notes_parts = [f"school_type={st_type}"]
        if div is not None: notes_parts.append(f"grade_5_division={div}")
        if rd3 is not None: notes_parts.append(f"grade_3_reading={rd3}")

        existing = session.query(GpiIndicatorValue).filter_by(
            indicator_id=ind.id, state_id=st.id, fiscal_year=fiscal_year,
        ).one_or_none()

        payload = {
            "raw_value":         val,
            "source_url":        "https://asercentre.org/",
            "source_document":   f"ASER {fiscal_year} · {source_pdf}",
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
    ap.add_argument("--pdf", required=True, help="Path to ASER report PDF")
    ap.add_argument("--year", type=int, default=None,
                    help="Override survey year (default: auto-detect from PDF)")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--force", action="store_true",
                    help="Re-call Gemini even if cached JSON exists")
    args = ap.parse_args()

    pdf_path = Path(args.pdf)
    if not pdf_path.exists():
        raise SystemExit(f"No such file: {pdf_path}")

    RAW_JSON_DIR.mkdir(parents=True, exist_ok=True)

    print(f"ASER PDF: {pdf_path}")
    print(f"Model:    {MODEL_NAME}")
    print()

    # ── Extract (cached) ────────────────────────────────────────────────
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

    # ── Determine fiscal year ───────────────────────────────────────────
    detected = extraction.get("survey_year")
    filename_year = year_from_filename(pdf_path)
    fiscal_year = args.year or detected or filename_year
    if not fiscal_year:
        raise SystemExit("Couldn't determine survey year; pass --year explicitly")

    if detected and args.year and args.year != detected:
        print(f"  ⚠ Override: user set --year={args.year}, PDF says {detected}. "
              f"Using {args.year}.")

    state_data = extraction.get("state_data", [])
    print(f"  Survey year: {fiscal_year}")
    print(f"  State rows:  {len(state_data)}")

    # Show Punjab sanity
    for row in state_data:
        if normalize_state_name(row.get("state_name") or "") == "Punjab":
            print(f"  Punjab: reading_std2={row.get('grade_5_reading_std2_pct')} "
                  f"division={row.get('grade_5_division_pct')} "
                  f"grade3_read={row.get('grade_3_reading_std2_pct')}")
            break

    # Preview
    print("\n  First 5 states:")
    for row in state_data[:5]:
        print(f"    {row.get('state_name', '?'):<22s} "
              f"read5={row.get('grade_5_reading_std2_pct')} "
              f"div5={row.get('grade_5_division_pct')}")

    # ── Insert ──────────────────────────────────────────────────────────
    from app.database import SessionLocal
    from app import models
    session = SessionLocal()

    try:
        counts = insert_values(session, state_data, fiscal_year, pdf_path.name,
                                 args.dry_run)
    finally:
        session.close()

    print()
    print("═══════════════ Summary ═══════════════")
    print(f"  Inserted:   {counts['inserted']}")
    print(f"  Updated:    {counts['updated']}")
    print(f"  Skipped:    {counts['skipped_state']}")
    if args.dry_run:
        print("  (dry run — no writes)")
    print()
    print("Next: python scripts/gpi_compute_scores.py")


if __name__ == "__main__":
    main()
