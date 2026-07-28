"""Send PDFs directly to Gemini as multimodal input (bypass Cloud
Vision entirely). For cases where Cloud Vision fails on Hindi /
regional-language affidavits, Gemini 2.5 Flash can read the PDF
natively and extract structured fields.

Reads target files from a mini-allowlist (or a state's gaps CSV).
Writes rescue extractions to a SIDECAR folder — never overwrites
the existing llm_extracted/<slug>/ so we can review before applying.

Usage:
    # Target the 24 UP 2022 gap candidates
    python scripts/gemini_direct_pdf_extract.py \\
        --state "Uttar Pradesh" --year 2022 \\
        --from-gaps

    # Later, apply results to DB via resolve_collision_gaps if they land

Note: sending PDFs as binary is more expensive per file than sending
OCR text (~5-10x). Only use for the small set of hard cases.
"""
from __future__ import annotations
import argparse
import csv
import json
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
MODEL_NAME = "gemini-2.5-flash"


def _load_prompt_and_schema():
    """Reuse the same EXTRACTION_PROMPT + RESPONSE_SCHEMA the text-based
    extractor uses so downstream tooling (apply_llm_extraction) consumes
    our output identically."""
    sys.path.insert(0, str(ROOT / "scripts"))
    from llm_extract_via_gemini import EXTRACTION_PROMPT, RESPONSE_SCHEMA
    return EXTRACTION_PROMPT, RESPONSE_SCHEMA


def extract_one(pdf_path: Path, state: str, year: int, client,
                 prompt_template: str, schema: dict) -> tuple[dict | None, str]:
    """One PDF -> structured extraction. Returns (extraction_dict, message)."""
    from google.genai import types
    try:
        pdf_bytes = pdf_path.read_bytes()
    except Exception as e:
        return None, f"read_bytes failed: {e}"

    # EXTRACTION_PROMPT is a static instruction block. When sent alongside
    # a PDF part, Gemini reads the PDF natively (its own OCR + language
    # understanding, incl. Devanagari). We don't append OCR text because
    # the PDF itself is the source.
    prompt = prompt_template

    try:
        resp = client.models.generate_content(
            model=MODEL_NAME,
            contents=[
                types.Part.from_bytes(data=pdf_bytes, mime_type="application/pdf"),
                prompt,
            ],
            config=types.GenerateContentConfig(
                temperature=0.1,
                response_mime_type="application/json",
                response_schema=schema,
                max_output_tokens=32768,
            ),
        )
    except Exception as e:
        return None, f"api_error: {type(e).__name__}: {str(e)[:200]}"

    try:
        text = resp.text or ""
        parsed = json.loads(text)
    except Exception as e:
        return None, f"json_parse_failed: {e}; raw first 200: {text[:200]!r}"

    usage = {}
    try:
        u = resp.usage_metadata
        usage = {
            "prompt_tokens":     getattr(u, "prompt_token_count", None),
            "completion_tokens": getattr(u, "candidates_token_count", None),
            "total_tokens":      getattr(u, "total_token_count", None),
        }
    except Exception:
        pass
    return {
        "extraction": parsed,
        "usage": usage,
        "extraction_source": "gemini_direct_pdf",
    }, "ok"


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                  formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--state", required=True)
    ap.add_argument("--year", type=int, required=True)
    ap.add_argument("--from-gaps", action="store_true",
                    help="Target the state's gap CSV")
    ap.add_argument("--pdfs", nargs="*", default=[],
                    help="Alternative: specific PDF filenames to process")
    ap.add_argument("--workers", type=int, default=5,
                    help="Parallel API calls (default 5)")
    ap.add_argument("--limit", type=int, default=0,
                    help="Smoke test — process only first N")
    ap.add_argument("--dry-run", action="store_true",
                    help="List targets, don't call API")
    args = ap.parse_args()

    slug = args.state.lower().replace(" ", "")
    # Slug aliases: some cycles use short forms in the raw_pdfs dir
    ALIASES = {"jammuandkashmir": ["jk", "jammukashmir"]}
    slug_candidates = [slug] + ALIASES.get(slug, [])
    slug_year = f"{slug_candidates[-1] if slug == 'jammuandkashmir' else slug}_{args.year}"
    # Prefer canonical short-slug for JK output folder so it matches llm_extracted/jk_2024/
    if slug == "jammuandkashmir":
        slug_year = f"jk_{args.year}"

    # Find cycle dir — try each candidate slug against raw_pdfs entries.
    raw_root = ROOT / "data/eci/raw_pdfs"
    cycle_dir = None
    for d in sorted(raw_root.iterdir()):
        for s in slug_candidates:
            if d.name.lower().startswith(s) and d.name.endswith(str(args.year)):
                cycle_dir = d
                break
        if cycle_dir:
            break
    if not cycle_dir:
        sys.exit(f"No cycle dir for {args.state} {args.year} (tried {slug_candidates})")
    raw_dir = cycle_dir / "raw_pdfs" if (cycle_dir / "raw_pdfs").exists() else cycle_dir

    # Determine target PDFs
    targets: list[str] = []
    if args.from_gaps:
        gap_csv = ROOT / f"data/reports/gaps_{slug_year}.csv"
        if not gap_csv.exists():
            sys.exit(f"No gap CSV: {gap_csv.relative_to(ROOT)}")
        targets = [r["pdf"] for r in csv.DictReader(gap_csv.open())]
    elif args.pdfs:
        targets = args.pdfs
    else:
        sys.exit("Need --from-gaps or --pdfs")

    if args.limit:
        targets = targets[:args.limit]

    out_dir = ROOT / f"data/eci/for_ai/direct_pdf_extracted/{slug_year}"
    out_dir.mkdir(parents=True, exist_ok=True)

    print(f"State:       {args.state} {args.year}")
    print(f"PDFs target: {len(targets)}")
    print(f"Output dir:  {out_dir.relative_to(ROOT)}")
    print()

    if args.dry_run:
        for t in targets[:10]:
            print(f"  would process: {t}")
        if len(targets) > 10:
            print(f"  ... and {len(targets) - 10} more")
        return

    # Init client
    from google import genai
    project = os.environ.get("GCP_PROJECT")
    if not project:
        sys.exit("GCP_PROJECT env var not set (source secrets/.env)")
    client = genai.Client(vertexai=True, project=project, location="us-central1")

    prompt_template, schema = _load_prompt_and_schema()

    ok = fail = 0
    t0 = time.time()

    def _process(name):
        pdf_path = raw_dir / name
        if not pdf_path.exists():
            return name, None, f"pdf_missing: {pdf_path.name}"
        result, msg = extract_one(pdf_path, args.state, args.year, client,
                                    prompt_template, schema)
        if result:
            # Extract aff_id from filename NAME__<AFFID>.pdf so apply can
            # link this back to eci_candidates_provisional row.
            stem = name[:-4] if name.endswith(".pdf") else name
            aff_id = ""
            if "__" in stem:
                aff_id = stem.rsplit("__", 1)[-1]
            result["source_pdf"] = name
            result["state"] = args.state
            result["election_year"] = args.year
            result["affidavit_id"] = aff_id
            result["label"] = stem
            result["model"] = MODEL_NAME
            out_path = out_dir / (name.replace(".pdf", ".json"))
            out_path.write_text(json.dumps(result, indent=2, ensure_ascii=False))
        return name, result, msg

    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        futures = {ex.submit(_process, t): t for t in targets}
        for i, fut in enumerate(as_completed(futures), 1):
            name, result, msg = fut.result()
            if result:
                ext = result.get("extraction") or {}
                ident = ext.get("identity") or {}
                pol = ext.get("political") or {}
                am = ext.get("assets_movable") or {}
                cand_name = ident.get("name_in_english") or "(no name)"
                mov = am.get("total_movable_assets_inr")
                print(f"[{i:2d}/{len(targets)}] ✓ {name[:35]:<35s}  "
                      f"name={cand_name[:20]:<20s}  movable={mov!r}")
                ok += 1
            else:
                print(f"[{i:2d}/{len(targets)}] ✗ {name[:35]:<35s}  {msg}")
                fail += 1

    dt = time.time() - t0
    print(f"\n{'='*60}")
    print(f"Direct-PDF extraction summary:")
    print(f"  ok:    {ok}")
    print(f"  fail:  {fail}")
    print(f"  time:  {dt:.0f}s ({dt/60:.1f}m)")
    print(f"\nResults in: {out_dir.relative_to(ROOT)}")
    print(f"\nNext: review a few outputs, then move them into "
          f"data/eci/for_ai/llm_extracted/{slug_year}/ if they look good, "
          f"and re-run apply.")


if __name__ == "__main__":
    main()
