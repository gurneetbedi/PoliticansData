"""
One-PDF smoke test for the Google Cloud Vision OCR setup.

PURPOSE
=======
Validate three things in roughly 10 seconds without spending credits beyond
~15 pages worth (~$0.02):
  1. Your GOOGLE_APPLICATION_CREDENTIALS env var points at a working key
  2. The Cloud Vision API is enabled in your project
  3. The output JSON shape is what we expect for the planned pipeline

Picks one Delhi 2020 PDF at random, calls
`document_text_detection` (the high-quality mode optimized for multi-page
documents), and prints a summary plus a snippet of extracted text.

Run this BEFORE rewriting the full preprocessing pipeline — if anything
about credentials or quotas is off, you'd rather find out on one PDF than
on 670.

USAGE
=====
    export GOOGLE_APPLICATION_CREDENTIALS="$HOME/.gcp/lokvani-vision-key.json"
    cd "/path/to/Politicians Project"
    source .venv-eci/bin/activate

    pip install google-cloud-vision   # one-time
    python scripts/cloud_vision_smoke_test.py
"""
from __future__ import annotations

import os
import random
import sys
from pathlib import Path


def main():
    if not os.environ.get("GOOGLE_APPLICATION_CREDENTIALS"):
        sys.exit(
            "GOOGLE_APPLICATION_CREDENTIALS env var is not set.\n"
            "Add this line to ~/.zshrc and re-source it:\n"
            '   export GOOGLE_APPLICATION_CREDENTIALS="$HOME/.gcp/lokvani-vision-key.json"'
        )

    try:
        from google.cloud import vision
    except ImportError:
        sys.exit(
            "google-cloud-vision is not installed. From .venv-eci, run:\n"
            "    pip install google-cloud-vision"
        )

    project_root = Path(__file__).resolve().parent.parent
    pdf_dir = project_root / "data/eci/raw_pdfs/delhi-2020/raw_pdfs"
    if not pdf_dir.exists():
        sys.exit(f"PDF dir not found: {pdf_dir}")

    pdfs = sorted(pdf_dir.glob("*.pdf"))
    if not pdfs:
        sys.exit(f"No PDFs in {pdf_dir}")

    # Deterministic pick so re-running gives consistent results
    random.seed(42)
    pdf = random.choice(pdfs)
    print(f"Picked: {pdf.name}", file=sys.stderr)
    print(f"Size:   {pdf.stat().st_size:,} bytes", file=sys.stderr)

    # Read PDF bytes
    pdf_bytes = pdf.read_bytes()

    # Build the request — DOCUMENT_TEXT_DETECTION + application/pdf MIME.
    # Vision's sync API processes up to 5 pages per request. For multi-page
    # PDFs, use AsyncBatchAnnotate (more on this in the production pipeline).
    # For a smoke test, capping at 5 pages is fine.
    client = vision.ImageAnnotatorClient()
    request = {
        "input_config": {
            "content": pdf_bytes,
            "mime_type": "application/pdf",
        },
        "features": [{"type_": vision.Feature.Type.DOCUMENT_TEXT_DETECTION}],
        # Pages to OCR. 1-indexed. Vision sync mode caps at 5.
        "pages": [1, 2, 3, 4, 5],
    }

    print("Calling Cloud Vision (this hits the network, ~3-8 seconds)...",
          file=sys.stderr)

    try:
        responses = client.batch_annotate_files(requests=[request])
    except Exception as e:
        sys.exit(
            f"\nVision API call failed:\n  {type(e).__name__}: {e}\n\n"
            "Common causes:\n"
            "  - Cloud Vision API not enabled in your project\n"
            "  - Service account missing 'Cloud Vision API User' role\n"
            "  - Billing not linked to project (Vision requires linked billing\n"
            "    even on free trial)\n"
        )

    file_response = responses.responses[0]
    n_pages_returned = len(file_response.responses)
    total_chars = 0
    first_page_preview = ""

    for i, page_response in enumerate(file_response.responses, 1):
        if page_response.error.message:
            print(f"  Page {i}: ERROR: {page_response.error.message}",
                  file=sys.stderr)
            continue
        text = page_response.full_text_annotation.text or ""
        total_chars += len(text)
        if i == 1:
            first_page_preview = text[:800]

    print("\n" + "=" * 60, file=sys.stderr)
    print("✓ SUCCESS", file=sys.stderr)
    print(f"  Pages OCR'd:     {n_pages_returned}", file=sys.stderr)
    print(f"  Total chars:     {total_chars:,}", file=sys.stderr)
    print(f"  Cost so far:     ~${0.0015 * n_pages_returned:.4f}",
          file=sys.stderr)
    print("=" * 60, file=sys.stderr)
    print("\nPage 1 text preview (first 800 chars):", file=sys.stderr)
    print("-" * 60, file=sys.stderr)
    print(first_page_preview, file=sys.stderr)


if __name__ == "__main__":
    main()
