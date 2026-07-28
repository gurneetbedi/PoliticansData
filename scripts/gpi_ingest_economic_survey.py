"""Extract GPI indicator values from a State Economic Survey PDF via Gemini.

State Economic Surveys are annual publications by each state's Bureau of
Statistics / Finance Department. They consolidate GSDP, fiscal, health,
education, and infrastructure data in one document — typically with a
"Statistical Appendix" section holding multi-year time-series tables.

This ingester sends the PDF directly to Gemini (multimodal, no OCR) with
a structured extraction prompt + response schema. Gemini reads the tables,
finds the values for our 8 target indicators, and returns strict JSON that
we insert idempotently into gpi_indicator_values.

Reuses the direct-PDF pattern proven on ECI affidavits.

Usage:
    # Extract from a downloaded PDF
    python scripts/gpi_ingest_economic_survey.py \\
        --pdf data/gpi/economic_surveys/punjab_2024-25.pdf \\
        --state PB

    # Dry-run — see what Gemini extracted, don't touch DB
    python scripts/gpi_ingest_economic_survey.py \\
        --pdf data/gpi/economic_surveys/punjab_2024-25.pdf \\
        --state PB --dry-run

Where to find PDFs:
    Punjab       https://finance.punjab.gov.in/  (Budget section → Economic Survey)
    Delhi        https://finance.delhi.gov.in/finance/economic-survey
    Rajasthan    https://finance.rajasthan.gov.in/
    (Other states publish similar; Google "<state> economic survey <year> pdf")
"""
from __future__ import annotations
import argparse
import json
import os
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

MODEL_NAME = "gemini-2.5-flash"


# Which indicators to attempt to extract, and the human-readable prompt hint
# Gemini uses to find them in the PDF. We ONLY prompt for indicators an
# Economic Survey typically contains — Table Structure, Fiscal Chapter, etc.
INDICATOR_PROMPTS = {
    "E01": ("GSDP growth rate (real, at constant prices)",
             "% annual, year-on-year growth of Gross State Domestic Product at constant prices"),
    "E02": ("Per capita Net State Domestic Product (current prices)",
             "in INR, per person, current prices — usually listed as 'Per Capita Income' or 'PCI'"),
    "F01": ("Gross Fiscal Deficit as % of GSDP",
             "% of GSDP — the state's fiscal deficit"),
    "F02": ("Total Outstanding Liabilities (Debt) as % of GSDP",
             "% of GSDP — total outstanding debt including internal debt + provident fund + reserve funds"),
    "F04": ("Capital Expenditure as % of Total Expenditure",
             "% — capex divided by aggregate expenditure"),
    "F05": ("Revenue Deficit as % of GSDP",
             "% of GSDP — revenue deficit (may be negative if surplus)"),
    "F06": ("Interest Payments as % of Revenue Receipts",
             "% — interest burden on revenue receipts"),
    "H01": ("Infant Mortality Rate (SRS)",
             "per 1,000 live births — from SRS Bulletin, latest annual"),
}


EXTRACTION_PROMPT = """You are analyzing a State Economic Survey PDF from the Government of India.

Extract time-series data for these indicators. Only report values that are
DIRECTLY STATED in tables or text in the document. Do NOT estimate, infer,
extrapolate, or invent values. If a value isn't clearly stated, omit that row.

Target indicators:
{indicator_list}

For each value you find, return an object matching this schema exactly:
{{
  "state_name": "the state name as written in the document",
  "document_edition": "e.g. '2024-25' — the fiscal year the survey covers",
  "extractions": [
    {{
      "indicator_code": "E01" | "E02" | "F01" | "F02" | "F04" | "F05" | "F06" | "H01",
      "fiscal_year": 2018,     // integer, use fiscal-year-ending convention: 2017-18 → 2018
      "value": 5.9,             // numeric only, in the indicator's native unit
      "source_page": 42,        // page number where you found this value
      "source_table": "Table 1.3 · State GSDP Growth Rates",  // table or chapter title
      "confidence": "high" | "medium" | "low"  // how directly the value maps to the indicator
    }},
    ...
  ]
}}

Rules:
- Fiscal year uses ENDING calendar year (2017-18 → 2018).
- Only include years 2018 through 2026.
- If a table shows RE (Revised Estimates) or BE (Budget Estimates), you may include them —
  mark confidence as "medium" for RE and "low" for BE.
- Preferred order of source when the same indicator appears multiple times:
  Actuals > Revised Estimates > Budget Estimates. Include only one row per (indicator, year);
  pick the most reliable one.
- For indicator F04 (Capex % of Total Expenditure): only include if the survey provides
  the exact ratio; do NOT compute it yourself from absolute values.
- Watch for state-domestic-product rebasing (2011-12 → 2017-18 base). Prefer the newer
  base-year series if both are shown.
"""


def build_prompt() -> str:
    """Format the EXTRACTION_PROMPT with the indicator list."""
    lines = []
    for code, (name, note) in INDICATOR_PROMPTS.items():
        lines.append(f"  {code}: {name} — {note}")
    return EXTRACTION_PROMPT.format(indicator_list="\n".join(lines))


def build_response_schema() -> dict:
    """JSON schema constraining Gemini's output structure."""
    return {
        "type": "object",
        "properties": {
            "state_name":       {"type": "string"},
            "document_edition": {"type": "string"},
            "extractions": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "indicator_code": {"type": "string",
                                            "enum": list(INDICATOR_PROMPTS.keys())},
                        "fiscal_year":  {"type": "integer"},
                        "value":        {"type": "number"},
                        "source_page":  {"type": "integer"},
                        "source_table": {"type": "string"},
                        "confidence":   {"type": "string",
                                         "enum": ["high", "medium", "low"]},
                    },
                    "required": ["indicator_code", "fiscal_year", "value",
                                  "source_page", "confidence"],
                },
            },
        },
        "required": ["extractions"],
    }


def extract_via_gemini(pdf_path: Path) -> dict:
    """Send the PDF to Gemini, get back structured extractions."""
    try:
        from google import genai
        from google.genai import types
    except ImportError:
        raise SystemExit(
            "google-genai not installed. Install:\n"
            "    pip install google-genai\n"
        )

    project = os.environ.get("GCP_PROJECT")
    if not project:
        raise SystemExit("GCP_PROJECT env var not set — source secrets/.env first")

    client = genai.Client(vertexai=True, project=project, location="us-central1")
    pdf_bytes = pdf_path.read_bytes()

    print(f"  📄 Sending {pdf_path.name} ({len(pdf_bytes) // 1024}KB) to {MODEL_NAME} ...")
    resp = client.models.generate_content(
        model=MODEL_NAME,
        contents=[
            types.Part.from_bytes(data=pdf_bytes, mime_type="application/pdf"),
            build_prompt(),
        ],
        config=types.GenerateContentConfig(
            temperature=0.0,       # deterministic — data extraction, not creative
            response_mime_type="application/json",
            response_schema=build_response_schema(),
            max_output_tokens=32768,
        ),
    )

    try:
        parsed = json.loads(resp.text or "{}")
    except json.JSONDecodeError as e:
        raise RuntimeError(f"Gemini returned non-JSON: {e}\n\nRaw: {resp.text[:500]}")

    return parsed


def insert_into_db(session, state_code: str, extractions: list[dict],
                    pdf_path: Path, source_url: str | None,
                    dry_run: bool) -> dict:
    """Insert extraction results as gpi_indicator_values rows."""
    from app.gpi_models import GpiIndicator, GpiIndicatorValue
    from app.models import State

    st = session.query(State).filter_by(code=state_code).one_or_none()
    if not st:
        raise SystemExit(f"Unknown state code: {state_code}")

    indicators_by_code = {i.code: i for i in session.query(GpiIndicator).all()}

    counts = {"inserted": 0, "updated": 0, "skipped_unknown_indicator": 0,
              "skipped_out_of_window": 0, "skipped_low_confidence": 0}

    for row in extractions:
        code = row["indicator_code"]
        year = row["fiscal_year"]
        value = row["value"]
        confidence = row.get("confidence", "medium")

        ind = indicators_by_code.get(code)
        if not ind:
            counts["skipped_unknown_indicator"] += 1
            continue
        if year < 2018 or year > 2026:
            counts["skipped_out_of_window"] += 1
            continue

        existing = session.query(GpiIndicatorValue).filter_by(
            indicator_id=ind.id, state_id=st.id, fiscal_year=year
        ).one_or_none()

        source_table = row.get("source_table") or ""
        source_page = row.get("source_page")
        source_doc = f"{pdf_path.name}"
        if source_table:
            source_doc += f" · {source_table}"
        if source_page:
            source_doc += f" · p{source_page}"

        payload = {
            "raw_value":         value,
            "source_url":        source_url,
            "source_document":   source_doc,
            "extraction_method": "llm_extracted",
            "staleness":         "current",
            "notes":             f"Gemini confidence: {confidence}",
            "extracted_at":      datetime.utcnow(),
        }

        if existing:
            # Don't overwrite a high-confidence existing value with a lower-confidence one
            existing_conf = (existing.notes or "").lower()
            if "high" in existing_conf and confidence != "high":
                continue
            for k, v in payload.items():
                setattr(existing, k, v)
            existing.normalized_value = None
            existing.national_rank = None
            counts["updated"] += 1
        else:
            session.add(GpiIndicatorValue(
                indicator_id=ind.id,
                state_id=st.id,
                fiscal_year=year,
                **payload,
            ))
            counts["inserted"] += 1

    if not dry_run:
        session.commit()

    return counts


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                  formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--pdf", required=True, help="Path to the Economic Survey PDF")
    ap.add_argument("--state", required=True, help="2-letter state code (e.g. PB)")
    ap.add_argument("--source-url", default=None,
                    help="Original download URL (for provenance)")
    ap.add_argument("--dry-run", action="store_true",
                    help="Extract and print results, don't write to DB")
    ap.add_argument("--save-raw", action="store_true",
                    help="Save Gemini's raw JSON alongside the PDF (default: True)")
    args = ap.parse_args()

    pdf_path = Path(args.pdf)
    if not pdf_path.exists():
        raise SystemExit(f"No such file: {pdf_path}")

    # ── Extract ──────────────────────────────────────────────────────────────
    print(f"Extracting from {pdf_path.name} ...")
    result = extract_via_gemini(pdf_path)

    # Save raw for auditability
    raw_out = pdf_path.with_suffix(".gemini.json")
    raw_out.write_text(json.dumps(result, indent=2, ensure_ascii=False))
    print(f"  💾 Raw JSON saved → {raw_out.relative_to(ROOT)}")

    extractions = result.get("extractions", [])
    print(f"  ✓ Gemini returned {len(extractions)} extractions")
    print(f"    State: {result.get('state_name')}")
    print(f"    Edition: {result.get('document_edition')}")

    if not extractions:
        print("\n⚠ No extractions returned. Check the PDF has the expected data,")
        print("  and that Gemini could read it. Common issues:")
        print("  • PDF is a scanned image without OCR — Gemini can OCR but slowly.")
        print("  • Statistical Appendix section is not present in this survey edition.")
        sys.exit(1)

    # Preview
    print("\n  Preview:")
    for row in extractions[:10]:
        print(f"    {row['indicator_code']:<5s}  FY{row['fiscal_year']}  "
              f"value={row['value']:<12.2f}  conf={row['confidence']:<6s}  "
              f"p{row.get('source_page','?')}")
    if len(extractions) > 10:
        print(f"    ... and {len(extractions) - 10} more")

    # ── Insert ───────────────────────────────────────────────────────────────
    from app.database import SessionLocal
    from app import models  # ensures gpi_models loaded

    session = SessionLocal()
    try:
        counts = insert_into_db(session, args.state, extractions, pdf_path,
                                  args.source_url, args.dry_run)
    finally:
        session.close()

    print()
    print("═══════════════ Insert Summary ═══════════════")
    print(f"  Inserted:  {counts['inserted']}")
    print(f"  Updated:   {counts['updated']}")
    print(f"  Skipped (unknown indicator):    {counts['skipped_unknown_indicator']}")
    print(f"  Skipped (out of 2018-2026):     {counts['skipped_out_of_window']}")
    if args.dry_run:
        print("  (DRY RUN — no writes)")
    print()
    print("Next: python scripts/gpi_compute_scores.py --state " + args.state)


if __name__ == "__main__":
    main()
