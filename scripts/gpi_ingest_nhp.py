"""National Health Profile 2023 → GPI H05 Doctors per 10k population.

Data source:
    Central Bureau of Health Intelligence (CBHI) / MoHFW —
    "National Health Profile 2023". Chapter 6 (Health Human Resource) has
    state-wise "Number of Registered Allopathic Doctors" and Chapter 1
    has state-wise population figures.

Target indicator:
    H05  Doctors per 10,000 population
         (Direction: higher_better — more doctors = better healthcare access)

Fiscal-year: NHP 2023 = data as of 2022 → fiscal_year 2022.

Usage:
    source secrets/.env

    # 1. Download the NHP PDF (~40MB):
    mkdir -p data/nhp
    curl -L -o data/nhp/NHP_2023.pdf \\
      "https://cbhidghs.mohfw.gov.in/sites/default/files/NHP/NHP-2023-Last-Final.pdf"

    # 2. Ingest:
    python scripts/gpi_ingest_nhp.py --pdf data/nhp/NHP_2023.pdf

    # Dry-run:
    python scripts/gpi_ingest_nhp.py --pdf data/nhp/NHP_2023.pdf --dry-run

Note: Registered doctors data historically has quality issues (some states
report cumulative registrations without deducting deaths/retirements). We
extract whatever CBHI publishes and let the pillar normalization handle
outliers via winsorization.
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
RAW_JSON_DIR = ROOT / "data" / "nhp" / "extractions"

NHP_FY = 2022


PROMPT = """You are analyzing a CBHI (Central Bureau of Health Intelligence) /
MoHFW "National Health Profile 2023" PDF. This report has state-wise
health workforce and population data.

Target metric for H05 (Doctors per 10,000 population):

    Number of Registered Allopathic Doctors per State/UT (cumulative
    registrations with the State Medical Councils, as reported to CBHI),
    AND state-wise estimated population for the same reference year, so
    we can compute the per-10k rate.

Preferred source in the PDF:
    - Chapter 6 (Health Human Resource) — Table 6.1, 6.2, or 6.3 with
      "Number of Registered Allopathic Doctors" by State/UT.
    - Include the reference year (e.g., "up to 2022" or "as on 31.12.2022").
    - Chapter 1 (Demographic Indicators) — Table 1.1 or 1.2 with state-wise
      projected population for the same reference year.

STRICT RULES:
  1. Only include values DIRECTLY STATED in the tables.
     Do NOT compute, infer, or estimate.
  2. If the table shows CUMULATIVE registrations across years, prefer the
     LATEST year (usually 2022 or the report year - 1).
  3. Return BOTH the registered doctors count AND the population figure
     used. We'll compute the per-10k rate downstream.
  4. If the table provides a pre-computed "Doctors per 10k population"
     rate, use it directly (populate the rate_per_10k field).
  5. Skip all-India totals and aggregate rows.

Return JSON matching the response schema strictly.
"""

RESPONSE_SCHEMA = {
    "type": "object",
    "properties": {
        "reference_year": {
            "type": "integer",
            "description": "Reference year for doctors data (e.g., 2022)"
        },
        "state_data": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "state_name":         {"type": "string"},
                    "registered_doctors": {"type": "number",
                                            "description": "Number of registered allopathic doctors"},
                    "population":         {"type": "number",
                                            "description": "Estimated population for the reference year (absolute number)"},
                    "rate_per_10k":       {"type": "number",
                                            "description": "Pre-computed doctors per 10,000 population (if provided by NHP)"},
                    "source_table":       {"type": "string",
                                            "description": "NHP table number (e.g., '6.1', '6.2')"},
                },
                "required": ["state_name"],
            },
        },
        "extraction_notes": {
            "type": "string",
        },
    },
    "required": ["state_data"],
}


MAX_INLINE_MB = 30


def load_pdf_bytes(pdf_path: Path) -> bytes:
    """Load PDF, slicing aggressively if oversize.

    NHP has Chapter 1 (Demography, population by state) at the front and
    Chapter 6 (Health Human Resource — doctors) around pages 150-200. We
    take a big slice from the FRONT (pages 1-250) and if that's still too
    large, we halve down to 200, 150, 120, 90, 60. Both target chapters
    should still be covered at 150 pages.
    """
    size_mb = pdf_path.stat().st_size / 1_048_576
    if size_mb <= MAX_INLINE_MB:
        return pdf_path.read_bytes()

    import io
    from pypdf import PdfReader, PdfWriter
    reader = PdfReader(str(pdf_path))
    total = len(reader.pages)

    for take in [250, 200, 150, 120, 90, 60]:
        take = min(take, total)
        writer = PdfWriter()
        for i in range(take):
            writer.add_page(reader.pages[i])
        buf = io.BytesIO()
        writer.write(buf)
        data = buf.getvalue()
        data_mb = len(data) / 1_048_576
        if data_mb <= MAX_INLINE_MB:
            print(f"    ✂ PDF {size_mb:.0f}MB > {MAX_INLINE_MB}MB — sliced to FIRST "
                  f"{take}/{total} pages = {data_mb:.1f}MB (Ch 1 + Ch 6)",
                  flush=True)
            return data

    raise SystemExit(f"Even 60-page slice exceeds {MAX_INLINE_MB}MB — "
                      "compress with `brew install ghostscript && gs ...` and retry.")


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
    "andaman & nicobar islands":None,
    "andaman and nicobar islands": None,
    "a&n islands":              None,
    "chandigarh":               None,
    "ladakh":                   None,
    "lakshadweep":              None,
    "dadra & nagar haveli":     None,
    "daman & diu":              None,
}


def normalize_state_name(raw: str) -> str | None:
    if not raw:
        return None
    n = re.sub(r"\(UT\)|\*|\#|\@", "", str(raw), flags=re.I)
    n = re.sub(r"[.,]", "", n).strip().lower()
    n = re.sub(r"\s+", " ", n)
    if n in STATE_ALIASES:
        return STATE_ALIASES[n]
    return " ".join(w.capitalize() for w in n.split())


def compute_rate(row: dict) -> float | None:
    """Prefer explicit rate. Fall back to registered_doctors / population × 10k."""
    r = row.get("rate_per_10k")
    if r is not None and r > 0:
        return r
    d, p = row.get("registered_doctors"), row.get("population")
    if d is not None and p and p > 0:
        return round(d / p * 10_000, 2)
    return None


def insert_values(session, state_data: list[dict], source_pdf: str,
                    dry_run: bool) -> dict:
    from app.gpi_models import GpiIndicator, GpiIndicatorValue
    from app.models import State

    ind = session.query(GpiIndicator).filter_by(code="H05").one_or_none()
    if not ind:
        raise SystemExit("Indicator H05 not seeded — run gpi_seed.py")

    states_by_name = {s.name: s for s in session.query(State).all()}
    counts = {"inserted": 0, "updated": 0, "skipped": 0, "no_value": 0,
                "unresolved": []}

    for row in state_data:
        val = compute_rate(row)
        if val is None:
            counts["no_value"] += 1
            continue

        state_raw = row.get("state_name")
        canon = normalize_state_name(state_raw)
        if canon is None:
            counts["skipped"] += 1
            continue
        st = states_by_name.get(canon)
        if not st:
            counts["unresolved"].append((state_raw, canon))
            continue

        d, p = row.get("registered_doctors"), row.get("population")
        notes_parts = [f"table={row.get('source_table', '?')}"]
        if d is not None: notes_parts.append(f"doctors={d:.0f}")
        if p is not None: notes_parts.append(f"pop={p:.0f}")
        if row.get("rate_per_10k") is None:
            notes_parts.append("(rate computed)")

        existing = session.query(GpiIndicatorValue).filter_by(
            indicator_id=ind.id, state_id=st.id, fiscal_year=NHP_FY,
        ).one_or_none()

        payload = {
            "raw_value":         val,
            "source_url":        "https://cbhidghs.mohfw.gov.in/publications/national-health-profile",
            "source_document":   f"CBHI / MoHFW · National Health Profile 2023 · {source_pdf}",
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
                indicator_id=ind.id, state_id=st.id, fiscal_year=NHP_FY,
                **payload,
            ))
            counts["inserted"] += 1

    if not dry_run:
        session.commit()
    return counts


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                  formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--pdf", required=True, help="Path to NHP 2023 PDF")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--force", action="store_true")
    args = ap.parse_args()

    pdf_path = Path(args.pdf)
    if not pdf_path.exists():
        raise SystemExit(f"No such file: {pdf_path}")

    RAW_JSON_DIR.mkdir(parents=True, exist_ok=True)
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
    print(f"  Rows returned: {len(state_data)}")
    if extraction.get("extraction_notes"):
        print(f"  Notes: {extraction['extraction_notes'][:200]}")

    from app.database import SessionLocal
    session = SessionLocal()
    counts = insert_values(session, state_data, pdf_path.name, args.dry_run)
    session.close()

    print(f"\nH05 Doctors per 10k — FY{NHP_FY}")
    print(f"  Inserted:  {counts['inserted']}")
    print(f"  Updated:   {counts['updated']}")
    print(f"  Skipped:   {counts['skipped']}")
    print(f"  No value:  {counts['no_value']}")
    if counts["unresolved"]:
        print(f"  Unresolved: {counts['unresolved']}")
    if args.dry_run:
        print("\n(dry-run — no writes)")
    print("\nNext: python scripts/gpi_compute_scores.py")


if __name__ == "__main__":
    main()
