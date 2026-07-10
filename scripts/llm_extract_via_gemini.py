"""
Production Gemini-based LLM extraction over all preprocessed Form 26 affidavits.

Reads the per-affidavit JSONs produced by cloud_vision_preprocess.py
(Cloud Vision OCR output), runs structured extraction via Gemini 2.5
Flash, and writes one JSON of extracted fields per candidate to
`data/eci/for_ai/llm_extracted/{state}_{year}/`. Subsequent ingestion
into the canonical DB tables is handled by `apply_llm_extraction.py`.

DESIGN
======
1. **Same prompt + schema as the A/B test** — proven via Kejriwal,
   Sahiram, Hammad Khan. Anneuxre-fallback, name-deduplication, and
   dependent-privacy rules baked into the prompt. Detail arrays
   (vehicles, properties, cases, loans) captured.

2. **Parallel via ThreadPoolExecutor** — 20 workers default. Gemini
   sync API allows ~1,000 RPM for paid tier; at ~30-60 sec/affidavit
   we run at 20-40 RPM, well inside the limit.

3. **Resume support** — if the output JSON already exists for a
   candidate, skip. Re-running picks up only the missing ones.

4. **Cost tracking** — accumulates per-call cost based on Gemini 2.5
   Flash pricing ($0.30 / 1M input, $2.50 / 1M output). Prints
   running total after each call and the final tally at the end.

5. **Per-candidate isolation** — one bad affidavit (timeout, content
   filter, unparseable response) does not kill the run. The failure
   is logged and the script continues.

USAGE
=====
  pip install google-cloud-aiplatform

  # Delhi 2020 (1,016 affidavits)
  python scripts/llm_extract_via_gemini.py \\
    --in-dir data/eci/for_ai/preprocessed_delhi_2020 \\
    --out-dir data/eci/for_ai/llm_extracted/delhi_2020 \\
    --state Delhi --year 2020

  # Delhi 2025 (668 affidavits)
  python scripts/llm_extract_via_gemini.py \\
    --in-dir data/eci/for_ai/preprocessed \\
    --out-dir data/eci/for_ai/llm_extracted/delhi_2025 \\
    --state Delhi --year 2025

  # Smoke test on 5 first
  python scripts/llm_extract_via_gemini.py --limit 5 ...

ENV
===
  GOOGLE_APPLICATION_CREDENTIALS — same service-account JSON used
  for Cloud Vision (the lokvani-500314 project).
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

GCP_PROJECT = "lokvani-501706"
GCP_LOCATION = "us-central1"
MODEL_NAME = "gemini-2.5-flash"

# Gemini 2.5 Flash pricing (USD per token) — verify in console for current rate.
COST_INPUT_PER_TOKEN  = 0.30 / 1_000_000
COST_OUTPUT_PER_TOKEN = 2.50 / 1_000_000


# ---------------------------------------------------------------------------
# Prompt — same content the A/B test proved out
# ---------------------------------------------------------------------------

EXTRACTION_PROMPT = """\
You are extracting structured data from an Indian Election Commission
Form 26 candidate affidavit. The OCR text below was extracted from a
multi-page PDF; page breaks are marked with `=== PAGE N ===`.

Return a JSON object matching the response schema. Use `null` for any
field the affidavit does NOT disclose. Monetary values are plain
integers in Indian Rupees (no "₹", no commas, no decimals — convert
"Rs. 12,34,567/-" to the integer 1234567). Dates use ISO format
YYYY-MM-DD.

PER-DECLARANT TOTALS — ANNEXURE FALLBACK
========================================
The PART B "ABSTRACT" page (usually near the end) contains canonical
totals. HOWEVER, if the abstract states "NOT APPLICABLE", "NIL", "0",
or is blank for any per-declarant total (movable_self_inr,
movable_spouse_inr, immovable_self_inr, etc.), do NOT use that value.
Instead, look at the corresponding PART A detail tables — typically
titled "Annexure B" (movable assets self), "Annexure C-1" (immovable
self), "Annexure C-2/D-1" (spouse), etc. — and SUM the per-asset
values listed there for that declarant. Use that sum as the
per-declarant total.

The grand total `_inr` field is always the sum of self + spouse +
huf + dependents, regardless of source. If the affidavit's stated
grand total disagrees with your sum, prefer your sum and add a note.

NAME HANDLING — AVOID DUPLICATES
================================
Use the candidate's name as it appears in the IDENTITY / candidate-
declaration section of the form. Ignore signature blocks, verification
clauses, and stamp imprints. If a surname appears consecutively
duplicated in the OCR (e.g. "HAMMAD KHAN KHAN", "SHARMA SHARMA"),
use only ONE occurrence — the OCR pipeline sometimes captures the
same name twice from overlapping text regions.

PRIVACY — DEPENDENT LABELS, NOT NAMES
=====================================
For each dependent (children, parents, other family disclosed in the
affidavit), use the labels "dependent_1", "dependent_2", "dependent_3"
in the order they appear. DO NOT include the dependent's actual name
in any field. Capture:
  - `relationship` (e.g. "son", "daughter", "father", "mother")
  - `age_band` ("minor" if under 18, otherwise "adult")
  - Their per-declarant financial totals

Same rule for properties: use city/district names for `location_city`
and `location_district`, but DO NOT capture street addresses, plot
numbers, or specific survey numbers.

CONFIDENCE
==========
If you are uncertain about a value (OCR garbled, ambiguous source,
needs human judgment), set the value to null AND add a note in
`extraction_metadata.extraction_notes`. Do NOT guess. Add the field
name to `extraction_metadata.fields_low_confidence` so we can review
it later.

DETAIL ARRAYS
=============
The schema includes detail arrays (vehicles, properties, cases,
liabilities, dependents). Populate them from PART A tables. Use
empty array `[]` (not null) when the affidavit explicitly lists
"NIL" or "NONE" for a section. Use null only when the section is
missing or illegible.

---
AFFIDAVIT OCR TEXT:
"""


# ---------------------------------------------------------------------------
# Response schema — Vertex AI OpenAPI subset (string types, nullable flag)
# ---------------------------------------------------------------------------

def _s(nullable: bool = True) -> dict:
    return {"type": "string", "nullable": nullable}

def _i(nullable: bool = True) -> dict:
    return {"type": "integer", "nullable": nullable}

def _b(nullable: bool = True) -> dict:
    return {"type": "boolean", "nullable": nullable}


RESPONSE_SCHEMA = {
    "type": "object",
    "properties": {
        "identity": {
            "type": "object",
            "properties": {
                "name_in_english":         _s(),
                "father_or_husband_name":  _s(),
                "relationship":            _s(),
                "age_years":               _i(),
                "gender":                  _s(),
                "date_of_birth":           _s(),
                "marital_status":          _s(),
            },
        },
        "contact": {
            "type": "object",
            "properties": {
                "permanent_address_pin":   _s(),
                "phone":                   _s(),
                "email":                   _s(),
            },
        },
        "political": {
            "type": "object",
            "properties": {
                "party_name":              _s(),
                "constituency_name":       _s(),
                "is_independent":          _b(),
            },
        },
        "tax": {
            "type": "object",
            "properties": {
                "self_pan_declared":          _b(),
                "spouse_pan_declared":        _b(),
                "total_income_last_year_inr": _i(),
            },
        },
        "assets_movable": {
            "type": "object",
            "properties": {
                "total_movable_assets_self_inr":       _i(),
                "total_movable_assets_spouse_inr":     _i(),
                "total_movable_assets_huf_inr":        _i(),
                "total_movable_assets_dependents_inr": _i(),
                "total_movable_assets_inr":            _i(),
            },
        },
        "vehicles_detail": {
            "type": "array",
            "nullable": True,
            "items": {
                "type": "object",
                "properties": {
                    "declarant":         _s(nullable=False),
                    "description":       _s(),
                    "registration_year": _i(),
                    "value_inr":         _i(),
                },
            },
        },
        "assets_immovable": {
            "type": "object",
            "properties": {
                "total_immovable_assets_self_inr":       _i(),
                "total_immovable_assets_spouse_inr":     _i(),
                "total_immovable_assets_huf_inr":        _i(),
                "total_immovable_assets_dependents_inr": _i(),
                "total_immovable_assets_inr":            _i(),
            },
        },
        "immovable_detail": {
            "type": "array",
            "nullable": True,
            "items": {
                "type": "object",
                "properties": {
                    "declarant":                  _s(nullable=False),
                    "property_type":              _s(),
                    "location_city":              _s(),
                    "location_district":          _s(),
                    "area_description":           _s(),
                    "value_inr":                  _i(),
                    "acquisition_year":           _i(),
                    "ancestral_or_self_acquired": _s(),
                },
            },
        },
        "liabilities": {
            "type": "object",
            "properties": {
                "total_liabilities_self_inr":       _i(),
                "total_liabilities_spouse_inr":     _i(),
                "total_liabilities_huf_inr":        _i(),
                "total_liabilities_dependents_inr": _i(),
                "total_liabilities_inr":            _i(),
            },
        },
        "liabilities_detail": {
            "type": "array",
            "nullable": True,
            "items": {
                "type": "object",
                "properties": {
                    "declarant":      _s(nullable=False),
                    "liability_type": _s(),
                    "lender":         _s(),
                    "amount_inr":     _i(),
                    "purpose":        _s(),
                },
            },
        },
        "criminal_pending": {
            "type": "object",
            "properties": {
                "pending_cases_count": _i(nullable=False),
                "has_charges_framed":  _b(),
            },
        },
        "pending_cases_detail": {
            "type": "array",
            "nullable": True,
            "items": {
                "type": "object",
                "properties": {
                    "court_name":     _s(),
                    "case_number":    _s(),
                    "fir_number":     _s(),
                    "ipc_sections":   {"type": "array", "nullable": True,
                                        "items": {"type": "string"}},
                    "other_acts":     {"type": "array", "nullable": True,
                                        "items": {"type": "string"}},
                    "charges_framed": _b(),
                    "current_status": _s(),
                },
            },
        },
        "criminal_past": {
            "type": "object",
            "properties": {
                "convicted_cases_count": _i(nullable=False),
                "acquitted_cases_count": _i(),
            },
        },
        "convicted_cases_detail": {
            "type": "array",
            "nullable": True,
            "items": {
                "type": "object",
                "properties": {
                    "court_name":   _s(),
                    "case_number":  _s(),
                    "ipc_sections": {"type": "array", "nullable": True,
                                       "items": {"type": "string"}},
                    "sentence":     _s(),
                },
            },
        },
        "dependents": {
            "type": "array",
            "nullable": True,
            "items": {
                "type": "object",
                "properties": {
                    "label":                      _s(nullable=False),
                    "relationship":               _s(),
                    "age_band":                   _s(),
                    "total_movable_assets_inr":   _i(),
                    "total_immovable_assets_inr": _i(),
                    "total_liabilities_inr":      _i(),
                },
            },
        },
        "education": {
            "type": "object",
            "properties": {
                "highest_qualification": _s(),
            },
        },
        "profession": {
            "type": "object",
            "properties": {
                "profession_self":      _s(),
                "profession_spouse":    _s(),
                "source_of_livelihood": _s(),
            },
        },
        "documentation": {
            "type": "object",
            "properties": {
                "estamp_certificate_number": _s(),
                "estamp_issue_date":         _s(),
                "affidavit_signed_date":     _s(),
                "affidavit_signed_place":    _s(),
            },
        },
        "extraction_metadata": {
            "type": "object",
            "properties": {
                "extraction_notes":      {"type": "array",
                                            "items": {"type": "string"}},
                "fields_low_confidence": {"type": "array",
                                            "items": {"type": "string"}},
            },
        },
    },
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def load_preprocessed_text(json_path: Path) -> tuple[str, dict]:
    """Read a preprocessed JSON and return (page-marked OCR text, metadata)."""
    data = json.loads(json_path.read_text())
    pages = data.get("pages", [])
    parts = []
    for p in pages:
        parts.append(f"=== PAGE {p['page']} ===")
        parts.append(p.get("text", ""))
    return "\n".join(parts), {
        "source_pdf":  data.get("source_pdf"),
        "page_count":  data.get("page_count"),
        "stats":       data.get("stats", {}),
        "corrupt":     data.get("corrupt", False),
    }


def call_gemini(prompt: str, client):
    """One Gemini call. Returns (response_text, usage_dict, finish_reason).

    Uses the google-genai SDK (not the removed vertexai.generative_models).
    """
    from google.genai import types
    resp = client.models.generate_content(
        model=MODEL_NAME,
        contents=prompt,
        config=types.GenerateContentConfig(
            temperature=0.1,
            response_mime_type="application/json",
            response_schema=RESPONSE_SCHEMA,
            max_output_tokens=32768,
        ),
    )
    usage = {}
    if hasattr(resp, "usage_metadata") and resp.usage_metadata:
        um = resp.usage_metadata
        usage = {
            "prompt_token_count":     getattr(um, "prompt_token_count", 0) or 0,
            "candidates_token_count": getattr(um, "candidates_token_count", 0) or 0,
            "total_token_count":      getattr(um, "total_token_count", 0) or 0,
        }
    finish_reason = "UNKNOWN"
    try:
        finish_reason = str(resp.candidates[0].finish_reason)
    except Exception:
        pass
    return resp.text, usage, finish_reason


def estimate_cost(usage: dict) -> float:
    return (usage.get("prompt_token_count", 0)     * COST_INPUT_PER_TOKEN +
            usage.get("candidates_token_count", 0) * COST_OUTPUT_PER_TOKEN)


def extract_affidavit_id(filename: str) -> str | None:
    """From e.g. '061_ARVIND_KEJRIWAL__1057.json' or 'SAHIRAM__1515.json',
    return '1057' / '1515'. Matches the trailing __NNN.json pattern."""
    m = re.search(r"__(\d+)\.json$", filename)
    return m.group(1) if m else None


def process_one(pdf_json_path: Path, out_dir: Path, state: str, year: int,
                  vision_client) -> dict:
    """Extract one affidavit. Writes per-candidate JSON. Returns status dict."""
    out_path = out_dir / pdf_json_path.name
    aff_id = extract_affidavit_id(pdf_json_path.name)

    text, meta = load_preprocessed_text(pdf_json_path)
    if meta.get("corrupt"):
        # Bypass — bad source PDF, nothing to extract
        result = {
            "label":            pdf_json_path.stem,
            "state":            state,
            "election_year":    year,
            "affidavit_id":     aff_id,
            "source_pdf":       meta.get("source_pdf"),
            "model":            MODEL_NAME,
            "extraction":       None,
            "cost_usd":         0,
            "elapsed_seconds":  0,
            "skipped_corrupt":  True,
        }
        out_path.write_text(json.dumps(result, indent=2, ensure_ascii=False))
        return {"status": "corrupt", "name": pdf_json_path.name,
                "cost": 0, "elapsed": 0}

    prompt = EXTRACTION_PROMPT + "\n" + text
    t0 = time.time()
    try:
        resp_text, usage, finish_reason = call_gemini(prompt, vision_client)
    except Exception as e:
        return {"status": "error", "name": pdf_json_path.name,
                "error": f"{type(e).__name__}: {str(e)[:200]}",
                "cost": 0, "elapsed": time.time() - t0}
    elapsed = time.time() - t0
    cost = estimate_cost(usage)

    try:
        extraction = json.loads(resp_text)
    except json.JSONDecodeError as e:
        extraction = {"_raw": resp_text, "_parse_error": str(e)}

    result = {
        "label":            pdf_json_path.stem,
        "state":            state,
        "election_year":    year,
        "affidavit_id":     aff_id,
        "source_pdf":       meta.get("source_pdf"),
        "model":            MODEL_NAME,
        "extraction":       extraction,
        "usage":            usage,
        "cost_usd":         round(cost, 6),
        "elapsed_seconds":  round(elapsed, 1),
        "finish_reason":    finish_reason,
    }
    out_path.write_text(json.dumps(result, indent=2, ensure_ascii=False))
    return {"status": "ok", "name": pdf_json_path.name,
            "cost": cost, "elapsed": elapsed,
            "in_tokens":  usage.get("prompt_token_count", 0),
            "out_tokens": usage.get("candidates_token_count", 0)}


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    if not os.environ.get("GOOGLE_APPLICATION_CREDENTIALS"):
        sys.exit(
            "GOOGLE_APPLICATION_CREDENTIALS not set. Add to ~/.zshrc:\n"
            '   export GOOGLE_APPLICATION_CREDENTIALS="$HOME/.gcp/lokvani-vision-key.json"'
        )

    try:
        from google import genai  # noqa: F401
    except ImportError:
        sys.exit("pip install google-genai\n"
                 "(google-genai replaces the deprecated "
                 "google-cloud-aiplatform vertexai.generative_models SDK)")

    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument("--in-dir",  required=True,
                    help="Directory of preprocessed Cloud-Vision JSONs.")
    ap.add_argument("--out-dir", required=True,
                    help="Directory where per-candidate LLM JSONs go.")
    ap.add_argument("--state",   required=True,
                    help="State name (Delhi, etc.). Recorded in output.")
    ap.add_argument("--year",    type=int, required=True,
                    help="Election year (2020, 2025, ...). Recorded in output.")
    ap.add_argument("--workers", type=int, default=20,
                    help="Parallel Gemini calls. Default 20. "
                         "Gemini paid tier allows ~1000 RPM.")
    ap.add_argument("--limit",   type=int, default=0,
                    help="Process at most N candidates (smoke test).")
    ap.add_argument("--refresh", action="store_true",
                    help="Re-extract even if output JSON already exists.")
    args = ap.parse_args()

    # Normalize --state to TitleCase so LLM JSON's state field matches
    # canonical states.name (loader convention). Downstream
    # apply_llm_extraction reads this field and joins on states.name.
    if args.state:
        _SPECIAL = {
            "jammu and kashmir": "Jammu and Kashmir",
            "andhra pradesh":    "Andhra Pradesh",
            "arunachal pradesh": "Arunachal Pradesh",
            "himachal pradesh":  "Himachal Pradesh",
            "madhya pradesh":    "Madhya Pradesh",
            "tamil nadu":        "Tamil Nadu",
            "uttar pradesh":     "Uttar Pradesh",
            "west bengal":       "West Bengal",
            "jk":                "Jammu and Kashmir",
        }
        lc = args.state.strip().lower()
        args.state = _SPECIAL.get(lc, args.state.strip().title())

    project_root = Path(__file__).resolve().parent.parent
    in_dir  = Path(args.in_dir)
    out_dir = Path(args.out_dir)
    if not in_dir.is_absolute():
        in_dir = project_root / in_dir
    if not out_dir.is_absolute():
        out_dir = project_root / out_dir
    if not in_dir.exists():
        sys.exit(f"--in-dir not found: {in_dir}")
    out_dir.mkdir(parents=True, exist_ok=True)

    # Collect target JSONs
    inputs = sorted(p for p in in_dir.glob("*.json")
                     if not p.name.startswith("_"))
    if not inputs:
        sys.exit(f"No *.json files in {in_dir}")

    # Resume — skip already-done
    todo = []
    for p in inputs:
        out_path = out_dir / p.name
        if out_path.exists() and not args.refresh:
            continue
        todo.append(p)

    if args.limit:
        todo = todo[: args.limit]

    if not todo:
        print("Everything already extracted — nothing to do.", file=sys.stderr)
        return

    print(f"Input dir:  {in_dir}", file=sys.stderr)
    print(f"Output dir: {out_dir}", file=sys.stderr)
    print(f"State / year: {args.state} / {args.year}", file=sys.stderr)
    print(f"Model: {MODEL_NAME}", file=sys.stderr)
    print(f"Workers: {args.workers}", file=sys.stderr)
    print(f"Processing {len(todo)} of {len(inputs)} preprocessed JSONs "
          f"({len(inputs)-len(todo)} cached) ...", file=sys.stderr)

    # New google-genai SDK client — replaces vertexai.init + GenerativeModel.
    # Points at Vertex AI by passing vertexai=True (as opposed to the
    # standalone Gemini API).
    from google import genai
    client = genai.Client(
        vertexai=True,
        project=GCP_PROJECT,
        location=GCP_LOCATION,
    )

    t_start = time.time()
    succeeded = corrupt_n = failed = 0
    total_cost = 0.0
    total_in_tokens = total_out_tokens = 0

    with ThreadPoolExecutor(max_workers=args.workers) as executor:
        futures = {
            executor.submit(process_one, p, out_dir, args.state, args.year,
                              client): p
            for p in todo
        }
        for i, fut in enumerate(as_completed(futures), 1):
            p = futures[fut]
            try:
                r = fut.result()
            except Exception as e:
                failed += 1
                print(f"  ✗ [{i:4d}/{len(todo)}] {p.name}: "
                      f"{type(e).__name__}: {e}", file=sys.stderr)
                continue

            if r["status"] == "corrupt":
                corrupt_n += 1
                print(f"  ⚠ [{i:4d}/{len(todo)}] {p.name}: corrupt source",
                      file=sys.stderr)
            elif r["status"] == "error":
                failed += 1
                print(f"  ✗ [{i:4d}/{len(todo)}] {p.name}: {r.get('error')}",
                      file=sys.stderr)
            else:
                succeeded += 1
                total_cost += r["cost"]
                total_in_tokens  += r.get("in_tokens", 0)
                total_out_tokens += r.get("out_tokens", 0)
                print(f"  ✓ [{i:4d}/{len(todo)}] {p.name}  "
                      f"({r['elapsed']:5.1f}s, "
                      f"in={r.get('in_tokens', 0):,}, "
                      f"out={r.get('out_tokens', 0):,}, "
                      f"${r['cost']:.4f}, "
                      f"running=${total_cost:.2f})", file=sys.stderr)

    elapsed_total = time.time() - t_start

    print(f"\n========== GEMINI EXTRACTION SUMMARY ==========", file=sys.stderr)
    print(f"  Succeeded:            {succeeded}", file=sys.stderr)
    print(f"  Corrupt (skipped):    {corrupt_n}", file=sys.stderr)
    print(f"  Failed (transient):   {failed}", file=sys.stderr)
    print(f"  Total input tokens:   {total_in_tokens:,}", file=sys.stderr)
    print(f"  Total output tokens:  {total_out_tokens:,}", file=sys.stderr)
    print(f"  Total cost:           ${total_cost:.2f}", file=sys.stderr)
    print(f"  Wall time:            {elapsed_total/60:.1f}m", file=sys.stderr)
    print(f"  Outputs in: {out_dir}", file=sys.stderr)


if __name__ == "__main__":
    main()
