"""National Health Accounts 2021-22 → GPI H04 Out-of-Pocket expenditure share.

Data source:
    MoHFW / NHSRC — "National Health Accounts Estimates for India 2021-22".
    Report has state-level breakdown in Annexure Tables A.6 + A.7 with
    OOPE (Out-of-Pocket Expenditure) as % of Total Health Expenditure (THE).

Target indicator:
    H04  Out-of-pocket health expenditure share
         (Direction: lower_better — lower OOPE % = better financial protection)

Fiscal-year: NHA 2021-22 → fiscal_year 2022.

Usage:
    source secrets/.env

    # 1. Download the NHA PDF (~5MB):
    mkdir -p data/nha
    curl -L -o data/nha/NHA_2021-22.pdf \\
      "https://nhsrcindia.org/sites/default/files/2024-09/NHA%202021-22.pdf"

    # 2. Ingest:
    python scripts/gpi_ingest_nha.py --pdf data/nha/NHA_2021-22.pdf

    # Dry-run:
    python scripts/gpi_ingest_nha.py --pdf data/nha/NHA_2021-22.pdf --dry-run

Caveat: NHA Annexure A.6 covers "select States" that have completed State
Health Accounts studies (typically ~10-15 states). Not all 28 states may be
present. Table A.7 covers all states/UTs with Legislature — that's what we
prioritize.
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
RAW_JSON_DIR = ROOT / "data" / "nha" / "extractions"

NHA_FY = 2022   # NHA 2021-22 → fiscal year ending 2022


PROMPT = """You are analyzing an MoHFW / NHSRC "National Health Accounts
Estimates for India 2021-22" PDF. This report has state-level health
financing tables in Annexures A.6 and A.7.

Target metric for H04 (Out-of-Pocket Expenditure share):

    OOPE (Out-of-Pocket Expenditure) as % of Total Health Expenditure (THE),
    by State/UT, for the reference year 2021-22.

Preferred source in the PDF (in priority order):
    1. Annexure Table A.6 "Key Health Financing Indicators for select States"
        — has a column labeled "OOPE as % of THE" or "OOPE % of THE" for
        each state that has completed a State Health Account study.
    2. Annexure Table A.7 "Government Health Financing indicators" — if it
        includes OOPE share by state.
    3. Any other state-level table in the report that presents OOPE as
        percent of THE for individual states.

STRICT RULES:
  1. Only include values DIRECTLY STATED in the report tables.
     Do NOT compute, infer, or estimate.
  2. Use the 2021-22 reference period value. If both 2020-21 and 2021-22
     are shown for the same state, take 2021-22.
  3. Return OOPE as a percentage (e.g., 39.4, not 0.394).
  4. Some states won't have State Health Accounts — omit those entirely
     (do NOT insert all-India value as a placeholder).
  5. Skip all-India totals, aggregate rows, and UT-block subtotals.

Return JSON matching the response schema strictly.
"""

RESPONSE_SCHEMA = {
    "type": "object",
    "properties": {
        "reference_year": {
            "type": "string",
            "description": "Fiscal year the estimates refer to (e.g., '2021-22')."
        },
        "state_data": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "state_name":       {"type": "string"},
                    "oope_pct_of_the":  {"type": "number",
                                          "description": "OOPE as % of Total Health Expenditure"},
                    "source_table":     {"type": "string",
                                          "description": "Annexure table label (e.g., 'A.6')"},
                },
                "required": ["state_name", "oope_pct_of_the"],
            },
        },
        "extraction_notes": {
            "type": "string",
            "description": "Any caveats — states omitted, methodology notes."
        },
    },
    "required": ["state_data"],
}


MAX_INLINE_MB = 30


def load_pdf_bytes(pdf_path: Path) -> bytes:
    """Load PDF, slicing aggressively if oversize.

    NHA 2021-22 target tables are Annexure A.6 and A.7, which sit near the END
    of the report (typically last 20-30 pages). If the PDF is > MAX_INLINE_MB,
    we slice progressively — LAST N pages, halving N until under the limit.
    """
    size_mb = pdf_path.stat().st_size / 1_048_576
    if size_mb <= MAX_INLINE_MB:
        return pdf_path.read_bytes()

    import io
    from pypdf import PdfReader, PdfWriter
    reader = PdfReader(str(pdf_path))
    total = len(reader.pages)

    # Try successively smaller slices from the END (Annexures live at back)
    for take in [40, 30, 25, 20, 15, 12, 10]:
        take = min(take, total)
        writer = PdfWriter()
        for i in range(total - take, total):
            writer.add_page(reader.pages[i])
        buf = io.BytesIO()
        writer.write(buf)
        data = buf.getvalue()
        data_mb = len(data) / 1_048_576
        if data_mb <= MAX_INLINE_MB:
            print(f"    ✂ PDF {size_mb:.0f}MB > {MAX_INLINE_MB}MB — sliced to LAST "
                  f"{take}/{total} pages = {data_mb:.1f}MB (Annexure section)",
                  flush=True)
            return data

    raise SystemExit(f"Even 10-page slice exceeds {MAX_INLINE_MB}MB — "
                      "PDF has huge embedded images. Manually compress with "
                      "`gs -sDEVICE=pdfwrite -dPDFSETTINGS=/ebook ...` and retry.")


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


def insert_values(session, state_data: list[dict], source_pdf: str,
                    dry_run: bool) -> dict:
    from app.gpi_models import GpiIndicator, GpiIndicatorValue
    from app.models import State

    ind = session.query(GpiIndicator).filter_by(code="H04").one_or_none()
    if not ind:
        raise SystemExit("Indicator H04 not seeded — run gpi_seed.py")

    states_by_name = {s.name: s for s in session.query(State).all()}
    counts = {"inserted": 0, "updated": 0, "skipped": 0, "unresolved": []}

    for row in state_data:
        val = row.get("oope_pct_of_the")
        if val is None:
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

        existing = session.query(GpiIndicatorValue).filter_by(
            indicator_id=ind.id, state_id=st.id, fiscal_year=NHA_FY,
        ).one_or_none()

        payload = {
            "raw_value":         val,
            "source_url":        "https://nhsrcindia.org/publications/national-health-accounts",
            "source_document":   ("NHSRC / MoHFW · National Health Accounts Estimates 2021-22 · "
                                    f"{source_pdf} · {row.get('source_table', '?')}"),
            "extraction_method": "llm_extracted",
            "staleness":         "current",
            "notes":             ("OOPE as % of Total Health Expenditure. "
                                    f"Reference: 2021-22. Table: {row.get('source_table', '?')}."),
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
                indicator_id=ind.id, state_id=st.id, fiscal_year=NHA_FY,
                **payload,
            ))
            counts["inserted"] += 1

    if not dry_run:
        session.commit()
    return counts


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                  formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--pdf", required=True, help="Path to NHA 2021-22 PDF")
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

    print(f"\nH04 OOPE as % of THE — FY{NHA_FY}")
    print(f"  Inserted:  {counts['inserted']}")
    print(f"  Updated:   {counts['updated']}")
    print(f"  Skipped:   {counts['skipped']}")
    if counts["unresolved"]:
        print(f"  Unresolved: {counts['unresolved']}")
    if args.dry_run:
        print("\n(dry-run — no writes)")
    print("\nNext: python scripts/gpi_compute_scores.py")


if __name__ == "__main__":
    main()
