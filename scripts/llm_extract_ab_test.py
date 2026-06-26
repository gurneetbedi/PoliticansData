"""
A/B test for Gemini-based comprehensive Form 26 affidavit extraction.

Picks 3 candidates with known characteristics, runs ONE comprehensive
Gemini call per affidavit (extracting every Form-26 field, not just the
six we'll display), and writes the full JSON output to disk for review.

WHY ONE-SHOT EXTRACTION
=======================
Coming back later to extract additional fields would mean paying for
the LLM call again. Better to extract everything once into a stable
JSON file, then choose what to publish from that. The JSON becomes our
"unpolished but complete" source; the canonical DB tables are the
"polished, displayed" subset.

TEST CASES
==========
1. ARVIND KEJRIWAL (NEW DELHI, 2025) — high-profile, results we can
   sanity-check against public reports.
2. SAHIRAM (TUGHLAKABAD, 2020) — sitting AAP MLA at the time, multiple
   filings (we pick affidavit_id=1515, the highest). Tests OCR-quality
   on a 2020 scanned PDF.
3. HAMMAD KHAN (2025, affidavit_id=484) — the candidate the OLD regex
   extractor falsely reported with declared assets of ₹17.4 Crore.
   Critical validation: does Gemini get the right number?

OUTPUT
======
For each candidate writes:
  data/eci/for_ai/llm_extracted/_ab_test_<name>.json
        — the full Gemini response, structured

Plus prints a side-by-side comparison table to stderr so you can
eyeball the extraction quality before scaling to 1,684 affidavits.

USAGE
=====
    pip install google-cloud-aiplatform
    # Vertex AI API must be enabled in your GCP project (lokvani-500314).
    # Uses the same service-account creds as Cloud Vision
    # (GOOGLE_APPLICATION_CREDENTIALS env var).

    python scripts/llm_extract_ab_test.py
"""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path


# ---------------------------------------------------------------------------
# Configuration — the 3 test candidates
# ---------------------------------------------------------------------------

PROJECT_ROOT = Path(__file__).resolve().parent.parent

# (label, year, preprocessed-json-path-relative-to-project-root)
TEST_CANDIDATES = [
    ("ARVIND_KEJRIWAL_2025",
     2025,
     "data/eci/for_ai/preprocessed/061_ARVIND_KEJRIWAL__1057.json"),
    ("SAHIRAM_2020",
     2020,
     "data/eci/for_ai/preprocessed_delhi_2020/SAHIRAM__1515.json"),
    ("HAMMAD_KHAN_2025",
     2025,
     "data/eci/for_ai/preprocessed/189_HAMMAD_KHAN__484.json"),
]

OUT_DIR = PROJECT_ROOT / "data/eci/for_ai/llm_extracted"

# Use lokvani-500314 (the GCP project the user set up earlier).
# us-central1 is the default Vertex AI region and supports Gemini 2.5 Flash.
GCP_PROJECT = "lokvani-500314"
GCP_LOCATION = "us-central1"
MODEL_NAME = "gemini-2.5-flash"


# ---------------------------------------------------------------------------
# Comprehensive Form 26 schema (the prompt's target structure)
# ---------------------------------------------------------------------------

# We send this schema description to Gemini as part of the prompt. The
# response should be JSON matching it. Fields are nullable; the model
# uses `null` when a value isn't disclosed in the affidavit.
#
# We deliberately ask for EVERY field documented in Form 26 (per the
# Conduct of Election Rules 1961, Form 26 schedule), even fields we
# may not display on the site. Storing the full extraction means we
# never have to re-call Gemini just to add a field later.

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
# Response schema — Gemini's structured-output JSON Schema
# ---------------------------------------------------------------------------
#
# Gemini validates the response against this schema before returning, so
# we get guaranteed-parseable JSON. We use a SIMPLIFIED schema for this
# A/B test: per-declarant TOTALS only (no nested per-vehicle, per-case
# detail arrays). The detail arrays balloon the output and trip the
# model up; we can add them in a separate "detail extraction" pass
# later if we decide they're worth the extra cost.

# Vertex AI uses OpenAPI 3.0 schema (subset). Nullable fields use a
# separate `nullable: True` flag instead of `["string", "null"]`. Types
# are case-insensitive but the SDK normalizes via `.upper()`, which
# breaks when you pass a list — so keep them as single strings.

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
                "self_pan_declared":       _b(),
                "spouse_pan_declared":     _b(),
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
            "description": "Vehicles disclosed in the affidavit. Empty array [] if affidavit states NIL.",
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
            "description": "Per-property details. Use city/district level only; do NOT capture street addresses or plot numbers.",
            "items": {
                "type": "object",
                "properties": {
                    "declarant":                     _s(nullable=False),
                    "property_type":                 _s(),
                    "location_city":                 _s(),
                    "location_district":             _s(),
                    "area_description":              _s(),
                    "value_inr":                     _i(),
                    "acquisition_year":              _i(),
                    "ancestral_or_self_acquired":    _s(),
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
            "description": "Per-loan/liability details. Empty array if affidavit states NIL.",
            "items": {
                "type": "object",
                "properties": {
                    "declarant":   _s(nullable=False),
                    "liability_type": _s(),
                    "lender":      _s(),
                    "amount_inr":  _i(),
                    "purpose":     _s(),
                },
            },
        },
        "criminal_pending": {
            "type": "object",
            "properties": {
                "pending_cases_count":  _i(nullable=False),
                "has_charges_framed":   _b(),
            },
        },
        "pending_cases_detail": {
            "type": "array",
            "nullable": True,
            "description": "Per-case detail for pending criminal cases. Empty array if affidavit states NIL.",
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
                "convicted_cases_count":   _i(nullable=False),
                "acquitted_cases_count":   _i(),
            },
        },
        "convicted_cases_detail": {
            "type": "array",
            "nullable": True,
            "description": "Per-case detail for past convictions. Empty array if affidavit states NIL.",
            "items": {
                "type": "object",
                "properties": {
                    "court_name":    _s(),
                    "case_number":   _s(),
                    "ipc_sections":  {"type": "array", "nullable": True,
                                       "items": {"type": "string"}},
                    "sentence":      _s(),
                },
            },
        },
        "dependents": {
            "type": "array",
            "nullable": True,
            "description": "Dependent family members. Use labels 'dependent_1', 'dependent_2' etc.; do NOT include dependent names.",
            "items": {
                "type": "object",
                "properties": {
                    "label":                          _s(nullable=False),
                    "relationship":                   _s(),
                    "age_band":                       _s(),
                    "total_movable_assets_inr":       _i(),
                    "total_immovable_assets_inr":     _i(),
                    "total_liabilities_inr":          _i(),
                },
            },
        },
        "education": {
            "type": "object",
            "properties": {
                "highest_qualification":   _s(),
            },
        },
        "profession": {
            "type": "object",
            "properties": {
                "profession_self":         _s(),
                "profession_spouse":       _s(),
                "source_of_livelihood":    _s(),
            },
        },
        "documentation": {
            "type": "object",
            "properties": {
                "estamp_certificate_number":   _s(),
                "estamp_issue_date":           _s(),
                "affidavit_signed_date":       _s(),
                "affidavit_signed_place":      _s(),
            },
        },
        "extraction_metadata": {
            "type": "object",
            "properties": {
                "extraction_notes":      {
                    "type": "array",
                    "items": {"type": "string"},
                },
                "fields_low_confidence": {
                    "type": "array",
                    "items": {"type": "string"},
                },
            },
        },
    },
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def load_preprocessed_text(json_path: Path) -> tuple[str, dict]:
    """Read a preprocessed JSON and return (concatenated page-marked text, metadata)."""
    data = json.loads(json_path.read_text())
    pages = data.get("pages", [])
    text_parts = []
    for p in pages:
        text_parts.append(f"=== PAGE {p['page']} ===")
        text_parts.append(p.get("text", ""))
    return "\n".join(text_parts), {
        "source_pdf": data.get("source_pdf"),
        "page_count": data.get("page_count"),
        "stats": data.get("stats", {}),
    }


def call_gemini(prompt: str, model) -> tuple[str, dict, str]:
    """Run one Gemini call. Returns (json_text, usage_dict, finish_reason).

    Uses response_schema to FORCE the model to produce valid JSON that
    validates against our schema — eliminates the parse errors we saw
    on the first run.
    """
    from vertexai.generative_models import GenerationConfig

    response = model.generate_content(
        prompt,
        generation_config=GenerationConfig(
            temperature=0.1,            # low — we want deterministic extraction
            response_mime_type="application/json",
            response_schema=RESPONSE_SCHEMA,   # schema-validated structured output
            max_output_tokens=32768,           # detail arrays balloon the output
        ),
    )
    usage = {}
    if hasattr(response, "usage_metadata"):
        usage = {
            "prompt_token_count":     response.usage_metadata.prompt_token_count,
            "candidates_token_count": response.usage_metadata.candidates_token_count,
            "total_token_count":      response.usage_metadata.total_token_count,
        }
    # Capture finish_reason — diagnostic if output is truncated
    finish_reason = "UNKNOWN"
    try:
        finish_reason = str(response.candidates[0].finish_reason)
    except Exception:
        pass
    return response.text, usage, finish_reason


def estimate_cost(usage: dict) -> float:
    """Gemini 2.5 Flash pricing (USD): $0.30 / 1M input, $2.50 / 1M output."""
    in_cost  = usage.get("prompt_token_count",     0) * 0.30 / 1_000_000
    out_cost = usage.get("candidates_token_count", 0) * 2.50 / 1_000_000
    return in_cost + out_cost


def summarize(label: str, extracted: dict, usage: dict) -> dict:
    """Build a flat dict of headline fields for the side-by-side comparison."""
    a_mov  = (extracted.get("assets_movable")   or {}).get("total_movable_assets_inr")
    a_imm  = (extracted.get("assets_immovable") or {}).get("total_immovable_assets_inr")
    liab   = (extracted.get("liabilities")      or {}).get("total_liabilities_inr")
    pend   = (extracted.get("criminal_pending") or {}).get("pending_cases_count")
    conv   = (extracted.get("criminal_past")    or {}).get("convicted_cases_count")
    name   = (extracted.get("identity")         or {}).get("name_in_english")
    party  = (extracted.get("political")        or {}).get("party_name")
    const  = (extracted.get("political")        or {}).get("constituency_name")
    age    = (extracted.get("identity")         or {}).get("age_years")
    educ   = (extracted.get("education")        or {}).get("highest_qualification")
    prof   = (extracted.get("profession")       or {}).get("profession_self")
    notes  = ((extracted.get("extraction_metadata") or {}).get("extraction_notes") or [])

    # Detail-array counts (None vs [] vs populated)
    def _count(key):
        v = extracted.get(key)
        if v is None:
            return "—"
        return len(v) if isinstance(v, list) else "—"
    n_vehicles  = _count("vehicles_detail")
    n_immov     = _count("immovable_detail")
    n_pending_d = _count("pending_cases_detail")
    n_convicted = _count("convicted_cases_detail")
    n_liab_d    = _count("liabilities_detail")
    n_dep       = _count("dependents")

    total = None
    if a_mov is not None or a_imm is not None:
        total = (a_mov or 0) + (a_imm or 0)

    def _fmt_inr(n):
        if n is None:
            return "—"
        if n >= 10_000_000:
            return f"₹{n/10_000_000:.2f} Cr"
        if n >= 100_000:
            return f"₹{n/100_000:.2f} L"
        return f"₹{n:,}"

    return {
        "label":              label,
        "name":               name,
        "party":              party,
        "constituency":       const,
        "age":                age,
        "education":          educ,
        "profession":         prof,
        "movable_assets":     _fmt_inr(a_mov),
        "immovable_assets":   _fmt_inr(a_imm),
        "total_assets":       _fmt_inr(total),
        "liabilities":        _fmt_inr(liab),
        "pending_cases":      pend,
        "convicted_cases":    conv,
        # Detail array counts — confirms detail extraction worked
        "vehicles_detail":    n_vehicles,
        "properties_detail":  n_immov,
        "pending_detail":     n_pending_d,
        "convicted_detail":   n_convicted,
        "liabilities_detail": n_liab_d,
        "dependents":         n_dep,
        "notes":              notes,
        "prompt_tokens":      usage.get("prompt_token_count", 0),
        "output_tokens":      usage.get("candidates_token_count", 0),
        "cost_usd":           estimate_cost(usage),
    }


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    # Lazy imports so --help and credential-check errors print cleanly
    if not os.environ.get("GOOGLE_APPLICATION_CREDENTIALS"):
        sys.exit(
            "GOOGLE_APPLICATION_CREDENTIALS env var is not set.\n"
            "Add to ~/.zshrc:\n"
            '   export GOOGLE_APPLICATION_CREDENTIALS="$HOME/.gcp/lokvani-vision-key.json"\n'
        )
    try:
        import vertexai
        from vertexai.generative_models import GenerativeModel
    except ImportError:
        sys.exit(
            "google-cloud-aiplatform not installed. From .venv-eci:\n"
            "    pip install google-cloud-aiplatform\n"
        )

    print(f"Initialising Vertex AI: project={GCP_PROJECT}, "
          f"location={GCP_LOCATION}", file=sys.stderr)
    vertexai.init(project=GCP_PROJECT, location=GCP_LOCATION)
    model = GenerativeModel(MODEL_NAME)
    print(f"Model: {MODEL_NAME}\n", file=sys.stderr)

    OUT_DIR.mkdir(parents=True, exist_ok=True)

    summaries = []
    total_cost = 0.0

    for label, year, rel_path in TEST_CANDIDATES:
        json_path = PROJECT_ROOT / rel_path
        if not json_path.exists():
            print(f"  ✗ {label}: preprocessed JSON not found at {json_path}",
                  file=sys.stderr)
            continue

        print(f"→ {label}", file=sys.stderr)
        text, meta = load_preprocessed_text(json_path)
        print(f"    source: {meta['source_pdf']} "
              f"({meta['page_count']} pages, "
              f"{len(text):,} chars OCR text)", file=sys.stderr)

        full_prompt = EXTRACTION_PROMPT + "\n" + text
        t0 = time.time()
        try:
            response_text, usage, finish_reason = call_gemini(full_prompt, model)
        except Exception as e:
            print(f"    ✗ Gemini call failed: {type(e).__name__}: {e}",
                  file=sys.stderr)
            continue
        elapsed = time.time() - t0
        cost = estimate_cost(usage)
        total_cost += cost
        print(f"    ✓ extracted in {elapsed:.1f}s "
              f"(in={usage.get('prompt_token_count', 0):,} tokens, "
              f"out={usage.get('candidates_token_count', 0):,} tokens, "
              f"finish={finish_reason}, ${cost:.4f})", file=sys.stderr)

        # Parse JSON
        try:
            extracted = json.loads(response_text)
        except json.JSONDecodeError as e:
            print(f"    ⚠ Gemini returned non-JSON: {e}", file=sys.stderr)
            extracted = {"_raw": response_text}

        # Persist full output
        out_path = OUT_DIR / f"_ab_test_{label}.json"
        out_path.write_text(json.dumps({
            "label":              label,
            "election_year":      year,
            "source_pdf":         meta["source_pdf"],
            "source_page_count":  meta["page_count"],
            "model":              MODEL_NAME,
            "usage":              usage,
            "cost_usd":           cost,
            "elapsed_seconds":    round(elapsed, 1),
            "extraction":         extracted,
        }, indent=2, ensure_ascii=False))
        print(f"    saved → {out_path.relative_to(PROJECT_ROOT)}\n",
              file=sys.stderr)

        summaries.append(summarize(label, extracted, usage))

    # ---------------- Side-by-side comparison ----------------
    print("=" * 80, file=sys.stderr)
    print("HEADLINE FIELDS — side-by-side", file=sys.stderr)
    print("=" * 80, file=sys.stderr)

    rows = [
        ("Name",            "name"),
        ("Party",           "party"),
        ("Constituency",    "constituency"),
        ("Age",             "age"),
        ("Education",       "education"),
        ("Profession",      "profession"),
        ("Movable assets",  "movable_assets"),
        ("Immovable assets","immovable_assets"),
        ("TOTAL ASSETS",    "total_assets"),
        ("Liabilities",     "liabilities"),
        ("Pending cases",   "pending_cases"),
        ("Convicted cases", "convicted_cases"),
        # Detail-array counts (proves we extracted the detail arrays)
        ("→ vehicles",        "vehicles_detail"),
        ("→ properties",      "properties_detail"),
        ("→ pending detail",  "pending_detail"),
        ("→ convicted detail","convicted_detail"),
        ("→ liabilities det", "liabilities_detail"),
        ("→ dependents",      "dependents"),
        ("Prompt tokens",     "prompt_tokens"),
        ("Output tokens",     "output_tokens"),
        ("Cost (USD)",        "cost_usd"),
    ]

    # Column widths
    col_width = 22
    print(f"{'Field':<22}" + "".join(
        f"{s['label'][:col_width-2]:<{col_width}}" for s in summaries),
          file=sys.stderr)
    print("-" * (22 + col_width * len(summaries)), file=sys.stderr)
    for human, key in rows:
        line = f"{human:<22}"
        for s in summaries:
            v = s.get(key)
            if isinstance(v, float):
                v = f"${v:.4f}"
            elif v is None:
                v = "—"
            line += f"{str(v)[:col_width-2]:<{col_width}}"
        print(line, file=sys.stderr)

    print("-" * (22 + col_width * len(summaries)), file=sys.stderr)
    print(f"\nTotal A/B test cost: ${total_cost:.4f}", file=sys.stderr)
    if total_cost > 0:
        scaled = total_cost / len(summaries) * 1684
        print(f"Projected cost for all 1,684 affidavits: ~${scaled:.2f}",
              file=sys.stderr)

    # Any LLM notes worth surfacing
    for s in summaries:
        if s.get("notes"):
            print(f"\nNotes from {s['label']}:", file=sys.stderr)
            for n in s["notes"]:
                print(f"  · {n}", file=sys.stderr)


if __name__ == "__main__":
    main()
