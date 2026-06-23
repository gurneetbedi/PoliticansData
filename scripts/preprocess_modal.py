"""
Modal version of preprocess_eci_pdfs.py — runs the EasyOCR + CV
preprocessing across many parallel containers in the cloud instead of
sequentially on your Mac.

100 PDFs that would take ~5 hours on a single Mac CPU finish in
~2 minutes on Modal because each container processes one PDF in
parallel.

USAGE
-----
    # One-time setup
    pip install modal
    modal token new                # opens browser to sign up / log in

    # Verify what's already preprocessed locally — Modal skips these
    ls data/eci/for_ai/preprocessed/*.json | wc -l

    # Smoke test on 2 PDFs first
    modal run scripts/preprocess_modal.py --limit 2

    # Once happy, full run on whatever's still missing
    modal run scripts/preprocess_modal.py

    # Force re-run everything (ignore cached outputs)
    modal run scripts/preprocess_modal.py --refresh

Results land in the same local folder as the Mac version:
  data/eci/for_ai/preprocessed/<base_name>.json

So downstream scripts (qc_preprocessed.py, extract_via_llm.py) work
identically — no other code needs to change.

COST
----
Modal's free tier is $30 / month. Processing all 104 Delhi PDFs costs
roughly $0.50 — comfortably inside the free tier. Pan-India scale
(~17k candidates) would be roughly $80, still inside reasonable limits.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import modal


# ---------------------------------------------------------------------------
# Modal app + image definition
# ---------------------------------------------------------------------------

app = modal.App("eci-preprocess")

# Container image: Debian slim + poppler (for pdf2image) + pinned Python deps
#
# Why every version is pinned:
#   - Latest torch + sympy 1.13 have a circular import bug
#     (AttributeError: module 'sympy' has no attribute 'core')
#   - Latest torchvision + torch combination triggers a partially-initialized
#     module error
#   - numpy 2.x breaks several downstream libraries' compiled wheels
#   Locking these to known-good versions = stable container builds.
#
# Why we pre-cache EasyOCR models:
#   Without this, every Modal container tries to download the same ~64MB
#   model files at startup. When you run 50 in parallel, the model server
#   resets connections and most containers crash with corrupted MD5 hashes.
#   .run_function() executes during *image build*, downloading the models
#   ONCE into the image filesystem. Every container then loads from local
#   disk — instant, no network race.


def _prefetch_easyocr_models():
    """Run at image build time so the OCR model weights are baked into the
    Docker layer instead of being downloaded by each container."""
    import easyocr
    easyocr.Reader(["en"], gpu=False, verbose=False)


image = (
    modal.Image.debian_slim(python_version="3.11")
    .apt_install("poppler-utils")
    .pip_install(
        "torch==2.3.1",
        "torchvision==0.18.1",
        "sympy==1.12",
        "easyocr==1.7.1",
        "opencv-python-headless==4.10.0.84",
        "pdf2image==1.17.0",
        "pdfplumber==0.11.4",
        "pillow==10.4.0",
        "numpy<2",
    )
    .run_function(_prefetch_easyocr_models)
)


# ---------------------------------------------------------------------------
# Remote function — runs once per PDF in its own container
# ---------------------------------------------------------------------------

@app.function(
    image=image,
    cpu=4.0,                  # CPU-only — the image doesn't bundle CUDA libraries
    memory=8192,
    timeout=900,
    # Reduced from 50 → 20 after the 2020 run saw 779/1014 failures with 50
    # parallel containers. The free tier appears to throttle hard at higher
    # concurrency. 20 takes ~2.5× longer wall time but lands more reliably.
    # Raise back to 50 once we've confirmed the failures weren't scheduling.
    max_containers=20,
    # Auto-retry once on transient infrastructure errors. Each retry uses a
    # fresh container, so a flaky worker doesn't permanently lose a PDF.
    retries=modal.Retries(max_retries=2, backoff_coefficient=2.0,
                            initial_delay=10.0),
)
def process_pdf_remote(pdf_bytes: bytes, filename: str) -> dict:
    """Render → mask stamps → OCR → spatial sort → column-delimit.

    Mirrors the logic in scripts/preprocess_eci_pdfs.py but inlined so
    the function has no local-machine dependencies.
    """
    import re
    import tempfile
    import time
    from pathlib import Path

    import cv2
    import easyocr
    import numpy as np
    import pdf2image
    import pdfplumber

    # --- HSV colour ranges for stamp removal -------------------------------
    STAMP_RANGES = [
        (np.array([ 90,  50,  50]), np.array([135, 255, 255])),   # blue
        (np.array([135,  30,  50]), np.array([170, 255, 255])),   # purple/violet
        (np.array([  0,  70,  50]), np.array([ 10, 255, 255])),   # red low
        (np.array([170,  70,  50]), np.array([180, 255, 255])),   # red high
        (np.array([ 35,  50,  50]), np.array([ 85, 255, 255])),   # green
    ]

    PAN_RE = re.compile(r"\b([A-Z]{5}\d{4})([0-9OQILSB])\b")
    PAN_REPAIR = {"0": "Q", "O": "Q", "5": "S", "1": "I", "L": "I", "8": "B"}

    def fix_pan(text: str) -> str:
        def _r(m):
            body, last = m.group(1), m.group(2)
            if last in "ABCDEFGHIJKLMNPRTUVWXYZ":
                return body + last
            return body + PAN_REPAIR.get(last, last)
        return PAN_RE.sub(_r, text)

    # --- Write PDF bytes to a temp file ------------------------------------
    with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as f:
        f.write(pdf_bytes)
        pdf_path = Path(f.name)

    # --- pdfplumber fast path ---------------------------------------------
    def try_pdfplumber(page_num: int) -> str | None:
        try:
            with pdfplumber.open(str(pdf_path)) as pdf:
                text = pdf.pages[page_num - 1].extract_text(layout=True) or ""
                if len(text.strip()) >= 150:
                    return text
        except Exception:
            return None
        return None

    # --- EasyOCR setup (model load happens once per container) -------------
    # CPU mode — matches the @app.function decorator above. EasyOCR will
    # auto-detect GPU if one is attached, so this stays safe if you ever
    # swap back to gpu="T4" after fixing the CUDA-image setup.
    reader = easyocr.Reader(["en"], gpu=False, verbose=False)

    # --- Render each page + extract ---------------------------------------
    # Some affidavits in the archive are corrupted (truncated download,
    # broken xref table, scanned PDF saved without a proper /Pages tree).
    # poppler-utils' pdfinfo bails out hard on these with PDFPageCountError.
    # If we let that propagate, Modal's `starmap(return_exceptions=False)`
    # cancels every other in-flight task — one bad PDF stops the whole run.
    # Catch it here, return a structured error so the caller can log it
    # and continue.
    try:
        info = pdf2image.pdfinfo_from_path(str(pdf_path))
        total_pages = int(info["Pages"])
    except Exception as e:
        # The function parameter is `filename`, not `pdf_name`. Earlier
        # version of this block had the wrong name and raised NameError,
        # which masked the corrupt-PDF case under a completely unrelated
        # error type. Use `filename` (matches the signature above).
        return {
            "source_pdf":   filename,
            "page_count":   0,
            "pages":        [],
            "stats":        {"pages_pdfplumber": 0, "pages_easyocr": 0,
                              "elapsed_seconds": 0,
                              "error": f"{type(e).__name__}: {str(e)[:200]}"},
            "corrupt":      True,
        }

    pages_out = []
    stats = {"pages_pdfplumber": 0, "pages_easyocr": 0}
    t_start = time.time()

    for p in range(1, total_pages + 1):
        text = try_pdfplumber(p)
        method = "pdfplumber"
        if text is None:
            # OCR fallback
            images = pdf2image.convert_from_path(
                str(pdf_path), first_page=p, last_page=p, dpi=300,
            )
            if not images:
                text = ""
            else:
                rgb = np.array(images[0])
                bgr = rgb[:, :, ::-1].copy()
                hsv = cv2.cvtColor(bgr, cv2.COLOR_BGR2HSV)
                mask = np.zeros(hsv.shape[:2], dtype=np.uint8)
                for lo, hi in STAMP_RANGES:
                    mask = cv2.bitwise_or(mask, cv2.inRange(hsv, lo, hi))
                bgr[mask > 0] = [255, 255, 255]
                gray = cv2.cvtColor(bgr, cv2.COLOR_BGR2GRAY)
                _, bw = cv2.threshold(
                    gray, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU
                )

                ocr_objects = reader.readtext(bw, detail=1)
                ocr_objects.sort(key=lambda x: (x[0][0][1], x[0][0][0]))

                rows = []
                current_y = -1
                row_buffer = []
                Y_TOL = 15
                for box, t, _conf in ocr_objects:
                    y, x = box[0][1], box[0][0]
                    if current_y < 0:
                        current_y = y
                    if abs(y - current_y) <= Y_TOL:
                        row_buffer.append((x, t))
                    else:
                        row_buffer.sort(key=lambda r: r[0])
                        rows.append(" | ".join(r[1] for r in row_buffer))
                        row_buffer = [(x, t)]
                        current_y = y
                if row_buffer:
                    row_buffer.sort(key=lambda r: r[0])
                    rows.append(" | ".join(r[1] for r in row_buffer))
                text = "\n".join(rows)
            method = "easyocr"

        text = fix_pan(text or "")
        pages_out.append({"page": p, "method": method, "text": text})
        stats[f"pages_{method}"] += 1

    stats["elapsed_seconds"] = round(time.time() - t_start, 1)

    return {
        "source_pdf": filename,
        "page_count": total_pages,
        "pages": pages_out,
        "stats": stats,
    }


# ---------------------------------------------------------------------------
# Local entrypoint — what runs when you invoke `modal run scripts/...`
# ---------------------------------------------------------------------------

@app.local_entrypoint()
def main(refresh: bool = False, limit: int = 0,
         pdf_dir: str = "", out_dir: str = ""):
    """Fan PDFs out to Modal containers and write results back to local disk.

    Args:
        refresh: re-process even if local output already exists
        limit:   only process the first N missing PDFs (smoke test)
        pdf_dir: input directory of PDFs (default: data/eci/for_ai/pdfs).
                 Pass an absolute or project-relative path to process a
                 different election cycle, e.g.:
                   --pdf-dir data/eci/raw_pdfs/delhi-2020/raw_pdfs
        out_dir: output directory for the per-PDF JSON results (default:
                 data/eci/for_ai/preprocessed). Use a per-cycle dir to
                 avoid mixing cycles, e.g.:
                   --out-dir data/eci/for_ai/preprocessed_delhi_2020
    """
    project_root = Path(__file__).resolve().parent.parent
    _pdf_dir = Path(pdf_dir) if pdf_dir else (project_root / "data/eci/for_ai/pdfs")
    _out_dir = Path(out_dir) if out_dir else (project_root / "data/eci/for_ai/preprocessed")
    if not _pdf_dir.is_absolute():
        _pdf_dir = project_root / _pdf_dir
    if not _out_dir.is_absolute():
        _out_dir = project_root / _out_dir
    pdf_dir = _pdf_dir
    out_dir = _out_dir
    out_dir.mkdir(parents=True, exist_ok=True)
    print(f"Input dir:  {pdf_dir}")
    print(f"Output dir: {out_dir}")

    if not pdf_dir.exists():
        sys.exit(f"Input dir missing: {pdf_dir}")

    pdfs = sorted(pdf_dir.glob("*.pdf"))
    if not pdfs:
        sys.exit(f"No PDFs in {pdf_dir}")

    todo = []
    for pdf in pdfs:
        out = out_dir / f"{pdf.stem}.json"
        if out.exists() and not refresh:
            continue
        todo.append(pdf)

    if limit:
        todo = todo[:limit]
    if not todo:
        print("Everything is already preprocessed — nothing to do.")
        return

    print(f"Fanning out {len(todo)} PDFs to Modal containers ...")
    # Build (bytes, filename) tuples
    inputs = [(p.read_bytes(), p.name) for p in todo]

    # .starmap fans out across many parallel containers and collects results
    # in order. `return_exceptions=True` ensures that a single broken PDF
    # (corrupt xref, unreadable header, etc.) doesn't cancel the whole map —
    # we get the exception object back as a normal result and keep going.
    succeeded = failed = corrupt = 0
    for pdf, result in zip(todo, process_pdf_remote.starmap(
            inputs, return_exceptions=True)):
        out_path = out_dir / f"{pdf.stem}.json"
        if isinstance(result, Exception):
            print(f"  ✗ {pdf.name}  {type(result).__name__}: {result}")
            failed += 1
            continue
        # Worker reported the PDF was unparseable (caught in-worker).
        if result.get("corrupt"):
            print(f"  ⚠ {pdf.name}  corrupt — "
                  f"{result.get('stats',{}).get('error','no detail')}")
            # Still write the JSON sentinel so downstream loaders can
            # decide what to do (skip vs flag-for-review).
            out_path.write_text(json.dumps(result, indent=2, ensure_ascii=False))
            corrupt += 1
            continue
        out_path.write_text(json.dumps(result, indent=2, ensure_ascii=False))
        stats = result.get("stats", {})
        print(f"  ✓ {pdf.name}  "
              f"(pdfplumber={stats.get('pages_pdfplumber', 0)}, "
              f"easyocr={stats.get('pages_easyocr', 0)}, "
              f"{stats.get('elapsed_seconds', 0)}s)")
        succeeded += 1

    print(f"\nDone — {succeeded} succeeded, {corrupt} corrupt (skipped), "
          f"{failed} other failures.")
    print(f"Outputs in: {out_dir}")
