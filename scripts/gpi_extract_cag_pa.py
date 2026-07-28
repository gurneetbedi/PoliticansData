"""Gemini extractor for CAG Performance Audit reports.

Each PA covers one specific scheme/topic (Health Infrastructure, MGNREGA,
UDAY, Higher Ed, etc.) — the extractor asks Gemini for a topic-agnostic
set of cross-comparable metrics:

    - total_projects_audited        (denominator)
    - projects_time_overrun         (numerator for delay rate)
    - projects_cost_overrun_over_10pct (numerator for cost overrun rate)
    - avg_time_overrun_months       (severity of delays)
    - avg_cost_overrun_pct          (severity of overruns)
    - total_budgeted / actual (₹ Crore)
    - financial_utilization_pct     (actual / budgeted × 100)
    - physical_achievement_pct      (vs targets)

Aggregated by gpi_aggregate_pa_efficiency.py into EF03-EF06 indicators.

Reads data/cag/pdfs/downloaded.csv, extracts PAs (report_type contains
"Performance"), caches raw Gemini JSON in data/cag/pa_extractions/.

Usage:
    source secrets/.env

    # Dry-run 5 PAs first to sanity-check the prompt
    python scripts/gpi_extract_cag_pa.py --limit 5 --dry-run

    # Extract all
    python scripts/gpi_extract_cag_pa.py

    # Only one state
    python scripts/gpi_extract_cag_pa.py --state Punjab

    # Force re-extract (bypasses cache)
    python scripts/gpi_extract_cag_pa.py --state Punjab --force
"""
from __future__ import annotations
import argparse
import csv
import json
import os
import re
import sys
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

DOWNLOADED_CSV = ROOT / "data" / "cag" / "pdfs" / "downloaded.csv"
RAW_JSON_DIR = ROOT / "data" / "cag" / "pa_extractions"

MODEL_NAME = "gemini-2.5-flash"
MAX_INLINE_MB = 30
SLICE_FIRST_N_PAGES = 200


# ═══════════════════════════════════════════════════════════════════════════
# Prompt + schema
# ═══════════════════════════════════════════════════════════════════════════
PROMPT = """You are analyzing a CAG (Comptroller and Auditor General of India)
PERFORMANCE AUDIT report. These reports evaluate the implementation of a
specific government scheme, department, or program (e.g., MGNREGA, Public
Health Infrastructure, District Hospitals, etc.).

Extract topic-agnostic metrics so we can aggregate across audits:

STRICT RULES:
  1. Only report values DIRECTLY STATED in the report. Do NOT compute,
     infer, or estimate. Omit fields not clearly present.
  2. "Projects/schemes audited" refers to the sample of individual works
     (roads, hospitals, sub-projects, DBT beneficiaries, etc.) the audit
     examined. Use the executive summary's headline count if given.
  3. Time overrun = project delayed beyond planned completion date.
     Cost overrun = actual cost exceeded original sanctioned by >10%.
  4. Extract the audit period the report covers (e.g., "2017-22" or
     single year "2019-20"). Report start + end fiscal years as ENDING
     calendar years (2017-18 → 2018).
  5. financial_utilization_pct = actual expenditure / allocated × 100.
     Just extract if directly stated; do not compute yourself.
  6. physical_achievement_pct = target achievement (e.g., "78% of planned
     works completed"). Look in executive summary.
"""

RESPONSE_SCHEMA = {
    "type": "object",
    "properties": {
        "state_name":                 {"type": "string"},
        "report_topic":               {"type": "string",
                                        "description": "Brief description of what this PA covers (e.g. 'MGNREGA Implementation')."},
        "report_no":                  {"type": "string"},
        "audit_period_start_fy":      {"type": "integer",
                                        "description": "Ending year of the START of audit period. E.g. audit '2017-22' → 2018."},
        "audit_period_end_fy":        {"type": "integer",
                                        "description": "Ending year of the END of audit period. E.g. audit '2017-22' → 2022."},
        "total_projects_audited":     {"type": "integer"},
        "projects_time_overrun":      {"type": "integer"},
        "projects_cost_overrun_over_10pct": {"type": "integer"},
        "avg_time_overrun_months":    {"type": "number"},
        "avg_cost_overrun_pct":       {"type": "number"},
        "total_budgeted_crore":       {"type": "number"},
        "total_actual_expenditure_crore": {"type": "number"},
        "financial_utilization_pct":  {"type": "number"},
        "physical_achievement_pct":   {"type": "number"},
        "observations_summary":       {"type": "string",
                                        "description": "1-2 sentences on the main audit findings."},
        "extraction_confidence":      {"type": "string", "enum": ["high", "medium", "low"]},
    },
    "required": ["state_name", "report_topic"],
}


def slice_pdf_first_pages(pdf_path: Path, n_pages: int) -> bytes:
    from pypdf import PdfReader, PdfWriter
    import io
    reader = PdfReader(str(pdf_path))
    writer = PdfWriter()
    for i in range(min(n_pages, len(reader.pages))):
        writer.add_page(reader.pages[i])
    buf = io.BytesIO()
    writer.write(buf)
    return buf.getvalue()


def get_pdf_bytes(pdf_path: Path) -> tuple[bytes, bool]:
    size_mb = pdf_path.stat().st_size / 1_048_576
    if size_mb <= MAX_INLINE_MB:
        return pdf_path.read_bytes(), False
    sliced = slice_pdf_first_pages(pdf_path, SLICE_FIRST_N_PAGES)
    return sliced, True


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
    pdf_bytes, was_sliced = get_pdf_bytes(pdf_path)
    prompt = PROMPT
    if was_sliced:
        prompt += ("\n\nNOTE: You are only seeing the first {n} pages of the PA "
                    "(Executive Summary + main analytical chapters). Cross-PA "
                    "aggregate figures live in these pages.").format(n=SLICE_FIRST_N_PAGES)

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
            max_output_tokens=8192,
        ),
    )
    return json.loads(resp.text or "{}")


def persist(session, meta: dict, extraction: dict, pdf_path: Path,
              source_url: str, dry_run: bool) -> str:
    from app.gpi_models import GpiCagPaExtraction
    from app.models import State

    state_name = extraction.get("state_name") or meta.get("state")
    if not state_name:
        return "no_state"
    st = session.query(State).filter_by(name=state_name).one_or_none()
    if not st:
        # Retry with normalized name (strip trailing spaces etc)
        st = session.query(State).filter_by(name=state_name.strip()).one_or_none()
    if not st:
        return f"unknown_state:{state_name}"

    report_no = extraction.get("report_no", "") or meta.get("report_no", "") or pdf_path.stem[:64]

    existing = session.query(GpiCagPaExtraction).filter_by(
        state_id=st.id, report_no=report_no,
    ).one_or_none()

    end_fy = extraction.get("audit_period_end_fy")
    start_fy = extraction.get("audit_period_start_fy")

    payload = {
        "state_id":                      st.id,
        "audit_period_start":            start_fy,
        "audit_period_end":              end_fy,
        "fiscal_year":                   end_fy,   # canonical mapping
        "report_topic":                  extraction.get("report_topic"),
        "report_no":                     report_no,
        "total_projects_audited":        extraction.get("total_projects_audited"),
        "projects_time_overrun":         extraction.get("projects_time_overrun"),
        "projects_cost_overrun_over_10pct": extraction.get("projects_cost_overrun_over_10pct"),
        "avg_time_overrun_months":       extraction.get("avg_time_overrun_months"),
        "avg_cost_overrun_pct":          extraction.get("avg_cost_overrun_pct"),
        "total_budgeted_crore":          extraction.get("total_budgeted_crore"),
        "total_actual_expenditure_crore": extraction.get("total_actual_expenditure_crore"),
        "financial_utilization_pct":     extraction.get("financial_utilization_pct"),
        "physical_achievement_pct":      extraction.get("physical_achievement_pct"),
        "observations_summary":          extraction.get("observations_summary"),
        "source_pdf":                    str(pdf_path.relative_to(ROOT)),
        "source_url":                    source_url,
        "extraction_confidence":         extraction.get("extraction_confidence"),
        "extracted_at":                  datetime.utcnow(),
        "gemini_raw_response":           json.dumps(extraction, ensure_ascii=False),
    }

    if existing:
        for k, v in payload.items():
            setattr(existing, k, v)
        result = "updated"
    else:
        session.add(GpiCagPaExtraction(**payload))
        result = "inserted"

    if not dry_run:
        session.commit()
    return result


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                  formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--state", help="Only process this state")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--force", action="store_true", help="Bypass cache")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    if not DOWNLOADED_CSV.exists():
        raise SystemExit(f"Downloaded manifest not found: {DOWNLOADED_CSV.relative_to(ROOT)}")

    with DOWNLOADED_CSV.open("r", encoding="utf-8") as f:
        all_rows = list(csv.DictReader(f))

    # Filter to Performance Audits
    rows = [r for r in all_rows
            if "performance" in r.get("report_type", "").lower()]
    if args.state:
        rows = [r for r in rows if r["state"] == args.state]
    if args.limit:
        rows = rows[:args.limit]

    print(f"Manifest total:  {len(all_rows)}")
    print(f"PA rows matched: {len(rows)}")
    print(f"Model:           {MODEL_NAME}")
    print()

    RAW_JSON_DIR.mkdir(parents=True, exist_ok=True)

    from app.database import SessionLocal
    from app import models
    session = SessionLocal()

    counts = {"extracted": 0, "cached": 0, "failed": 0, "persisted": 0}

    try:
        for i, row in enumerate(rows, 1):
            local_path = ROOT / row["local_path"]
            if not local_path.exists():
                print(f"[{i:>3d}/{len(rows)}] MISSING: {local_path}")
                counts["failed"] += 1
                continue

            print(f"[{i:>3d}/{len(rows)}] {row['state']:<15s} "
                  f"{local_path.name[:60]}")

            cache_path = RAW_JSON_DIR / (local_path.stem + ".gemini.json")
            if cache_path.exists() and not args.force:
                extraction = json.loads(cache_path.read_text())
                print(f"           ✓ cached")
                counts["cached"] += 1
            else:
                try:
                    t0 = time.time()
                    extraction = call_gemini(local_path)
                    dt = time.time() - t0
                    cache_path.write_text(json.dumps(extraction, indent=2, ensure_ascii=False))
                    print(f"           ✓ extracted in {dt:.0f}s")
                    counts["extracted"] += 1
                except Exception as e:
                    print(f"           ✗ Gemini error: {type(e).__name__}: {str(e)[:100]}")
                    counts["failed"] += 1
                    continue

            result = persist(session, row, extraction, local_path,
                              row["pdf_url"], args.dry_run)
            print(f"             → {result}  topic={extraction.get('report_topic', '?')[:40]!r}")
            if result in ("inserted", "updated"):
                counts["persisted"] += 1
    finally:
        session.close()

    print()
    print("═══════════════════════ Summary ═══════════════════════")
    for k, v in counts.items():
        print(f"  {k:<12s} {v}")
    if args.dry_run:
        print("  (dry run — no writes)")
    print()
    print("Next: python scripts/gpi_aggregate_pa_efficiency.py")


if __name__ == "__main__":
    main()
