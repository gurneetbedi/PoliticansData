"""
Extract eStamp paper metadata from the cover page of an ECI affidavit PDF.

Every Indian state government issues affidavit eStamp papers with a fixed
header format from the Stock Holding Corporation of India (SHCIL). We use
the e-Stamp Certificate Number as the CANONICAL FILING ID — it's
government-stamped, unique per filing, and survives re-uploads.

Pipeline:
  PDF page 1 -> pdftoppm (PNG) -> Tesseract (eng, psm 6) -> regex extraction

The cover page is text-extractable in some PDFs and scanned in others, so we
always OCR it for reliability. ~1 second per cover page.

Usage:
    python scripts/extract_estamp_metadata.py Affadivit/Affidavit-1780926334.pdf
    python scripts/extract_estamp_metadata.py --all Affadivit/
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import tempfile
from dataclasses import dataclass, asdict
from pathlib import Path


# ---------------------------------------------------------------------------
# OCR — cover page only (fast, ~1s)
# ---------------------------------------------------------------------------

def _ocr_page(pdf_path: Path, page_num: int, dpi: int = 300) -> str:
    """Render a single page to PNG and OCR it. ~1 second."""
    with tempfile.TemporaryDirectory() as tmp:
        wd = Path(tmp)
        subprocess.run(
            ["pdftoppm", "-r", str(dpi), "-f", str(page_num), "-l", str(page_num),
             str(pdf_path), str(wd / "page"), "-png"],
            check=True, capture_output=True,
        )
        png = next(wd.glob("page-*.png"), None)
        if not png:
            return ""
        res = subprocess.run(
            ["tesseract", str(png), "stdout", "-l", "eng", "--psm", "6"],
            check=True, capture_output=True, text=True,
        )
        return res.stdout


def _ocr_cover_page(pdf_path: Path, dpi: int = 300) -> str:
    """OCR the eStamp cover page. Some PDFs have a colored title page before
    the actual eStamp; fall back to pages 2-3 if page 1 lacks the cert."""
    for p in (1, 2, 3):
        text = _ocr_page(pdf_path, p, dpi)
        if ESTAMP_CERT_RE.search(text):
            return text
    # If no cert found anywhere, return page 1's text so caller sees something
    return _ocr_page(pdf_path, 1, dpi)


# ---------------------------------------------------------------------------
# Regex anchors — SHCIL eStamp cover page (standard government layout)
# ---------------------------------------------------------------------------
# Indian state codes appear in the cert number: IN-DL, IN-UP, IN-MH, etc.
ESTAMP_CERT_RE = re.compile(
    r"(?:Certificate\s+No\.?\s*[:.]?\s*)?(IN-[A-Z]{2}\d{14,}[A-Za-z])",
)
ESTAMP_DATE_RE = re.compile(
    # Format: "13-Jan-2025 05:23 PM" — DD-Mon-YYYY HH:MM AM/PM
    r"Certificate\s+Issued\s+Date\s*[:.]?\s*"
    r"(\d{1,2}-[A-Za-z]{3}-\d{4}\s+\d{1,2}:\d{2}\s*[AP]M)",
)
ESTAMP_PURCHASER_RE = re.compile(
    # "Purchased by : <NAME>" — OCR sometimes adds " : " or just "  "
    # Sometimes the name OCRs as garbage if scan is poor — accept any caps.
    r"Purchased\s+by\s*[:.]?\s*([A-Z][A-Z\s\.]{2,60}?)(?:\n|Description)",
)
ESTAMP_FIRST_PARTY_RE = re.compile(
    r"First\s+Party\s*[:.]?\s*([A-Z][A-Z\s\.]{2,60}?)(?:\n|Second)",
)
ESTAMP_DUTY_RE = re.compile(
    r"Stamp\s+Duty\s+Amount\s*\(?\s*Rs\.?\s*\)?\s*[:.]?\s*(\d+)",
)
ESTAMP_DOC_DESC_RE = re.compile(
    r"Description\s+of\s+Document\s*[:.]?\s*(Article\s+\d+\s+\w+)",
    re.IGNORECASE,
)


@dataclass
class EstampMetadata:
    """eStamp paper cover-page metadata. The cert_number is the canonical
    filing ID — anything sharing the same cert_number is the SAME filing."""
    pdf_path: str = ""
    cert_number: str = ""
    issue_date: str = ""
    purchaser: str = ""
    first_party: str = ""
    stamp_duty_rs: int | None = None
    doc_description: str = ""
    confidence: dict = None
    raw_cover_text: str = ""

    def __post_init__(self):
        if self.confidence is None:
            self.confidence = {}


def _strip_artifacts(s: str) -> str:
    """Tidy up OCR garbage at the start/end of captured strings."""
    s = s.strip(" :.\n\r\t|")
    # Collapse multi-space
    s = re.sub(r"\s{2,}", " ", s)
    return s.strip()


def extract_from_cover_text(cover_text: str) -> EstampMetadata:
    """Extract eStamp metadata from OCR'd cover-page text."""
    m = EstampMetadata(raw_cover_text=cover_text[:1500])

    if r := ESTAMP_CERT_RE.search(cover_text):
        # eStamp cert numbers always end with a single letter; OCR sometimes
        # lower-cases the trailing letter. Normalize to upper for stable IDs.
        cert = r.group(1)
        cert = cert[:-1] + cert[-1].upper()
        m.cert_number = cert
        m.confidence["cert_number"] = "high"

    if r := ESTAMP_DATE_RE.search(cover_text):
        m.issue_date = _strip_artifacts(r.group(1))
        m.confidence["issue_date"] = "high"

    if r := ESTAMP_PURCHASER_RE.search(cover_text):
        purchaser = _strip_artifacts(r.group(1))
        # Reject obvious OCR garbage (single character, all spaces, etc.)
        if len(purchaser) >= 3 and any(c.isalpha() for c in purchaser):
            m.purchaser = purchaser
            m.confidence["purchaser"] = "medium"

    if r := ESTAMP_FIRST_PARTY_RE.search(cover_text):
        first = _strip_artifacts(r.group(1))
        if len(first) >= 3 and any(c.isalpha() for c in first):
            m.first_party = first
            m.confidence["first_party"] = "medium"

    if r := ESTAMP_DUTY_RE.search(cover_text):
        try:
            m.stamp_duty_rs = int(r.group(1))
            m.confidence["stamp_duty_rs"] = "high"
        except ValueError:
            pass

    if r := ESTAMP_DOC_DESC_RE.search(cover_text):
        m.doc_description = _strip_artifacts(r.group(1))
        m.confidence["doc_description"] = "high"

    return m


def extract(pdf_path: Path) -> EstampMetadata:
    """Full pipeline — OCR + extract."""
    cover_text = _ocr_cover_page(pdf_path)
    meta = extract_from_cover_text(cover_text)
    meta.pdf_path = str(pdf_path)
    return meta


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("path", help="PDF file or directory")
    ap.add_argument("--all", action="store_true",
                    help="Treat path as a directory and process all *.pdf in it")
    ap.add_argument("--include-raw", action="store_true",
                    help="Include the raw OCR text in the output JSON")
    args = ap.parse_args()

    path = Path(args.path).resolve()

    if args.all or path.is_dir():
        targets = sorted(path.glob("*.pdf"))
    else:
        targets = [path]

    if not targets:
        sys.exit(f"No PDFs at {path}")

    results = []
    for t in targets:
        print(f"  extracting {t.name} ...", file=sys.stderr)
        meta = extract(t)
        d = asdict(meta)
        if not args.include_raw:
            d.pop("raw_cover_text", None)
        results.append(d)

    print(json.dumps(results, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
