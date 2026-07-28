"""NITI Aayog composite-index PDF → GPI indicator (SDG / FHI / CWMI).

Three NITI Aayog reports, one ingester:
  --report sdg   SDG India Index 2023-24        → G05 (governance pillar)
  --report fhi   Fiscal Health Index 2025       → F07 (public finance pillar)
                                                     (18 major states only)
  --report cwmi  Composite Water Management 2.0 → I07 (infrastructure pillar)
                                                     (25 states + 2 UTs)

Each report publishes a single-page (or 2-page) state-ranking table where
we need one column: the OVERALL COMPOSITE SCORE per state. Since the layouts
differ (portrait/landscape/wrapped), we use Gemini with a strict schema
rather than fragile pypdf parsing.

Usage:
    source secrets/.env

    # 1. Download the 3 PDFs:
    mkdir -p data/niti
    curl -L -o data/niti/SDG_India_Index_2023-24.pdf \\
      "https://www.niti.gov.in/sites/default/files/2024-07/SDG_India_Index_2023-24.pdf"
    curl -L -o data/niti/Fiscal_Health_Index_2025.pdf \\
      "https://niti.gov.in/sites/default/files/2025-01/Fiscal_Health_Index_24012025_Final.pdf"
    curl -L -o data/niti/CWMI_2.0.pdf \\
      "https://www.niti.gov.in/sites/default/files/2023-03/Composite%20Water%20Management%20Index%202.0.pdf"

    # 2. Ingest (one report at a time):
    python scripts/gpi_ingest_niti_index.py --report sdg  --pdf data/niti/SDG_India_Index_2023-24.pdf
    python scripts/gpi_ingest_niti_index.py --report fhi  --pdf data/niti/Fiscal_Health_Index_2025.pdf
    python scripts/gpi_ingest_niti_index.py --report cwmi --pdf data/niti/CWMI_2.0.pdf

    python scripts/gpi_compute_scores.py
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
RAW_JSON_DIR = ROOT / "data" / "niti" / "extractions"


# ═══════════════════════════════════════════════════════════════════════════
# Per-report config: which indicator to write, prompt, fiscal_year mapping
# ═══════════════════════════════════════════════════════════════════════════
REPORTS = {
    "sdg": {
        "indicator_code":  "G05",
        "fiscal_year":     2024,   # SDG India Index 2023-24 published July 2024
        "unit_hint":       "0-100 score (higher is better)",
        "source_url":      "https://www.niti.gov.in/reports-sdg",
        "source_document": "NITI Aayog · SDG India Index 2023-24",
        "prompt_extra": (
            "The SDG India Index scores each State/UT on a 0-100 scale "
            "composited from 16 goals, 70 targets, 113 indicators. Extract "
            "the OVERALL / COMPOSITE / AGGREGATE SDG SCORE per state — NOT "
            "goal-specific scores. Look for a summary state-ranking table "
            "labelled 'Composite Score' or 'Overall Score' (usually near "
            "the front of the report and again in the ranking summary)."
        ),
    },
    "fhi": {
        "indicator_code":  "F07",
        "fiscal_year":     2023,   # FHI 2025 covers FY2022-23 data
        "unit_hint":       "0-100 score (higher is better)",
        "source_url":      "https://www.niti.gov.in/whats-new/fiscal-health-index-2025",
        "source_document": "NITI Aayog · Fiscal Health Index 2025 (FY2022-23 data)",
        "prompt_extra": (
            "The Fiscal Health Index scores 18 MAJOR states on a 0-100 scale, "
            "composited from 5 sub-indices: Revenue Mobilization, Expenditure "
            "Quality, Fiscal Prudence, Debt Index, Debt Sustainability. "
            "Extract the OVERALL COMPOSITE FHI SCORE per state (Odisha topped "
            "at 67.8 in FHI 2025). Only 18 states will be present — that's "
            "expected. Ignore sub-index breakdowns unless the state's overall "
            "score is not otherwise available."
        ),
    },
    "cwmi": {
        "indicator_code":  "I07",
        "fiscal_year":     2018,   # CWMI 2.0 covers FY2017-18 as latest
        "unit_hint":       "0-100 score (higher is better)",
        "source_url":      "https://social.niti.gov.in/water-index",
        "source_document": "NITI Aayog · Composite Water Management Index 2.0 (FY2017-18)",
        "prompt_extra": (
            "The Composite Water Management Index scores 25 states + 2 UTs on "
            "a 0-100 scale, composited from 9 themes covering irrigation, "
            "drinking water, groundwater, watershed development, and governance. "
            "Extract the OVERALL CWMI SCORE for FY2017-18 (or the LATEST year "
            "the report presents) per state. States are usually grouped into "
            "'Non-Himalayan' and 'North-Eastern & Himalayan' categories — "
            "extract all of them and let downstream normalization handle it. "
            "Ignore theme-wise sub-scores unless the overall composite is not "
            "otherwise available."
        ),
    },
}


# ═══════════════════════════════════════════════════════════════════════════
# Gemini prompt + response schema (shared across all 3 reports)
# ═══════════════════════════════════════════════════════════════════════════
BASE_PROMPT = """You are analyzing a NITI Aayog composite-index PDF report.
Your job is to extract ONE overall composite score per State/UT from the
state-ranking table(s) in the report.

{report_specific}

STRICT RULES:
  1. Only include values DIRECTLY STATED in the report tables.
     Do NOT compute, infer, or estimate.
  2. Return the OVERALL / COMPOSITE / AGGREGATE score for each state — NOT
     sub-component scores (e.g., not goal-1-only or theme-1-only).
  3. Skip all-India averages, aggregate rows, and category subtotals.
  4. Include the state's NATIONAL RANK if the table publishes it —
     otherwise leave rank null (we'll compute rank downstream).
  5. Score is a number in the range roughly 0-100 (see {unit_hint}).

Return JSON matching the response schema strictly.
"""

RESPONSE_SCHEMA = {
    "type": "object",
    "properties": {
        "reference_period": {
            "type": "string",
            "description": "Reporting period as shown on the report cover (e.g., '2023-24')."
        },
        "state_data": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "state_name":       {"type": "string"},
                    "composite_score":  {"type": "number",
                                          "description": "Overall composite index score (higher = better)"},
                    "rank":             {"type": "number",
                                          "description": "National rank if published (null otherwise)"},
                    "source_table":     {"type": "string",
                                          "description": "Table label / page reference"},
                },
                "required": ["state_name", "composite_score"],
            },
        },
        "extraction_notes": {
            "type": "string",
            "description": "Any caveats — states omitted, aggregation notes."
        },
    },
    "required": ["state_data"],
}


MAX_INLINE_MB = 30


def load_pdf_bytes(pdf_path: Path) -> bytes:
    """Progressive slice-down for oversized PDFs — try LAST N pages first
    (state-ranking summary tables usually sit at back), fall back to first N."""
    size_mb = pdf_path.stat().st_size / 1_048_576
    if size_mb <= MAX_INLINE_MB:
        return pdf_path.read_bytes()

    import io
    from pypdf import PdfReader, PdfWriter
    reader = PdfReader(str(pdf_path))
    total = len(reader.pages)

    for take in [80, 60, 40, 30, 20, 15]:
        take = min(take, total)
        # First try FRONT slice (many NITI reports have summary tables in the intro)
        for label, page_range in [
            ("FIRST", range(0, take)),
            ("LAST",  range(total - take, total)),
        ]:
            writer = PdfWriter()
            for i in page_range:
                writer.add_page(reader.pages[i])
            buf = io.BytesIO()
            writer.write(buf)
            data = buf.getvalue()
            data_mb = len(data) / 1_048_576
            if data_mb <= MAX_INLINE_MB:
                print(f"    ✂ PDF {size_mb:.0f}MB > {MAX_INLINE_MB}MB — sliced to {label} "
                      f"{take}/{total} pages = {data_mb:.1f}MB", flush=True)
                return data

    raise SystemExit(f"Even 15-page slice exceeds {MAX_INLINE_MB}MB — compress with "
                      "ghostscript and retry.")


def call_gemini(pdf_path: Path, report_key: str) -> dict:
    try:
        from google import genai
        from google.genai import types
    except ImportError:
        raise SystemExit("pip install google-genai")

    project = os.environ.get("GCP_PROJECT")
    if not project:
        raise SystemExit("GCP_PROJECT not set — source secrets/.env")

    cfg = REPORTS[report_key]
    prompt = BASE_PROMPT.format(
        report_specific=cfg["prompt_extra"],
        unit_hint=cfg["unit_hint"],
    )

    client = genai.Client(vertexai=True, project=project, location="us-central1")
    pdf_bytes = load_pdf_bytes(pdf_path)
    print(f"    → Gemini call ({len(pdf_bytes) // 1_048_576}MB PDF, report={report_key})",
          flush=True)

    resp = client.models.generate_content(
        model=MODEL_NAME,
        contents=[
            types.Part.from_bytes(data=pdf_bytes, mime_type="application/pdf"),
            prompt,
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
# State-name normalization (same alias table as other ingesters)
# ═══════════════════════════════════════════════════════════════════════════
STATE_ALIASES = {
    "orissa":                       "Odisha",
    "odisha":                       "Odisha",
    "jammu & kashmir":              "Jammu and Kashmir",
    "jammu and kashmir":            "Jammu and Kashmir",
    "j&k":                          "Jammu and Kashmir",
    "nct of delhi":                 "Delhi",
    "nct delhi":                    "Delhi",
    "delhi":                        "Delhi",
    "chhattisgarh":                 "Chhattisgarh",
    "chattisgarh":                  "Chhattisgarh",
    "uttarakhand":                  "Uttarakhand",
    "uttaranchal":                  "Uttarakhand",
    "puducherry":                   "Puducherry",
    "pondicherry":                  "Puducherry",
    # UTs / all-India we don't score in GPI
    "india":                        None,
    "all india":                    None,
    "all-india":                    None,
    "andaman & nicobar islands":    None,
    "andaman and nicobar islands":  None,
    "a & n islands":                None,
    "a&n islands":                  None,
    "chandigarh":                   None,
    "ladakh":                       None,
    "lakshadweep":                  None,
    "dadra & nagar haveli":         None,
    "daman & diu":                  None,
    "dadra and nagar haveli and daman & diu": None,
    "d&n haveli and daman & diu":   None,
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


# ═══════════════════════════════════════════════════════════════════════════
# DB insert
# ═══════════════════════════════════════════════════════════════════════════
def insert_values(session, state_data: list[dict], report_key: str,
                    source_pdf: str, dry_run: bool) -> dict:
    from app.gpi_models import GpiIndicator, GpiIndicatorValue
    from app.models import State

    cfg = REPORTS[report_key]
    ind = session.query(GpiIndicator).filter_by(code=cfg["indicator_code"]).one_or_none()
    if not ind:
        raise SystemExit(f"Indicator {cfg['indicator_code']} not seeded — run gpi_seed.py")

    states_by_name = {s.name: s for s in session.query(State).all()}
    counts = {"inserted": 0, "updated": 0, "skipped": 0, "unresolved": []}

    fy = cfg["fiscal_year"]

    for row in state_data:
        val = row.get("composite_score")
        if val is None:
            continue

        canon = normalize_state_name(row.get("state_name"))
        if canon is None:
            counts["skipped"] += 1
            continue
        st = states_by_name.get(canon)
        if not st:
            counts["unresolved"].append((row.get("state_name"), canon))
            continue

        notes_parts = [f"table={row.get('source_table', '?')}"]
        if row.get("rank") is not None:
            notes_parts.append(f"published_rank={int(row['rank'])}")

        existing = session.query(GpiIndicatorValue).filter_by(
            indicator_id=ind.id, state_id=st.id, fiscal_year=fy,
        ).one_or_none()

        payload = {
            "raw_value":         val,
            "source_url":        cfg["source_url"],
            "source_document":   f"{cfg['source_document']} · {source_pdf}",
            "extraction_method": "llm_extracted",
            "staleness":         "current",
            "notes":             " · ".join(notes_parts),
            "extracted_at":      datetime.utcnow(),
        }

        if existing:
            for k, v in payload.items():
                setattr(existing, k, v)
            existing.normalized_value = None
            # Preserve any published rank if provided; otherwise let
            # gpi_compute_scores.py compute a rank downstream.
            if row.get("rank") is not None:
                existing.national_rank = int(row["rank"])
            else:
                existing.national_rank = None
            counts["updated"] += 1
        else:
            new_row = GpiIndicatorValue(
                indicator_id=ind.id, state_id=st.id, fiscal_year=fy,
                **payload,
            )
            if row.get("rank") is not None:
                new_row.national_rank = int(row["rank"])
            session.add(new_row)
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
    ap.add_argument("--report", required=True, choices=list(REPORTS.keys()),
                    help="Which NITI report: sdg | fhi | cwmi")
    ap.add_argument("--pdf", required=True, help="Path to the NITI report PDF")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--force", action="store_true",
                    help="Re-call Gemini even if cached JSON exists")
    args = ap.parse_args()

    pdf_path = Path(args.pdf)
    if not pdf_path.exists():
        raise SystemExit(f"No such file: {pdf_path}")

    RAW_JSON_DIR.mkdir(parents=True, exist_ok=True)
    cache_path = RAW_JSON_DIR / f"{args.report}__{pdf_path.stem}.gemini.json"

    if cache_path.exists() and not args.force:
        extraction = json.loads(cache_path.read_text())
        print(f"  ✓ using cached extraction: {cache_path.relative_to(ROOT)}")
    else:
        t0 = time.time()
        try:
            extraction = call_gemini(pdf_path, args.report)
        except Exception as e:
            print(f"  ✗ Gemini error: {type(e).__name__}: {e}")
            sys.exit(1)
        dt = time.time() - t0
        cache_path.write_text(json.dumps(extraction, indent=2, ensure_ascii=False))
        print(f"  ✓ extracted in {dt:.0f}s → {cache_path.relative_to(ROOT)}")

    state_data = extraction.get("state_data", [])
    print(f"  Reference: {extraction.get('reference_period')}")
    print(f"  Rows returned: {len(state_data)}")
    if extraction.get("extraction_notes"):
        print(f"  Notes: {extraction['extraction_notes'][:250]}")

    from app.database import SessionLocal
    session = SessionLocal()
    counts = insert_values(session, state_data, args.report,
                             pdf_path.name, args.dry_run)

    cfg = REPORTS[args.report]
    print(f"\n{cfg['indicator_code']} — {cfg['source_document']}")
    print(f"  Inserted:  {counts['inserted']}")
    print(f"  Updated:   {counts['updated']}")
    print(f"  Skipped:   {counts['skipped']}")
    if counts["unresolved"]:
        print(f"  Unresolved: {counts['unresolved']}")

    # Sanity summary — top 5 + bottom 5
    if not args.dry_run:
        from app.gpi_models import GpiIndicator, GpiIndicatorValue
        from app.models import State
        ind = session.query(GpiIndicator).filter_by(code=cfg["indicator_code"]).one()
        rows = (session.query(GpiIndicatorValue, State)
                .join(State, State.id == GpiIndicatorValue.state_id)
                .filter(GpiIndicatorValue.indicator_id == ind.id,
                        GpiIndicatorValue.fiscal_year == cfg["fiscal_year"])
                .all())
        rows.sort(key=lambda r: r[0].raw_value, reverse=True)
        print(f"\nTop 5 (FY{cfg['fiscal_year']}):")
        for v, st in rows[:5]:
            print(f"  {st.name:<25s} {v.raw_value:>6.1f}")
        if len(rows) > 8:
            print("  ...")
            print("Bottom 5:")
            for v, st in rows[-5:]:
                print(f"  {st.name:<25s} {v.raw_value:>6.1f}")

    session.close()
    if args.dry_run:
        print("\n(dry-run — no writes)")
    print("\nNext: python scripts/gpi_compute_scores.py")


if __name__ == "__main__":
    main()
