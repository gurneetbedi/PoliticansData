"""
Image-sanitation + EasyOCR preprocessing pipeline for ECI Form 26 affidavits.

For each PDF in --input-dir, this script produces one JSON file with per-page
cleaned text. Pipeline per page:

  1. Try pdfplumber.extract_text(layout=True) — fast path for text-extractable
     pages (no OCR needed). When this yields a substantial text block, we use
     it directly and skip the rest.

  2. Otherwise, render the page to a 300-DPI image, then:
       a) Mask out colored notary stamps (blue, purple/violet, red, green)
          by whitening those pixels in HSV space — they obscure text below.
       b) Convert to high-contrast B&W via Otsu adaptive threshold.
       c) Run EasyOCR with detail=1 to get bounding boxes per text fragment.
       d) Group fragments by Y-coordinate (15px tolerance) into rows.
       e) Sort within a row by X-coordinate and join with " | " delimiters
          so column cells don't bleed together in the linearised text.

  3. Apply PAN-typography repair on the page text (OCR commonly misreads
     PAN trailing letters as digits — e.g. Q→0).

OUTPUT (per PDF):
  data/eci/for_ai/preprocessed/<base_name>.json
    {
      "source_pdf": "005_AKHILESH_PATI_TRIPATHI__1679.pdf",
      "page_count": 34,
      "pages": [
        {"page": 1, "method": "pdfplumber|easyocr", "text": "..."},
        {"page": 2, "method": "easyocr", "text": "..."},
        ...
      ],
      "stats": {"pages_pdfplumber": N, "pages_easyocr": N, "elapsed_seconds": N}
    }

Resumable: skip PDFs whose JSON already exists. Use --refresh to force redo.

DEPENDENCIES (install once):
  pip install easyocr opencv-python pdf2image pdfplumber pillow
  brew install poppler   # for pdf2image
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sys
import time
from pathlib import Path

import numpy as np


# ---------------------------------------------------------------------------
# System-dep + import checks (fail loudly upfront)
# ---------------------------------------------------------------------------

def _check_environment():
    missing = []
    if shutil.which("pdftoppm") is None:
        missing.append("poppler-utils (`brew install poppler`)")
    try:
        import easyocr  # noqa: F401
    except ImportError:
        missing.append("easyocr (`pip install easyocr`)")
    try:
        import cv2  # noqa: F401
    except ImportError:
        missing.append("opencv-python (`pip install opencv-python`)")
    try:
        import pdf2image  # noqa: F401
    except ImportError:
        missing.append("pdf2image (`pip install pdf2image`)")
    try:
        import pdfplumber  # noqa: F401
    except ImportError:
        missing.append("pdfplumber (`pip install pdfplumber`)")
    if missing:
        sys.exit("Missing dependencies:\n  - " + "\n  - ".join(missing))


# ---------------------------------------------------------------------------
# Colour masking: notary stamps are usually blue/violet, but candidates also
# sign in red/green. Whiten all stamp pixels before OCR so text under the
# stamp is recoverable.
# ---------------------------------------------------------------------------

STAMP_HSV_RANGES = [
    (np.array([ 90,  50,  50]), np.array([135, 255, 255])),   # blue
    (np.array([135,  30,  50]), np.array([170, 255, 255])),   # purple / violet
    (np.array([  0,  70,  50]), np.array([ 10, 255, 255])),   # red — low hue wrap
    (np.array([170,  70,  50]), np.array([180, 255, 255])),   # red — high hue wrap
    (np.array([ 35,  50,  50]), np.array([ 85, 255, 255])),   # green
]


def _build_stamp_mask(hsv_image):
    import cv2
    mask = np.zeros(hsv_image.shape[:2], dtype=np.uint8)
    for lo, hi in STAMP_HSV_RANGES:
        mask = cv2.bitwise_or(mask, cv2.inRange(hsv_image, lo, hi))
    return mask


# ---------------------------------------------------------------------------
# PAN-typography repair
# ---------------------------------------------------------------------------

# Standard PAN: 5 letters + 4 digits + 1 letter (e.g. AUFPT4082Q)
# OCR commonly corrupts the trailing letter into a visually similar digit
# (Q↔0/O, S↔5, I↔1/L, B↔8). Restore the most likely intent.
PAN_TRAILING_REPAIR = {
    "0": "Q", "O": "Q",
    "5": "S",
    "1": "I", "L": "I",
    "8": "B",
}
PAN_PATTERN = re.compile(r"\b([A-Z]{5}\d{4})([0-9OQILSB])\b")


def fix_pan_typography(text: str) -> str:
    """OCR-friendly PAN repair. Maps obvious digit↔letter confusions in the
    final character. Idempotent."""
    def _repair(m):
        body, last = m.group(1), m.group(2)
        # If it's already a valid letter, leave it alone
        if last in "ABCDEFGHIJKLMNPRTUVWXYZ":
            return body + last
        return body + PAN_TRAILING_REPAIR.get(last, last)
    return PAN_PATTERN.sub(_repair, text)


# ---------------------------------------------------------------------------
# Per-page pipeline
# ---------------------------------------------------------------------------

# pdfplumber threshold: if the layout-preserving text is shorter than this,
# we don't trust it and fall through to OCR. Tune empirically.
PDFPLUMBER_MIN_CHARS = 150


def extract_via_pdfplumber(pdf_path: Path, page_num: int) -> str | None:
    """Try the digital text layer first. Returns text if useful, None otherwise."""
    import pdfplumber
    try:
        with pdfplumber.open(str(pdf_path)) as pdf:
            page = pdf.pages[page_num - 1]
            text = page.extract_text(layout=True) or ""
            if len(text.strip()) >= PDFPLUMBER_MIN_CHARS:
                return text
    except Exception:
        pass
    return None


def extract_via_easyocr(pdf_path: Path, page_num: int, reader, dpi: int = 300,
                         y_tolerance: int = 15) -> str:
    """Render → mask stamps → OCR → spatial row-group → column-delimit."""
    import cv2
    import pdf2image

    images = pdf2image.convert_from_path(
        str(pdf_path), first_page=page_num, last_page=page_num, dpi=dpi,
    )
    if not images:
        return ""

    # PIL → OpenCV BGR
    rgb = np.array(images[0])
    bgr = rgb[:, :, ::-1].copy()

    # Stamp removal
    hsv = cv2.cvtColor(bgr, cv2.COLOR_BGR2HSV)
    stamp_mask = _build_stamp_mask(hsv)
    bgr[stamp_mask > 0] = [255, 255, 255]

    # B&W with Otsu
    gray = cv2.cvtColor(bgr, cv2.COLOR_BGR2GRAY)
    _, bw = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)

    # OCR with bounding boxes. EasyOCR accepts numpy arrays directly — no
    # need to write a temp PNG.
    ocr_objects = reader.readtext(bw, detail=1)

    # Spatial sort: top-to-bottom, then left-to-right within a row
    ocr_objects.sort(key=lambda x: (x[0][0][1], x[0][0][0]))

    rows: list[str] = []
    current_y = -1
    row_buffer: list[tuple[int, str]] = []

    for box, text, _conf in ocr_objects:
        y = box[0][1]
        x = box[0][0]
        if current_y < 0:
            current_y = y
        if abs(y - current_y) <= y_tolerance:
            row_buffer.append((x, text))
        else:
            row_buffer.sort(key=lambda t: t[0])
            rows.append(" | ".join(t[1] for t in row_buffer))
            row_buffer = [(x, text)]
            current_y = y

    if row_buffer:
        row_buffer.sort(key=lambda t: t[0])
        rows.append(" | ".join(t[1] for t in row_buffer))

    return "\n".join(rows)


# ---------------------------------------------------------------------------
# Per-PDF driver
# ---------------------------------------------------------------------------

def process_pdf(pdf_path: Path, output_path: Path, reader) -> dict:
    import pdf2image

    info = pdf2image.pdfinfo_from_path(str(pdf_path))
    total_pages = int(info["Pages"])

    pages_out = []
    stats = {"pages_pdfplumber": 0, "pages_easyocr": 0}
    start = time.time()

    for p in range(1, total_pages + 1):
        # Fast path
        text = extract_via_pdfplumber(pdf_path, p)
        method = "pdfplumber"
        if text is None:
            text = extract_via_easyocr(pdf_path, p, reader)
            method = "easyocr"
        text = fix_pan_typography(text or "")

        pages_out.append({"page": p, "method": method, "text": text})
        stats[f"pages_{method}"] += 1

    stats["elapsed_seconds"] = round(time.time() - start, 1)

    output = {
        "source_pdf": pdf_path.name,
        "page_count": total_pages,
        "pages": pages_out,
        "stats": stats,
    }
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(output, indent=2, ensure_ascii=False))
    return stats


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                  formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--input-dir", default="data/eci/for_ai/pdfs",
                    help="Folder of PDFs to process")
    ap.add_argument("--output-dir", default="data/eci/for_ai/preprocessed",
                    help="Where to write per-PDF JSON")
    ap.add_argument("--limit", type=int, default=0,
                    help="Process at most N PDFs (smoke test). 0 = no limit.")
    ap.add_argument("--refresh", action="store_true",
                    help="Re-process even if output JSON already exists")
    ap.add_argument("--only", action="append",
                    help="Restrict to specific filenames (can repeat). Smoke test on Akhilesh etc.")
    ap.add_argument("--gpu", action="store_true",
                    help="Enable GPU for EasyOCR if a CUDA card is available")
    args = ap.parse_args()

    _check_environment()
    import easyocr

    in_dir = Path(args.input_dir).resolve()
    out_dir = Path(args.output_dir).resolve()
    if not in_dir.exists():
        sys.exit(f"Input dir not found: {in_dir}")

    pdfs = sorted(in_dir.glob("*.pdf"))
    if args.only:
        keeper = set(args.only)
        pdfs = [p for p in pdfs if p.name in keeper]
    if args.limit:
        pdfs = pdfs[:args.limit]
    if not pdfs:
        sys.exit(f"No PDFs to process in {in_dir}")

    print(f"Initialising EasyOCR (English, gpu={args.gpu}) — first run downloads "
          f"~64 MB of model weights ...", file=sys.stderr)
    reader = easyocr.Reader(["en"], gpu=args.gpu, verbose=False)

    overall = {"pages_pdfplumber": 0, "pages_easyocr": 0, "elapsed_seconds": 0.0}
    skipped = 0

    for i, pdf in enumerate(pdfs, 1):
        out_path = out_dir / f"{pdf.stem}.json"
        if out_path.exists() and not args.refresh:
            print(f"[{i}/{len(pdfs)}] {pdf.name}  (cached, skip)", file=sys.stderr)
            skipped += 1
            continue
        print(f"[{i}/{len(pdfs)}] {pdf.name}  ...", file=sys.stderr, end=" ", flush=True)
        try:
            stats = process_pdf(pdf, out_path, reader)
            for k in ("pages_pdfplumber", "pages_easyocr"):
                overall[k] += stats[k]
            overall["elapsed_seconds"] += stats["elapsed_seconds"]
            print(f"done in {stats['elapsed_seconds']}s "
                  f"(pdfplumber={stats['pages_pdfplumber']}, "
                  f"easyocr={stats['pages_easyocr']})", file=sys.stderr)
        except Exception as e:
            print(f"FAILED: {type(e).__name__}: {e}", file=sys.stderr)

    # Final summary
    print(f"\n========== PREPROCESSING SUMMARY ==========", file=sys.stderr)
    print(f"  PDFs processed:     {len(pdfs) - skipped}", file=sys.stderr)
    print(f"  PDFs skipped (cached): {skipped}", file=sys.stderr)
    print(f"  Pages via pdfplumber (fast path): {overall['pages_pdfplumber']}",
          file=sys.stderr)
    print(f"  Pages via EasyOCR (heavy path):   {overall['pages_easyocr']}",
          file=sys.stderr)
    print(f"  Total elapsed (sum of per-PDF):   {overall['elapsed_seconds']:.0f}s",
          file=sys.stderr)
    print(f"  Outputs in:         {out_dir}", file=sys.stderr)


if __name__ == "__main__":
    main()
