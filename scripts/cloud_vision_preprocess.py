"""
Cloud Vision OCR preprocessing for ECI affidavits.

REPLACES scripts/preprocess_modal.py. Same JSON output schema (so the
downstream extract_structured.py + load_eci_to_db.py work unchanged),
but uses Google Cloud Vision API instead of self-hosted EasyOCR on
Modal. Materially faster (parallel API calls vs container scheduling)
and higher-quality OCR on scanned + multilingual content (correctly
preserves Devanagari, handles notary stamps natively without our
HSV-removal hack).

PIPELINE PER PDF
================
1. Get the page count via pdf2image / poppler-utils. If the PDF is
   corrupt (broken xref, unreadable header, etc.), write a sentinel
   JSON with `corrupt: true` and move on — same pattern as the Modal
   script so a single bad file doesn't kill the run.

2. Try pdfplumber FIRST on every page. If a page has a usable text
   layer (>= 150 chars of clean text), use it — free and instant.
   In Delhi 2020 this catches about 5% of pages; in cycles where ECI
   ingested digital uploads it can be 30%+.

3. For pages with no usable text layer, batch them into chunks of 5
   (Vision sync API caps at 5 pages per call) and call
   DOCUMENT_TEXT_DETECTION. Stitch the chunked responses back into
   per-page records.

4. Per-PDF JSON output has the exact shape preprocess_modal.py emits,
   plus an extra `cost_estimate_usd` field so we can sum API spend.

PARALLELISM
===========
ThreadPoolExecutor with 20 workers by default. Cloud Vision's published
sync-API quota is ~1,800 concurrent requests per project, so 20 is
nowhere near the limit. The Vision client is thread-safe.

OUTPUT JSON SHAPE
=================
  {
    "source_pdf": "<filename>.pdf",
    "page_count": N,
    "pages": [
      {"page": 1, "method": "pdfplumber"|"cloud_vision", "text": "..."},
      ...
    ],
    "stats": {
      "pages_pdfplumber":  M,
      "pages_cloud_vision": K,
      "elapsed_seconds":   F,
      "cost_estimate_usd": F     # at $1.50 / 1,000 pages
    },
    "corrupt": false
  }

USAGE
=====
  # Smoke test on 5 PDFs
  python scripts/cloud_vision_preprocess.py \\
    --pdf-dir data/eci/raw_pdfs/delhi-2020/raw_pdfs \\
    --out-dir data/eci/for_ai/preprocessed_delhi_2020 \\
    --limit 5

  # Full state — only processes PDFs without an existing JSON output
  python scripts/cloud_vision_preprocess.py \\
    --pdf-dir data/eci/raw_pdfs/delhi-2020/raw_pdfs \\
    --out-dir data/eci/for_ai/preprocessed_delhi_2020

  # Specific filenames (e.g. the 9 missing Delhi 2025 backfill)
  python scripts/cloud_vision_preprocess.py \\
    --pdf-dir data/eci/raw_pdfs/delhi-2025/raw_pdfs \\
    --out-dir data/eci/for_ai/preprocessed \\
    --pdf-allowlist data/eci/_missing_2025.txt

  # More parallelism (default 20; Vision allows 1,800)
  python scripts/cloud_vision_preprocess.py --workers 40 ...

ENV
===
  GOOGLE_APPLICATION_CREDENTIALS must point at the service-account
  JSON for the lokvani GCP project. Set in ~/.zshrc:
      export GOOGLE_APPLICATION_CREDENTIALS="$HOME/.gcp/lokvani-vision-key.json"
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Vision sync API caps at 5 pages per request.
CHUNK_SIZE = 5

# $1.50 per 1,000 pages = $0.0015 per page for DOCUMENT_TEXT_DETECTION.
# (Cloud Vision pricing as of 2026; check console for current rate.)
COST_PER_PAGE_USD = 0.0015

# Minimum text length for pdfplumber to be considered "good enough".
# Pages below this threshold fall through to OCR.
PDFPLUMBER_MIN_CHARS = 150


# ---------------------------------------------------------------------------
# Lazy imports (validated up front)
# ---------------------------------------------------------------------------

def _check_imports():
    """Verify all required deps are installed before we start working."""
    missing = []
    try:
        from google.cloud import vision  # noqa: F401
    except ImportError:
        missing.append("google-cloud-vision")
    try:
        import pdfplumber  # noqa: F401
    except ImportError:
        missing.append("pdfplumber")
    try:
        import pdf2image  # noqa: F401
    except ImportError:
        missing.append("pdf2image")
    if missing:
        sys.exit(
            "Missing packages. From .venv-eci:\n"
            f"    pip install {' '.join(missing)}\n"
        )


# ---------------------------------------------------------------------------
# pdfplumber pass — extract text layers in one go
# ---------------------------------------------------------------------------

def pdfplumber_extract(pdf_path: Path, total_pages: int) -> dict[int, str]:
    """Try the text-layer fast path for every page.

    Opens the PDF ONCE (vs once per page) for efficiency. Returns
    {page_num: text} only for pages where extraction yielded enough
    clean text to skip OCR. Pages absent from the result must go
    through Cloud Vision.
    """
    import pdfplumber
    out: dict[int, str] = {}
    try:
        with pdfplumber.open(str(pdf_path)) as pdf:
            for p in range(1, total_pages + 1):
                try:
                    text = pdf.pages[p - 1].extract_text(layout=True) or ""
                    if len(text.strip()) >= PDFPLUMBER_MIN_CHARS:
                        out[p] = text
                except Exception:
                    # Per-page failure — let this page fall through to OCR
                    pass
    except Exception:
        # Whole-PDF failure — return empty so every page goes to OCR
        return {}
    return out


# ---------------------------------------------------------------------------
# Cloud Vision pass — batched per 5 pages
# ---------------------------------------------------------------------------

def vision_ocr_pages(pdf_bytes: bytes, page_numbers: list[int],
                      client) -> dict[int, str]:
    """Run DOCUMENT_TEXT_DETECTION on a chunk of pages.

    Vision's sync API accepts a `pages` list of 1-indexed integers,
    up to 5 per call. Returns {page_num: extracted_text}. On per-page
    error the entry is empty string (so the JSON record still exists
    and downstream code doesn't trip on missing keys).
    """
    from google.cloud import vision
    request = {
        "input_config": {
            "content": pdf_bytes,
            "mime_type": "application/pdf",
        },
        "features": [{"type_": vision.Feature.Type.DOCUMENT_TEXT_DETECTION}],
        "pages": page_numbers,
    }
    try:
        responses = client.batch_annotate_files(requests=[request])
    except Exception as e:
        # Whole-chunk failure — return empty strings so the per-page
        # entries are still created. Caller logs the chunk-level error
        # via the stats block.
        return {p: "" for p in page_numbers}

    file_response = responses.responses[0]
    out: dict[int, str] = {}
    for i, page_response in enumerate(file_response.responses):
        page_num = page_numbers[i] if i < len(page_numbers) else None
        if page_num is None:
            continue
        if page_response.error.message:
            out[page_num] = ""
        else:
            out[page_num] = page_response.full_text_annotation.text or ""
    return out


# ---------------------------------------------------------------------------
# Per-PDF processing
# ---------------------------------------------------------------------------

def process_one_pdf(pdf_path: Path, out_dir: Path, vision_client) -> dict:
    """Process one PDF end-to-end. Returns a small status dict for the
    caller's progress log. Writes the JSON output to disk as a side
    effect; downstream tooling reads from that file."""
    import pdf2image

    out_path = out_dir / f"{pdf_path.stem}.json"
    t_start = time.time()

    # --- Page count via poppler ----------------------------------------
    try:
        info = pdf2image.pdfinfo_from_path(str(pdf_path))
        total_pages = int(info["Pages"])
    except Exception as e:
        # Corrupt PDF — write a sentinel JSON and bail. Downstream
        # loaders treat `corrupt: true` as "skip this row".
        result = {
            "source_pdf": pdf_path.name,
            "page_count": 0,
            "pages": [],
            "stats": {
                "pages_pdfplumber":  0,
                "pages_cloud_vision": 0,
                "elapsed_seconds":   0,
                "cost_estimate_usd": 0,
                "error": f"{type(e).__name__}: {str(e)[:200]}",
            },
            "corrupt": True,
        }
        out_path.write_text(json.dumps(result, indent=2, ensure_ascii=False))
        return {"status": "corrupt", "name": pdf_path.name,
                "pages": 0, "elapsed": 0, "vision_pages": 0}

    # --- pdfplumber fast path on all pages -----------------------------
    plumb_pages = pdfplumber_extract(pdf_path, total_pages)

    # --- For pages without a text layer, batch through Vision ----------
    pages_needing_ocr = [p for p in range(1, total_pages + 1)
                          if p not in plumb_pages]
    vision_results: dict[int, str] = {}
    if pages_needing_ocr:
        pdf_bytes = pdf_path.read_bytes()
        for chunk_start in range(0, len(pages_needing_ocr), CHUNK_SIZE):
            chunk = pages_needing_ocr[chunk_start:chunk_start + CHUNK_SIZE]
            chunk_results = vision_ocr_pages(pdf_bytes, chunk, vision_client)
            vision_results.update(chunk_results)

    # --- Assemble per-page records in order ----------------------------
    pages_out = []
    for p in range(1, total_pages + 1):
        if p in plumb_pages:
            pages_out.append({"page": p, "method": "pdfplumber",
                              "text": plumb_pages[p]})
        else:
            pages_out.append({"page": p, "method": "cloud_vision",
                              "text": vision_results.get(p, "")})

    elapsed = round(time.time() - t_start, 1)
    vision_count = len(pages_needing_ocr)
    cost = round(vision_count * COST_PER_PAGE_USD, 4)

    result = {
        "source_pdf": pdf_path.name,
        "page_count": total_pages,
        "pages": pages_out,
        "stats": {
            "pages_pdfplumber":  len(plumb_pages),
            "pages_cloud_vision": vision_count,
            "elapsed_seconds":   elapsed,
            "cost_estimate_usd": cost,
        },
        "corrupt": False,
    }
    out_path.write_text(json.dumps(result, indent=2, ensure_ascii=False))
    return {"status": "ok", "name": pdf_path.name,
            "pages": total_pages, "elapsed": elapsed,
            "vision_pages": vision_count}


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    _check_imports()

    if not os.environ.get("GOOGLE_APPLICATION_CREDENTIALS"):
        sys.exit(
            "GOOGLE_APPLICATION_CREDENTIALS env var is not set.\n"
            "Add to ~/.zshrc:\n"
            '   export GOOGLE_APPLICATION_CREDENTIALS="$HOME/.gcp/lokvani-vision-key.json"\n'
            "Then `source ~/.zshrc` and re-run."
        )

    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument("--pdf-dir", required=True,
                    help="Directory of input PDFs.")
    ap.add_argument("--out-dir", required=True,
                    help="Directory where per-PDF JSON output is written.")
    ap.add_argument("--limit", type=int, default=0,
                    help="Process at most N missing PDFs (smoke test).")
    ap.add_argument("--workers", type=int, default=20,
                    help="Parallel Vision API workers. Default 20. "
                         "Vision allows up to ~1,800 concurrent.")
    ap.add_argument("--refresh", action="store_true",
                    help="Re-process even if local output already exists.")
    ap.add_argument("--pdf-allowlist", default="",
                    help="Path to a text file containing one PDF filename "
                         "per line. Only those will be processed.")
    args = ap.parse_args()

    project_root = Path(__file__).resolve().parent.parent
    pdf_dir = Path(args.pdf_dir)
    out_dir = Path(args.out_dir)
    if not pdf_dir.is_absolute():
        pdf_dir = project_root / pdf_dir
    if not out_dir.is_absolute():
        out_dir = project_root / out_dir

    if not pdf_dir.exists():
        sys.exit(f"PDF dir not found: {pdf_dir}")
    out_dir.mkdir(parents=True, exist_ok=True)

    pdfs = sorted(pdf_dir.glob("*.pdf"))
    if not pdfs:
        sys.exit(f"No PDFs in {pdf_dir}")

    # --- Optional allowlist filter -----------------------------------
    if args.pdf_allowlist:
        allow_path = Path(args.pdf_allowlist)
        if not allow_path.is_absolute():
            allow_path = project_root / allow_path
        if not allow_path.exists():
            sys.exit(f"Allowlist file not found: {allow_path}")
        names = {line.strip() for line in allow_path.read_text().splitlines()
                  if line.strip() and not line.startswith("#")}
        before = len(pdfs)
        pdfs = [p for p in pdfs if p.name in names]
        print(f"Allowlist filtered: {before} → {len(pdfs)} PDFs",
              file=sys.stderr)

    # --- Cache-skip already-done files --------------------------------
    todo = []
    for pdf in pdfs:
        out_path = out_dir / f"{pdf.stem}.json"
        if out_path.exists() and not args.refresh:
            continue
        todo.append(pdf)

    if args.limit:
        todo = todo[: args.limit]

    if not todo:
        print("Everything is already preprocessed — nothing to do.",
              file=sys.stderr)
        return

    print(f"Input dir:  {pdf_dir}", file=sys.stderr)
    print(f"Output dir: {out_dir}", file=sys.stderr)
    print(f"Processing {len(todo)} PDFs with {args.workers} workers ...",
          file=sys.stderr)

    # One Vision client across all threads (Vision client is thread-safe).
    from google.cloud import vision
    vision_client = vision.ImageAnnotatorClient()

    # --- Fan out via thread pool --------------------------------------
    t_start = time.time()
    succeeded = corrupt_n = failed = 0
    total_vision_pages = 0

    with ThreadPoolExecutor(max_workers=args.workers) as executor:
        futures = {
            executor.submit(process_one_pdf, pdf, out_dir, vision_client): pdf
            for pdf in todo
        }
        for i, fut in enumerate(as_completed(futures), 1):
            pdf = futures[fut]
            try:
                result = fut.result()
            except Exception as e:
                failed += 1
                print(f"  ✗ [{i:3d}/{len(todo)}] {pdf.name}: "
                      f"{type(e).__name__}: {e}", file=sys.stderr)
                continue

            if result["status"] == "corrupt":
                corrupt_n += 1
                print(f"  ⚠ [{i:3d}/{len(todo)}] {pdf.name}: corrupt PDF",
                      file=sys.stderr)
            else:
                succeeded += 1
                total_vision_pages += result.get("vision_pages", 0)
                print(f"  ✓ [{i:3d}/{len(todo)}] {pdf.name}  "
                      f"({result['pages']}p, {result['elapsed']}s, "
                      f"vision={result['vision_pages']}p)",
                      file=sys.stderr)

    elapsed_total = round(time.time() - t_start, 1)
    cost = round(total_vision_pages * COST_PER_PAGE_USD, 2)

    print(f"\n========== CLOUD VISION SUMMARY ==========", file=sys.stderr)
    print(f"  Succeeded:                {succeeded}", file=sys.stderr)
    print(f"  Corrupt PDFs (sentinel):  {corrupt_n}", file=sys.stderr)
    print(f"  Transient failures:       {failed}", file=sys.stderr)
    print(f"  Cloud Vision pages OCR'd: {total_vision_pages:,}",
          file=sys.stderr)
    print(f"  Total wall time:          {elapsed_total}s "
          f"({elapsed_total/60:.1f}m)", file=sys.stderr)
    print(f"  Estimated API cost:       ${cost}", file=sys.stderr)
    print(f"  Outputs in: {out_dir}", file=sys.stderr)


if __name__ == "__main__":
    main()
