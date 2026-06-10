"""
Quality-check the per-PDF preprocessed JSONs from preprocess_eci_pdfs.py.

For each candidate, scan the cleaned page text for key markers that should
be present in any well-extracted Form 26:

  - Candidate NAME (matched against the filename prefix)
  - PAN format (5 letters + 4 digits + 1 letter)
  - Party name token (AAP / BJP / INC / INDEPENDENT / etc., or any
    Hindi-loaded party word like "PARTY", "DAL", "MORCHA")
  - Currency-format token (Rs. or ₹ followed by digits)
  - "Part B" or "ABSTRACT" header — indicates the financial summary page

For each candidate, tags the run as:

  CLEAN     — all key markers found, looks production-ready
  LIGHT     — most markers found, 1-2 missing (worth a quick look)
  FLAG      — multiple markers missing, escalate to LLM or manual review
  EMPTY     — nearly no usable text extracted, definitely escalate

Writes a CSV report (data/eci/for_ai/preprocessed/_qc_report.csv) plus a
console summary. Use the FLAG/EMPTY rows to decide whether the cheap
EasyOCR-only pipeline is enough or you need to spend the LLM tax.
"""
from __future__ import annotations

import argparse
import csv
import json
import re
import sys
from pathlib import Path


# ---------------------------------------------------------------------------
# Marker detectors
# ---------------------------------------------------------------------------

PAN_RE = re.compile(r"\b[A-Z]{5}\d{4}[A-Z]\b")
RS_RE = re.compile(r"(?:Rs\.?|₹|RS\.?)\s*[\d,]{3,}")
PARTY_TOKENS = (
    "AAM AADMI", "BHARATIYA JANATA", "INDIAN NATIONAL CONGRESS",
    "INDEPENDENT", "JANATA", "DAL", "MORCHA", "SAMAJWADI", "MAJLIS",
    "BAHUJAN", "RASHTRIYA", "PARTY",
)
PART_B_TOKENS = ("PART-B", "PART - B", "PART B", "ABSTRACT OF THE DETAILS")


def join_text(payload: dict) -> str:
    """Concatenate every page's text into one searchable string."""
    return "\n".join(p.get("text", "") for p in payload.get("pages", []))


def candidate_name_from_filename(filename: str) -> str:
    """'005_AKHILESH_PATI_TRIPATHI__1679.json' -> 'AKHILESH PATI TRIPATHI'"""
    stem = Path(filename).stem
    if "__" not in stem:
        return ""
    name_part = stem.rsplit("__", 1)[0]
    # Drop the leading sequence number
    parts = name_part.split("_", 1)
    if len(parts) == 2 and parts[0].isdigit():
        name_part = parts[1]
    return name_part.replace("_", " ")


def name_match(candidate_name: str, text: str) -> bool:
    """Loose match: at least 60% of the words in the filename name appear
    in the extracted text. Tolerates OCR mangling of one or two characters."""
    if not candidate_name:
        return False
    words = [w for w in candidate_name.upper().split() if len(w) >= 3]
    if not words:
        return False
    upper_text = text.upper()
    hits = sum(1 for w in words if w in upper_text)
    return hits / len(words) >= 0.6


def party_match(text: str) -> bool:
    upper = text.upper()
    return any(token in upper for token in PARTY_TOKENS)


def part_b_match(text: str) -> bool:
    upper = text.upper()
    return any(token in upper for token in PART_B_TOKENS)


# ---------------------------------------------------------------------------
# Scoring
# ---------------------------------------------------------------------------

def evaluate(payload: dict) -> dict:
    """Return a row of QC results for one candidate."""
    name = candidate_name_from_filename(payload["source_pdf"])
    text = join_text(payload)
    text_len = len(text)

    markers = {
        "name_found":    name_match(name, text),
        "pan_found":     bool(PAN_RE.search(text)),
        "party_found":   party_match(text),
        "currency_found": bool(RS_RE.search(text)),
        "part_b_found":  part_b_match(text),
    }
    hits = sum(markers.values())

    if text_len < 500:
        status = "EMPTY"
    elif hits >= 5:
        status = "CLEAN"
    elif hits >= 4:
        status = "LIGHT"
    else:
        status = "FLAG"

    return {
        "source_pdf": payload["source_pdf"],
        "candidate_name": name,
        "status": status,
        "marker_hits": f"{hits}/5",
        "text_chars": text_len,
        "page_count": payload.get("page_count", 0),
        "pages_pdfplumber": payload.get("stats", {}).get("pages_pdfplumber", 0),
        "pages_easyocr": payload.get("stats", {}).get("pages_easyocr", 0),
        "elapsed_seconds": payload.get("stats", {}).get("elapsed_seconds", 0),
        **{k: int(v) for k, v in markers.items()},
        "pan_sample": (PAN_RE.search(text).group(0) if PAN_RE.search(text) else ""),
        "rs_sample": (RS_RE.search(text).group(0) if RS_RE.search(text) else ""),
    }


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--preprocessed-dir", default="data/eci/for_ai/preprocessed")
    ap.add_argument("--out", default="data/eci/for_ai/preprocessed/_qc_report.csv")
    args = ap.parse_args()

    in_dir = Path(args.preprocessed_dir).resolve()
    if not in_dir.exists():
        sys.exit(f"Preprocessed dir not found: {in_dir}")

    files = sorted(f for f in in_dir.glob("*.json")
                    if not f.name.startswith("_"))
    if not files:
        sys.exit(f"No preprocessed JSONs in {in_dir}")

    rows: list[dict] = []
    for f in files:
        try:
            payload = json.loads(f.read_text())
        except Exception as e:
            print(f"  bad JSON in {f.name}: {e}", file=sys.stderr)
            continue
        rows.append(evaluate(payload))

    # Write CSV
    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w", newline="") as fp:
        if not rows:
            sys.exit("No rows to write")
        writer = csv.DictWriter(fp, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)
    print(f"Wrote {out_path}", file=sys.stderr)

    # Summary
    from collections import Counter
    counts = Counter(r["status"] for r in rows)
    total = len(rows)
    print(f"\n========== QC SUMMARY ({total} candidates) ==========", file=sys.stderr)
    for status in ("CLEAN", "LIGHT", "FLAG", "EMPTY"):
        n = counts.get(status, 0)
        pct = (n / total * 100) if total else 0
        bar = "█" * int(pct / 2)
        print(f"  {status:6s}  {n:4d}  ({pct:5.1f}%)  {bar}", file=sys.stderr)

    flagged = [r for r in rows if r["status"] in ("FLAG", "EMPTY")]
    if flagged:
        print(f"\nFLAGGED / EMPTY candidates (consider LLM escalation):",
              file=sys.stderr)
        for r in flagged[:20]:
            print(f"  {r['status']:5s}  {r['candidate_name'][:35]:35s}  "
                  f"hits={r['marker_hits']}  chars={r['text_chars']:>6d}",
                  file=sys.stderr)
        if len(flagged) > 20:
            print(f"  ... and {len(flagged)-20} more (see CSV)", file=sys.stderr)


if __name__ == "__main__":
    main()
