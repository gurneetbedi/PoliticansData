"""Gemini-powered extractor for CAG State Finances Audit Reports (SFARs).

Reads  data/cag/pdfs/downloaded.csv  to find SFAR PDFs, sends each to Gemini
with a strict response schema, and stores the extracted fiscal indicators +
audit-para counts.

Persists to two places:
  1. gpi_cag_extractions — full structured extract per SFAR (all fields)
  2. gpi_indicator_values — F02 (debt/GSDP), F06 (interest/rev receipts)
     inserted so the scoring engine picks them up automatically

Cross-validates:
  - F01 (fiscal deficit / GSDP) vs RBI-Handbook-derived value already in DB
  - F05 (revenue deficit / GSDP) vs RBI value
  Flags discrepancies > 10% for manual review.

Design choices:
  - We send the WHOLE SFAR PDF to Gemini 2.5 Flash. SFARs are 50-300 pages;
    at ~2c per PDF for the 8 Punjab reports the cost is trivial (~$0.20 total).
    Optimization to slice-by-chapter can come later if scaling to all states.
  - We save the RAW Gemini response as a JSON file next to the PDF for audit,
    and also stash it in gpi_cag_extractions.gemini_raw_response.
  - Idempotent: re-running skips PDFs already extracted unless --force.

Usage:
    source secrets/.env

    # Extract all downloaded SFARs
    python scripts/gpi_extract_cag_sfar.py

    # Only Punjab
    python scripts/gpi_extract_cag_sfar.py --state Punjab

    # Force re-extract even if already done (e.g. after prompt tweak)
    python scripts/gpi_extract_cag_sfar.py --force

    # Dry-run — call Gemini, print result, don't touch DB
    python scripts/gpi_extract_cag_sfar.py --dry-run
"""
from __future__ import annotations
import argparse
import csv
import json
import os
import sys
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

DOWNLOADED_CSV = ROOT / "data" / "cag" / "pdfs" / "downloaded.csv"
RAW_JSON_DIR = ROOT / "data" / "cag" / "extractions"

MODEL_NAME = "gemini-2.5-flash"


# ═══════════════════════════════════════════════════════════════════════════
# Gemini prompt + response schema
# ═══════════════════════════════════════════════════════════════════════════
PROMPT = """You are analyzing a CAG (Comptroller and Auditor General of India)
State Finances Audit Report (SFAR) PDF.

Extract the following fiscal indicators for the state and audit year this
report covers. These indicators are typically presented in the "Fiscal
Indicators Summary" or "Overview of State Finances" section in the early
chapters (usually Chapter 1 or 2). The Executive Summary often has them too.

STRICT RULES:
  1. Only report values DIRECTLY STATED in the PDF. Do NOT compute, infer,
     or estimate. If a value isn't explicitly shown in the audit report,
     OMIT it (return null or don't include the key).
  2. Use "as % of GSDP" values for all ratio fields. If the SFAR reports
     the same ratio in multiple forms (%, per capita), prefer the %-of-GSDP
     figure.
  3. For "Interest Payments to Revenue Receipts", the SFAR usually reports
     it directly. Do NOT compute it manually.
  4. Audit-para counts often appear in the Executive Summary or in a table
     titled "Follow-up of Audit Reports" or "Status of Audit Recommendations".
     Report the counts if visible.
  5. State the AUDIT YEAR the SFAR covers (e.g. "2022-23"), not the
     publication year.
  6. If the report contains multi-year time-series data (typical: current
     audit year + 4 prior years), only extract the CURRENT AUDIT YEAR's
     figures. Do not aggregate.
  7. SIGN CONVENTION: report DEFICIT values as POSITIVE numbers (magnitude).
     CAG tables sometimes present "Deficit = Receipts − Expenditure" which
     is negative when a deficit exists. Convert to positive magnitude before
     reporting. (Example: if the table shows -2.65, report 2.65.) Similarly
     for revenue deficit and primary deficit. If the state has a SURPLUS,
     report the fiscal deficit as 0 and note it in extraction_notes.

Return JSON matching the response schema strictly.
"""

RESPONSE_SCHEMA = {
    "type": "object",
    "properties": {
        "state_name":       {"type": "string"},
        "audit_year":       {"type": "string",
                              "description": "Fiscal year the report covers, e.g. '2022-23'"},
        "report_number":    {"type": "string",
                              "description": "e.g. 'Report No. 2 of 2024'"},
        "fiscal_indicators": {
            "type": "object",
            "properties": {
                "gross_fiscal_deficit_pct_gsdp":  {"type": "number"},
                "revenue_deficit_pct_gsdp":       {"type": "number"},
                "primary_deficit_pct_gsdp":       {"type": "number"},
                "debt_pct_gsdp":                  {"type": "number"},
                "interest_pct_revenue_receipts":  {"type": "number"},
                "capital_outlay_pct_gsdp":        {"type": "number"},
                "revenue_receipts_pct_gsdp":      {"type": "number"},
                "own_tax_revenue_pct_gsdp":       {"type": "number"},
            },
        },
        "absolute_figures": {
            "type": "object",
            "properties": {
                "gsdp_current_crore":       {"type": "number",
                                              "description": "GSDP at current prices, in ₹ Crore"},
                "outstanding_debt_crore":   {"type": "number"},
            },
        },
        "audit_paras": {
            "type": "object",
            "properties": {
                "raised_this_year":            {"type": "integer"},
                "outstanding_over_5_years":    {"type": "integer"},
                "pac_recommendations_pending": {"type": "integer"},
                "money_value_of_observations_crore": {"type": "number"},
            },
        },
        "extraction_confidence": {"type": "string", "enum": ["high", "medium", "low"]},
        "extraction_notes": {"type": "string",
                              "description": "Any caveats about the extraction — which section values came from, ambiguities, etc."},
    },
    "required": ["state_name", "audit_year"],
}


# PDFs over this size get pre-sliced. Gemini's inline-part limit is ~50MB,
# but even 30-40MB PDFs sometimes trip INVALID_ARGUMENT. Fiscal indicator
# summaries in CAG SFARs live in Chapter 1 + Executive Summary. In older
# SFARs 60 pages was enough; newer editions (2023-24+) push some tables
# into Chapter 2. Slicing to first 150 pages covers both without breaching
# Gemini's payload limit for typical CAG editions.
MAX_INLINE_MB = 30
SLICE_FIRST_N_PAGES = 150


def slice_pdf_first_pages(pdf_path: Path, n_pages: int) -> bytes:
    """Return an in-memory PDF containing only the first n_pages of pdf_path."""
    try:
        from pypdf import PdfReader, PdfWriter
    except ImportError:
        raise SystemExit("pip install pypdf")

    import io
    reader = PdfReader(str(pdf_path))
    writer = PdfWriter()
    total = len(reader.pages)
    take = min(n_pages, total)
    for i in range(take):
        writer.add_page(reader.pages[i])
    buf = io.BytesIO()
    writer.write(buf)
    return buf.getvalue()


def get_pdf_bytes_for_gemini(pdf_path: Path) -> tuple[bytes, bool]:
    """Return (bytes_to_send, was_sliced).
    Slices PDFs over MAX_INLINE_MB to the first SLICE_FIRST_N_PAGES pages —
    where the fiscal indicators summary lives in CAG SFARs anyway."""
    size_mb = pdf_path.stat().st_size / 1_048_576
    if size_mb <= MAX_INLINE_MB:
        return pdf_path.read_bytes(), False

    print(f"    ✂ PDF is {size_mb:.0f}MB > {MAX_INLINE_MB}MB threshold; "
          f"slicing to first {SLICE_FIRST_N_PAGES} pages ...", flush=True)
    sliced = slice_pdf_first_pages(pdf_path, SLICE_FIRST_N_PAGES)
    print(f"    ✂ sliced to {len(sliced) // 1_048_576}MB", flush=True)
    return sliced, True


def call_gemini(pdf_path: Path) -> dict:
    """Send the SFAR PDF to Gemini and return parsed JSON.
    Large PDFs are pre-sliced to Chapter 1 (first 80 pages) where the
    fiscal-indicators summary always lives."""
    try:
        from google import genai
        from google.genai import types
    except ImportError:
        raise SystemExit("pip install google-genai")

    project = os.environ.get("GCP_PROJECT")
    if not project:
        raise SystemExit("GCP_PROJECT env var not set — source secrets/.env first")

    client = genai.Client(vertexai=True, project=project, location="us-central1")

    pdf_bytes, was_sliced = get_pdf_bytes_for_gemini(pdf_path)
    prompt = PROMPT
    if was_sliced:
        prompt = PROMPT + (
            f"\n\nNOTE: You are only seeing the first {SLICE_FIRST_N_PAGES} pages of "
            f"the SFAR (Executive Summary + Chapter 1: Overview of State Finances). "
            f"The fiscal indicators summary and audit-para counts are always in this "
            f"section, so this should be complete for your extraction. If a specific "
            f"value referenced here can't be found in these pages, omit it (do not guess)."
        )

    print(f"    → Gemini call ({len(pdf_bytes) // 1_048_576}MB PDF) ...", flush=True)
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


# ═══════════════════════════════════════════════════════════════════════════
# DB persistence
# ═══════════════════════════════════════════════════════════════════════════
def persist(session, meta: dict, extraction: dict, source_pdf: Path,
              source_url: str, dry_run: bool) -> dict:
    """Write extraction to gpi_cag_extractions + relevant gpi_indicator_values."""
    from app.gpi_models import GpiCagExtraction, GpiIndicator, GpiIndicatorValue
    from app.models import State

    state_name = extraction.get("state_name") or meta.get("state")
    audit_year = extraction.get("audit_year") or meta.get("audit_year")
    if not state_name or not audit_year:
        return {"error": f"Missing state/year (state={state_name!r}, year={audit_year!r})"}

    st = session.query(State).filter_by(name=state_name).one_or_none()
    if not st:
        return {"error": f"Unknown state: {state_name}"}

    # Fiscal year integer (audit year END, e.g. 2022-23 → 2023)
    fy_end = None
    if audit_year and "-" in audit_year:
        try:
            fy_end = int(audit_year.split("-")[0]) + 1
        except ValueError:
            pass

    fi = extraction.get("fiscal_indicators") or {}
    ab = extraction.get("absolute_figures") or {}
    ap = extraction.get("audit_paras") or {}

    # Sign-convention normalization. CAG SFAR tables sometimes show deficit
    # as (receipts − expenditure), which comes out negative when there IS a
    # deficit. RBI (and our GPI spec) uses positive values for deficit magnitude.
    # abs() the deficit fields to align conventions across sources.
    for key in ("gross_fiscal_deficit_pct_gsdp",
                 "revenue_deficit_pct_gsdp",
                 "primary_deficit_pct_gsdp"):
        val = fi.get(key)
        if val is not None:
            fi[key] = abs(val)

    # Sanity checks. Zero values for indicators that MUST be positive
    # (every Indian state has some interest burden, some debt, etc.) are
    # almost certainly Gemini extraction failures where it couldn't find
    # the table. Null them out so they don't pollute the pillar score.
    SANITY_MIN = {
        "interest_pct_revenue_receipts": 3.0,   # even lowest states >3%
        "debt_pct_gsdp":                 5.0,   # every state has meaningful debt
    }
    for key, min_val in SANITY_MIN.items():
        val = fi.get(key)
        if val is not None and val < min_val:
            fi[key] = None

    report_no = extraction.get("report_number", "") or meta.get("report_no", "")

    # Upsert into gpi_cag_extractions
    existing = session.query(GpiCagExtraction).filter_by(
        state_id=st.id, audit_year=audit_year, report_no=report_no,
    ).one_or_none()

    payload = {
        "state_id":         st.id,
        "audit_year":       audit_year,
        "report_no":        report_no,
        "publication_year": int(meta["publication_year"]) if meta.get("publication_year") else None,
        "fiscal_deficit_pct_gsdp":       fi.get("gross_fiscal_deficit_pct_gsdp"),
        "revenue_deficit_pct_gsdp":      fi.get("revenue_deficit_pct_gsdp"),
        "primary_deficit_pct_gsdp":      fi.get("primary_deficit_pct_gsdp"),
        "debt_pct_gsdp":                 fi.get("debt_pct_gsdp"),
        "interest_pct_revenue_receipts": fi.get("interest_pct_revenue_receipts"),
        "capital_outlay_pct_gsdp":       fi.get("capital_outlay_pct_gsdp"),
        "revenue_receipts_pct_gsdp":     fi.get("revenue_receipts_pct_gsdp"),
        "own_tax_revenue_pct_gsdp":      fi.get("own_tax_revenue_pct_gsdp"),
        "gsdp_current_crore":            ab.get("gsdp_current_crore"),
        "outstanding_debt_crore":        ab.get("outstanding_debt_crore"),
        "audit_paras_raised":            ap.get("raised_this_year"),
        "audit_paras_over_5_yrs":        ap.get("outstanding_over_5_years"),
        "pac_recommendations_pending":   ap.get("pac_recommendations_pending"),
        "money_value_observations_crore": ap.get("money_value_of_observations_crore"),
        "source_pdf":                    str(source_pdf.relative_to(ROOT)),
        "source_url":                    source_url,
        "extraction_confidence":         extraction.get("extraction_confidence"),
        "extraction_notes":              extraction.get("extraction_notes"),
        "extracted_at":                  datetime.utcnow(),
        "gemini_raw_response":           json.dumps(extraction, ensure_ascii=False),
    }

    if existing:
        for k, v in payload.items():
            setattr(existing, k, v)
    else:
        session.add(GpiCagExtraction(**payload))

    # Also write F02 (debt/GSDP) + F06 (interest/rev receipts) as indicator values
    #   These are the two the scoring engine actually uses.
    indicators_by_code = {
        i.code: i for i in session.query(GpiIndicator).filter(
            GpiIndicator.code.in_(["F02", "F06"])
        ).all()
    }

    indicator_writes = 0
    for code, value in [
        ("F02", fi.get("debt_pct_gsdp")),
        ("F06", fi.get("interest_pct_revenue_receipts")),
    ]:
        if value is None or fy_end is None:
            continue
        ind = indicators_by_code.get(code)
        if not ind:
            continue

        iv = session.query(GpiIndicatorValue).filter_by(
            indicator_id=ind.id, state_id=st.id, fiscal_year=fy_end,
        ).one_or_none()

        common = {
            "raw_value":         value,
            "source_url":        source_url,
            "source_document":   f"CAG SFAR {audit_year} · {report_no}",
            "extraction_method": "llm_extracted",
            "staleness":         "current",
            "notes":             f"Gemini extraction from CAG SFAR PDF; "
                                   f"confidence: {extraction.get('extraction_confidence', 'medium')}",
            "extracted_at":      datetime.utcnow(),
        }
        if iv:
            for k, v in common.items():
                setattr(iv, k, v)
            iv.normalized_value = None
            iv.national_rank = None
        else:
            session.add(GpiIndicatorValue(
                indicator_id=ind.id, state_id=st.id, fiscal_year=fy_end,
                **common,
            ))
        indicator_writes += 1

    if not dry_run:
        session.commit()

    # Cross-validate F01, F05 against RBI-sourced values already in DB
    warnings = []
    for code, cag_value in [
        ("F01", fi.get("gross_fiscal_deficit_pct_gsdp")),
        ("F05", fi.get("revenue_deficit_pct_gsdp")),
    ]:
        if cag_value is None or fy_end is None:
            continue
        ind = session.query(GpiIndicator).filter_by(code=code).one_or_none()
        if not ind:
            continue
        iv = session.query(GpiIndicatorValue).filter_by(
            indicator_id=ind.id, state_id=st.id, fiscal_year=fy_end,
        ).one_or_none()
        if not iv or iv.raw_value is None:
            continue
        rbi_val = iv.raw_value
        if abs(rbi_val) < 0.01:
            continue
        diff_pct = abs(cag_value - rbi_val) / abs(rbi_val) * 100
        if diff_pct > 10:
            warnings.append(
                f"{code}: CAG={cag_value:.2f}%, RBI={rbi_val:.2f}% (diff {diff_pct:.1f}%)"
            )

    return {
        "indicator_writes": indicator_writes,
        "cross_check_warnings": warnings,
    }


# ═══════════════════════════════════════════════════════════════════════════
# Main
# ═══════════════════════════════════════════════════════════════════════════
def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                  formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--state", help="Only process rows for this state")
    ap.add_argument("--force", action="store_true",
                    help="Re-extract even if already extracted")
    ap.add_argument("--dry-run", action="store_true",
                    help="Call Gemini + parse, but don't write to DB")
    ap.add_argument("--limit", type=int, default=0,
                    help="Cap at this many PDFs (0 = no limit)")
    ap.add_argument("--only-sfar", action="store_true", default=True,
                    help="Skip Revenue-only reports (default). "
                          "SFARs have the full fiscal-indicators summary; "
                          "revenue reports only have tax/revenue data.")
    args = ap.parse_args()

    if not DOWNLOADED_CSV.exists():
        raise SystemExit(f"Downloaded manifest not found: {DOWNLOADED_CSV.relative_to(ROOT)}")

    with DOWNLOADED_CSV.open("r", encoding="utf-8") as f:
        all_rows = list(csv.DictReader(f))

    # Filter to SFARs (title contains "State Finances", not just "State Revenues")
    rows = []
    for r in all_rows:
        if args.state and r["state"] != args.state:
            continue
        title = r.get("title", "").lower()
        if args.only_sfar and "state finance" not in title:
            continue
        rows.append(r)

    if args.limit and len(rows) > args.limit:
        rows = rows[:args.limit]

    print(f"CAG SFAR extractor")
    print(f"Downloaded manifest: {len(all_rows)} rows")
    print(f"Matched SFARs:       {len(rows)}")
    print(f"Model:               {MODEL_NAME}")
    print()

    RAW_JSON_DIR.mkdir(parents=True, exist_ok=True)

    from app.database import SessionLocal
    from app import models  # ensures gpi_models loaded
    from app.gpi_models import GpiCagExtraction

    session = SessionLocal()

    counts = {"extracted": 0, "skipped": 0, "failed": 0}
    warnings_all = []

    try:
        for i, row in enumerate(rows, 1):
            local_path = ROOT / row["local_path"]
            if not local_path.exists():
                print(f"[{i:>2d}/{len(rows)}] MISSING: {local_path}")
                counts["failed"] += 1
                continue

            print(f"[{i:>2d}/{len(rows)}] {row['state']:<10s} {row['audit_year']:<10s} "
                  f"{local_path.name[:60]}")

            # Skip if raw JSON already exists (unless --force)
            raw_json_path = RAW_JSON_DIR / (local_path.stem + ".gemini.json")
            if raw_json_path.exists() and not args.force:
                # Load previously-extracted result
                extraction = json.loads(raw_json_path.read_text(encoding="utf-8"))
                print(f"           ✓ using cached extraction ({raw_json_path.name})")
                counts["skipped"] += 1
            else:
                try:
                    t0 = time.time()
                    extraction = call_gemini(local_path)
                    dt = time.time() - t0
                    raw_json_path.write_text(json.dumps(extraction, indent=2, ensure_ascii=False))
                    print(f"           ✓ extracted in {dt:.1f}s  → {raw_json_path.name}")
                    counts["extracted"] += 1
                except Exception as e:
                    print(f"           ✗ Gemini error: {type(e).__name__}: {e}")
                    counts["failed"] += 1
                    continue

            # Persist to DB
            meta = {
                "state":            row["state"],
                "audit_year":       row.get("audit_year", ""),
                "publication_year": row.get("publication_date", "").split()[-1] if row.get("publication_date") else "",
                "report_no":        "",  # let Gemini's report_number field win
            }
            result = persist(session, meta, extraction, local_path,
                              row["pdf_url"], args.dry_run)
            if "error" in result:
                print(f"           ⚠ persist error: {result['error']}")
                continue

            fi = extraction.get("fiscal_indicators") or {}
            print(f"             F02 (debt/GSDP)  = {fi.get('debt_pct_gsdp')}")
            print(f"             F06 (int/rev)    = {fi.get('interest_pct_revenue_receipts')}")
            print(f"             fiscal deficit   = {fi.get('gross_fiscal_deficit_pct_gsdp')}")
            print(f"             revenue deficit  = {fi.get('revenue_deficit_pct_gsdp')}")

            for w in result.get("cross_check_warnings", []):
                warnings_all.append((row["state"], row["audit_year"], w))
                print(f"             ⚠ {w}")

    finally:
        session.close()

    print()
    print("═══════════════════════ Summary ═══════════════════════")
    print(f"  Extracted (this run): {counts['extracted']}")
    print(f"  Skipped (cached):     {counts['skipped']}")
    print(f"  Failed:               {counts['failed']}")
    if warnings_all:
        print(f"\n  Cross-check warnings: {len(warnings_all)}")
        for state, yr, w in warnings_all:
            print(f"    {state} FY{yr}: {w}")
    if args.dry_run:
        print("\n  (dry run — no DB writes)")
    print()
    print("Next: python scripts/gpi_compute_scores.py")


if __name__ == "__main__":
    main()
