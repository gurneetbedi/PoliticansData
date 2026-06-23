"""
Run all candidate PDFs through an LLM to produce structured JSON
matching data/eci/for_ai/extraction_schema.json.

Two modes:

  --mode pdf     Send the raw PDF to the model's vision endpoint.
                 Higher accuracy, higher cost (~$0.40/candidate at
                 Claude Sonnet 4 prices on a 30-page affidavit).
                 Requires no preprocessing run.

  --mode text    Send the preprocessed per-page text from
                 scripts/preprocess_eci_pdfs.py output. Much cheaper
                 (~$0.05/candidate) and faster, but quality depends on
                 the OCR pipeline having captured enough.

DEFAULT: --mode pdf for highest quality.

Outputs one JSON per candidate at data/eci/for_ai/output/<base>.json
matching the project's extraction_schema.json. Resumable — re-running
skips any candidate whose output already exists.

USAGE
-----
    # Required: set the API key for your provider of choice
    export ANTHROPIC_API_KEY=sk-ant-...

    # Install the SDK
    pip install anthropic jsonschema

    # Smoke test on 2 PDFs first
    python scripts/extract_via_llm.py --limit 2

    # Once happy, full run
    python scripts/extract_via_llm.py

Cost is tracked per-candidate and a running total printed to stderr.
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import sys
import time
from pathlib import Path

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

PROJECT_ROOT = Path(__file__).resolve().parent.parent

PDF_DIR_DEFAULT = PROJECT_ROOT / "data/eci/for_ai/pdfs"
TEXT_DIR_DEFAULT = PROJECT_ROOT / "data/eci/for_ai/preprocessed"
OUT_DIR_DEFAULT = PROJECT_ROOT / "data/eci/for_ai/output"

PROMPT_PATH = PROJECT_ROOT / "data/eci/for_ai/extraction_prompt.md"
SCHEMA_PATH = PROJECT_ROOT / "data/eci/for_ai/extraction_schema.json"
EXAMPLE_PATH = PROJECT_ROOT / "data/eci/for_ai/example_output.json"

# Claude Sonnet 4.6 — best quality/cost ratio for structured extraction
# from multi-page PDFs as of June 2026.
CLAUDE_MODEL = "claude-sonnet-4-6"
CLAUDE_MAX_OUTPUT_TOKENS = 8000

# Pricing (USD per million tokens) — used for cost tracking only.
# Update if Anthropic changes pricing.
CLAUDE_INPUT_PER_MTOK = 3.00
CLAUDE_OUTPUT_PER_MTOK = 15.00


# ---------------------------------------------------------------------------
# Provider — Anthropic (Claude)
# ---------------------------------------------------------------------------

def _build_system_prompt() -> str:
    """The system prompt = the extraction_prompt.md content. Keep it as the
    user's source of truth so prompt edits don't require code changes."""
    return PROMPT_PATH.read_text()


def _build_example_message() -> str:
    """Show the model the gold-standard example up front, in JSON form."""
    schema = json.loads(SCHEMA_PATH.read_text())
    example = json.loads(EXAMPLE_PATH.read_text())
    return (
        "Here is the JSON Schema your output must match:\n"
        + json.dumps(schema, ensure_ascii=False)
        + "\n\nHere is one fully-completed real example (human-reviewed):\n"
        + json.dumps(example, ensure_ascii=False)
        + "\n\nReturn JSON only — no prose, no markdown fences."
    )


def call_claude_pdf(client, pdf_path: Path) -> tuple[dict, dict]:
    """Send a PDF + extraction prompt to Claude. Returns (parsed_json, usage)."""
    pdf_b64 = base64.b64encode(pdf_path.read_bytes()).decode()

    response = client.messages.create(
        model=CLAUDE_MODEL,
        max_tokens=CLAUDE_MAX_OUTPUT_TOKENS,
        system=_build_system_prompt(),
        messages=[
            {
                "role": "user",
                "content": [
                    {
                        "type": "document",
                        "source": {
                            "type": "base64",
                            "media_type": "application/pdf",
                            "data": pdf_b64,
                        },
                    },
                    {
                        "type": "text",
                        "text": _build_example_message()
                        + f"\n\nThe attached PDF is: {pdf_path.name}\n"
                        "Extract it now.",
                    },
                ],
            }
        ],
    )

    text = "".join(b.text for b in response.content if hasattr(b, "text"))
    parsed = _parse_json_response(text)
    usage = {
        "input_tokens": response.usage.input_tokens,
        "output_tokens": response.usage.output_tokens,
    }
    return parsed, usage


def call_claude_text(client, pdf_name: str, preprocessed_text: str) -> tuple[dict, dict]:
    """Send the OCR'd text (much cheaper than the PDF itself) to Claude."""
    response = client.messages.create(
        model=CLAUDE_MODEL,
        max_tokens=CLAUDE_MAX_OUTPUT_TOKENS,
        system=_build_system_prompt(),
        messages=[
            {
                "role": "user",
                "content": (
                    _build_example_message()
                    + f"\n\nThe candidate PDF is: {pdf_name}\n"
                    + "Below is the per-page text extracted via EasyOCR + image "
                    "sanitation. Column-separators `|` indicate table cells. "
                    "Numbers may be Indian-format (lakhs/crores). The text is "
                    "noisy — apply your best judgement and follow the rules in "
                    "the system prompt. Extract it now.\n\n"
                    + preprocessed_text
                ),
            }
        ],
    )

    text = "".join(b.text for b in response.content if hasattr(b, "text"))
    parsed = _parse_json_response(text)
    usage = {
        "input_tokens": response.usage.input_tokens,
        "output_tokens": response.usage.output_tokens,
    }
    return parsed, usage


# ---------------------------------------------------------------------------
# JSON response handling
# ---------------------------------------------------------------------------

def _parse_json_response(text: str) -> dict:
    """Claude sometimes wraps output in ```json ... ``` even when told not to.
    Strip that defensively before json.loads."""
    s = text.strip()
    if s.startswith("```"):
        # Drop opening ```[lang] line
        s = s.split("\n", 1)[1] if "\n" in s else s.lstrip("`")
        # Drop trailing ```
        if s.endswith("```"):
            s = s[: -3].rstrip()
    return json.loads(s)


def validate_against_schema(payload: dict, schema: dict) -> list[str]:
    """Return a list of validation errors. Empty list = valid."""
    try:
        import jsonschema
    except ImportError:
        return ["jsonschema not installed — pip install jsonschema to validate"]
    errs = []
    validator = jsonschema.Draft202012Validator(schema)
    for err in validator.iter_errors(payload):
        loc = "/".join(str(p) for p in err.absolute_path) or "<root>"
        errs.append(f"{loc}: {err.message}")
    return errs


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument("--mode", choices=["pdf", "text"], default="pdf",
                    help="pdf = send raw PDF to vision endpoint (default, "
                         "highest quality, ~$0.40/candidate). text = send "
                         "preprocessed text from preprocess_eci_pdfs.py output "
                         "(~$0.05/candidate, lower quality on noisy scans).")
    ap.add_argument("--input-dir", default=None,
                    help="Override input dir. Defaults to "
                         "data/eci/for_ai/pdfs/ for pdf mode or "
                         "data/eci/for_ai/preprocessed/ for text mode.")
    ap.add_argument("--output-dir", default=str(OUT_DIR_DEFAULT),
                    help="Where to write extracted JSON files.")
    ap.add_argument("--limit", type=int, default=0,
                    help="Stop after N candidates (smoke test). 0 = no limit.")
    ap.add_argument("--only", action="append",
                    help="Restrict to specific filenames (can repeat). "
                         "For pdf mode, pass the .pdf filename.")
    ap.add_argument("--refresh", action="store_true",
                    help="Re-extract even if output JSON already exists.")
    ap.add_argument("--no-validate", action="store_true",
                    help="Skip JSON Schema validation of each output.")
    args = ap.parse_args()

    # Resolve input dir
    if args.input_dir:
        in_dir = Path(args.input_dir).resolve()
    else:
        in_dir = PDF_DIR_DEFAULT if args.mode == "pdf" else TEXT_DIR_DEFAULT
    out_dir = Path(args.output_dir).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)

    # Required files
    for p, label in [(PROMPT_PATH, "prompt"), (SCHEMA_PATH, "schema"),
                      (EXAMPLE_PATH, "example")]:
        if not p.exists():
            sys.exit(f"Missing {label} file: {p}")

    if not os.environ.get("ANTHROPIC_API_KEY"):
        sys.exit("ANTHROPIC_API_KEY not set. Export it before running:\n"
                  "  export ANTHROPIC_API_KEY=sk-ant-...")

    try:
        import anthropic
    except ImportError:
        sys.exit("Anthropic SDK not installed. Run:\n  pip install anthropic")

    client = anthropic.Anthropic()
    schema = json.loads(SCHEMA_PATH.read_text())

    # Gather inputs
    if args.mode == "pdf":
        inputs = sorted(in_dir.glob("*.pdf"))
    else:
        inputs = sorted(in_dir.glob("*.json"))
        # Skip QC report / underscored files
        inputs = [p for p in inputs if not p.name.startswith("_")]

    if args.only:
        keeper = set(args.only)
        inputs = [p for p in inputs if p.name in keeper]
    if args.limit:
        inputs = inputs[: args.limit]
    if not inputs:
        sys.exit(f"No inputs in {in_dir}")

    print(f"Mode: {args.mode}  |  Input dir: {in_dir}  |  Output dir: {out_dir}",
          file=sys.stderr)
    print(f"Model: {CLAUDE_MODEL}  |  {len(inputs)} candidates to process",
          file=sys.stderr)

    total_in_tokens = 0
    total_out_tokens = 0
    succeeded = 0
    failed = 0
    skipped = 0
    t_start = time.time()

    for i, input_path in enumerate(inputs, 1):
        base = input_path.stem
        out_path = out_dir / f"{base}.json"
        if out_path.exists() and not args.refresh:
            print(f"[{i}/{len(inputs)}] {input_path.name}  (cached, skip)",
                  file=sys.stderr)
            skipped += 1
            continue

        print(f"[{i}/{len(inputs)}] {input_path.name}  ... ",
              file=sys.stderr, end="", flush=True)
        try:
            if args.mode == "pdf":
                parsed, usage = call_claude_pdf(client, input_path)
            else:
                payload = json.loads(input_path.read_text())
                pages_text = "\n\n=== PAGE BREAK ===\n\n".join(
                    f"[Page {p.get('page')}]\n{p.get('text', '')}"
                    for p in payload.get("pages", [])
                )
                source_pdf = payload.get("source_pdf", input_path.name)
                parsed, usage = call_claude_text(client, source_pdf, pages_text)
        except Exception as e:
            print(f"FAILED: {type(e).__name__}: {e}", file=sys.stderr)
            failed += 1
            continue

        # Validate
        validation_errors = []
        if not args.no_validate:
            validation_errors = validate_against_schema(parsed, schema)

        # Attach run metadata as a private field
        parsed.setdefault("_run_metadata", {}).update({
            "model": CLAUDE_MODEL,
            "mode": args.mode,
            "input_tokens": usage["input_tokens"],
            "output_tokens": usage["output_tokens"],
            "validation_errors": validation_errors,
            "extracted_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        })
        out_path.write_text(json.dumps(parsed, indent=2, ensure_ascii=False))

        total_in_tokens += usage["input_tokens"]
        total_out_tokens += usage["output_tokens"]
        cost = (usage["input_tokens"] / 1e6 * CLAUDE_INPUT_PER_MTOK
                 + usage["output_tokens"] / 1e6 * CLAUDE_OUTPUT_PER_MTOK)

        flag = "✓" if not validation_errors else f"⚠ {len(validation_errors)} schema warnings"
        print(f"done  in={usage['input_tokens']:>6d} out={usage['output_tokens']:>4d} "
              f"  ${cost:.3f}  {flag}", file=sys.stderr)
        succeeded += 1

    elapsed = time.time() - t_start
    total_cost = (total_in_tokens / 1e6 * CLAUDE_INPUT_PER_MTOK
                  + total_out_tokens / 1e6 * CLAUDE_OUTPUT_PER_MTOK)
    print(f"\n========== EXTRACTION SUMMARY ==========", file=sys.stderr)
    print(f"  Succeeded: {succeeded}", file=sys.stderr)
    print(f"  Failed:    {failed}", file=sys.stderr)
    print(f"  Skipped (cached): {skipped}", file=sys.stderr)
    print(f"  Total tokens: in={total_in_tokens:,}  out={total_out_tokens:,}",
          file=sys.stderr)
    print(f"  Total cost (USD): ${total_cost:.2f}", file=sys.stderr)
    print(f"  Elapsed: {elapsed:.0f}s ({elapsed / 60:.1f} min)", file=sys.stderr)
    print(f"  Outputs: {out_dir}", file=sys.stderr)


if __name__ == "__main__":
    main()
