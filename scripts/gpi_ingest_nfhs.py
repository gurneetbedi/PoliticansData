"""NFHS-5 Compendium of Fact Sheets → GPI H02 + H03 (healthcare pillar).

Data source:
    IIPS / MoHFW / DHS Program — "National Family Health Survey (NFHS-5),
    2019-21: India and State/UTs Compendium of Fact Sheets", Phase-I + Phase-II.
    Two PDFs together cover all 30 GPI-scored states.

Target indicators:
    H02  Institutional births (%)         — NFHS Factsheet line 50, TOTAL column
                                             (Direction: higher_better)
    H03  Fully vaccinated 12-23 months    — NFHS Factsheet line 57, TOTAL column
                                             (Direction: higher_better)

NOT extracted (needs different source):
    H05  Doctors per 10k                  — NFHS doesn't publish workforce
                                             stocks. Use National Health Profile
                                             (CBHI/MoHFW) with a separate script.
    H04  OOP expenditure                  — NFHS doesn't publish health-finance.
                                             Use National Health Accounts.

Fiscal-year: NFHS-5 fieldwork 2019-21 → tag as fiscal_year=2021.
Matches how H01 and H06 (already ingested) are stored.

Usage:
    source secrets/.env

    # 1. Download both compendium PDFs:
    mkdir -p data/nfhs
    curl -L -o data/nfhs/NFHS-5_Compendium_Phase-I.pdf \\
      "https://dhsprogram.com/pubs/pdf/OF43/NFHS-5_India_and_State_Factsheet_Compendium_Phase-I.pdf"
    curl -L -o data/nfhs/NFHS-5_Compendium_Phase-II.pdf \\
      "https://dhsprogram.com/pubs/pdf/OF43/NFHS-5_India_and_State_Factsheet_Compendium_Phase-II.pdf"

    # 2. Ingest both compendiums in one run:
    python scripts/gpi_ingest_nfhs.py \\
      --pdf data/nfhs/NFHS-5_Compendium_Phase-I.pdf \\
      --pdf data/nfhs/NFHS-5_Compendium_Phase-II.pdf

    # Dry-run:
    python scripts/gpi_ingest_nfhs.py --pdf data/nfhs/*.pdf --dry-run
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
RAW_JSON_DIR = ROOT / "data" / "nfhs" / "extractions"

# NFHS-5 reference period midpoint. Both H01 (IMR) and H06 (anemia) stored
# at FY2020-21 already; we align H02/H03 to same period.
NFHS5_FY = 2021


# ═══════════════════════════════════════════════════════════════════════════
# Gemini prompt + response schema
# ═══════════════════════════════════════════════════════════════════════════
PROMPT = """You are analyzing an NFHS-5 (2019-21) Compendium of Fact Sheets PDF
published by IIPS / MoHFW. The document contains one state/UT fact sheet per
section — each ~6 pages — starting after the table of contents.

Each state's factsheet has ~130 numbered rows. Each row has 4 columns:
    Column 1 = URBAN (NFHS-5), Column 2 = RURAL (NFHS-5),
    Column 3 = TOTAL (NFHS-5), Column 4 = TOTAL (NFHS-4, older comparison).

Extract per state:
  1. institutional_births_pct   — Row 50: "Institutional births (%)".
                                    Take the TOTAL column (col 3 = NFHS-5 Total).
  2. fully_vaccinated_pct       — Row 57: "Children age 12-23 months fully
                                    vaccinated based on information from either
                                    vaccination card or mother's recall (%)".
                                    Take the TOTAL column (col 3).

STRICT RULES:
  1. Only include values DIRECTLY STATED in the factsheet. Never estimate.
  2. Column 3 (NFHS-5 Total, both urban+rural) is REQUIRED — do not accept
     rural-only or urban-only values as substitutes.
  3. If a value is shown as "na" or "( )" (parentheses = unreliable sample),
     OMIT the state entirely for that indicator (leave field null).
  4. Cover ALL state/UT sections in the PDF. States are listed in the table
     of contents. INDIA fact sheet may appear first — extract it too under
     state_name = "India" if present.
  5. Do NOT include NFHS-4 (col 4) values — those are old comparison numbers.

Return JSON matching the response schema strictly.
"""

RESPONSE_SCHEMA = {
    "type": "object",
    "properties": {
        "phase": {
            "type": "string",
            "description": "Compendium phase — 'I' or 'II' (from cover page).",
        },
        "state_data": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "state_name":                {"type": "string"},
                    "institutional_births_pct":  {"type": "number",
                                                   "description": "Row 50, Total column (NFHS-5)"},
                    "fully_vaccinated_pct":      {"type": "number",
                                                   "description": "Row 57, Total column (NFHS-5)"},
                },
                "required": ["state_name"],
            },
        },
        "extraction_notes": {
            "type": "string",
            "description": "Any caveats about the extraction — states omitted, columns mislabeled, etc.",
        },
    },
    "required": ["state_data"],
}


# ═══════════════════════════════════════════════════════════════════════════
# PDF slicing — compendiums are ~5-10 MB, small enough to inline as-is
# ═══════════════════════════════════════════════════════════════════════════
MAX_INLINE_MB = 30


def load_pdf_bytes(pdf_path: Path) -> bytes:
    size_mb = pdf_path.stat().st_size / 1_048_576
    if size_mb <= MAX_INLINE_MB:
        return pdf_path.read_bytes()

    # Fallback: take first 200 pages (each state factsheet is 6 pages, 22 states = 132)
    import io
    from pypdf import PdfReader, PdfWriter
    reader = PdfReader(str(pdf_path))
    writer = PdfWriter()
    total = len(reader.pages)
    take = min(200, total)
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
    pdf_bytes = load_pdf_bytes(pdf_path)
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
    "nct delhi":                "Delhi",
    "delhi":                    "Delhi",
    "chhattisgarh":             "Chhattisgarh",
    "chattisgarh":              "Chhattisgarh",
    "uttarakhand":              "Uttarakhand",
    "puducherry":               "Puducherry",
    "pondicherry":              "Puducherry",
    # UTs / all-India we don't score
    "india":                    None,
    "all india":                None,
    "andaman & nicobar island": None,
    "andaman & nicobar islands":None,
    "andaman and nicobar islands": None,
    "a&n islands":              None,
    "chandigarh":               None,
    "dadra nagar haveli & daman & diu": None,
    "dadra & nagar haveli":     None,
    "daman & diu":              None,
    "ladakh":                   None,
    "lakshadweep":              None,
}


def normalize_state_name(raw: str) -> str | None:
    if not raw:
        return None
    n = re.sub(r"\(UT\)", "", str(raw), flags=re.I)
    n = re.sub(r"[.*#$@,]", "", n).strip().lower()
    n = re.sub(r"\s+", " ", n)
    if n in STATE_ALIASES:
        return STATE_ALIASES[n]
    return " ".join(w.capitalize() for w in n.split())


# ═══════════════════════════════════════════════════════════════════════════
# DB insert
# ═══════════════════════════════════════════════════════════════════════════
INDICATOR_FIELDS = [
    ("H02", "institutional_births_pct",  "Institutional births (%) — NFHS-5 Total"),
    ("H03", "fully_vaccinated_pct",      "Fully vaccinated 12-23 months (%) — NFHS-5 Total"),
]


def insert_values(session, state_data: list[dict], source_pdf: str,
                    dry_run: bool) -> dict:
    from app.gpi_models import GpiIndicator, GpiIndicatorValue
    from app.models import State

    indicators = {}
    for code, _, _ in INDICATOR_FIELDS:
        ind = session.query(GpiIndicator).filter_by(code=code).one_or_none()
        if not ind:
            raise SystemExit(f"Indicator {code} not seeded — run gpi_seed.py")
        indicators[code] = ind

    states_by_name = {s.name: s for s in session.query(State).all()}
    per_ind_counts = {code: {"inserted": 0, "updated": 0} for code, _, _ in INDICATOR_FIELDS}
    skipped_state = 0
    unresolved = []

    for row in state_data:
        state_raw = row.get("state_name")
        state = normalize_state_name(state_raw)
        if state is None:
            skipped_state += 1
            continue
        st = states_by_name.get(state)
        if not st:
            unresolved.append((state_raw, state))
            continue

        for code, field, notes_suffix in INDICATOR_FIELDS:
            val = row.get(field)
            if val is None:
                continue

            existing = session.query(GpiIndicatorValue).filter_by(
                indicator_id=indicators[code].id, state_id=st.id,
                fiscal_year=NFHS5_FY,
            ).one_or_none()

            payload = {
                "raw_value":         val,
                "source_url":        "https://dhsprogram.com/pubs/pdf/OF43/",
                "source_document":   f"NFHS-5 Compendium (2019-21) · {source_pdf} · {notes_suffix}",
                "extraction_method": "llm_extracted",
                "staleness":         "current",
                "notes":             f"NFHS-5 fieldwork 2019-21. {notes_suffix}",
                "extracted_at":      datetime.utcnow(),
            }

            if existing:
                for k, v in payload.items():
                    setattr(existing, k, v)
                existing.normalized_value = None
                existing.national_rank = None
                per_ind_counts[code]["updated"] += 1
            else:
                session.add(GpiIndicatorValue(
                    indicator_id=indicators[code].id, state_id=st.id,
                    fiscal_year=NFHS5_FY, **payload,
                ))
                per_ind_counts[code]["inserted"] += 1

    if not dry_run:
        session.commit()

    return {
        "per_indicator": per_ind_counts,
        "skipped_state": skipped_state,
        "unresolved":    unresolved,
    }


# ═══════════════════════════════════════════════════════════════════════════
# Main
# ═══════════════════════════════════════════════════════════════════════════
def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                  formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--pdf", action="append", required=True,
                    help="Path to NFHS-5 compendium PDF (repeatable — pass both phases)")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--force", action="store_true",
                    help="Re-call Gemini even if cached JSON exists")
    args = ap.parse_args()

    RAW_JSON_DIR.mkdir(parents=True, exist_ok=True)

    all_state_data: list[dict] = []
    from app.database import SessionLocal
    session = SessionLocal()

    for pdf_arg in args.pdf:
        pdf_path = Path(pdf_arg)
        if not pdf_path.exists():
            raise SystemExit(f"No such file: {pdf_path}")

        print(f"\n═══ {pdf_path.name} ═══")
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

        rows = extraction.get("state_data", [])
        print(f"  Rows returned: {len(rows)}  (phase {extraction.get('phase', '?')})")
        if extraction.get("extraction_notes"):
            print(f"  Notes: {extraction['extraction_notes'][:200]}")

        counts = insert_values(session, rows, pdf_path.name, args.dry_run)

        for code, c in counts["per_indicator"].items():
            print(f"    {code}: inserted={c['inserted']}  updated={c['updated']}")
        if counts["skipped_state"]:
            print(f"    skipped (UTs/all-India): {counts['skipped_state']}")
        if counts["unresolved"]:
            print(f"    unresolved names:")
            for raw, canon in counts["unresolved"]:
                print(f"      '{raw}' → '{canon}'")

    session.close()

    if args.dry_run:
        print("\n(dry-run — no writes)")
    print("\nNext: python scripts/gpi_compute_scores.py")


if __name__ == "__main__":
    main()
