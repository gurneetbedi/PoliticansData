"""
Build an AI-agnostic extraction package from the downloaded ECI PDFs.

OUTPUT LAYOUT (under data/eci/for_ai/):
  pdfs/           — one canonical PDF per candidate, renamed
                    "<seq>_<NAME>__<affidavit_id>.pdf"
  index.csv       — candidate roster (one row per candidate)
  index.json      — same data as JSON
  README.md       — how to use this package with any AI

This script does NOT call any AI. It just prepares the inputs so the user
can drag PDFs into Claude / ChatGPT / Gemini / Mistral etc. and paste the
shared prompt at scripts/../prompts/extraction_prompt.md.
"""
from __future__ import annotations

import csv
import json
import shutil
import sys
from collections import defaultdict
from pathlib import Path


SOURCE = Path("data/eci/raw_pdfs/delhi-2025/raw_pdfs")
OUT = Path("data/eci/for_ai")
OUT_PDFS = OUT / "pdfs"


def candidate_key(filename: str) -> str:
    """Return the candidate-name prefix from a fetcher-written filename.
    'AKHILESH_PATI_TRIPATHI__1679.pdf' -> 'AKHILESH_PATI_TRIPATHI'
    """
    stem = Path(filename).stem
    if "__" not in stem:
        return stem
    return stem.rsplit("__", 1)[0]


def affidavit_id(filename: str) -> str:
    """Pull the trailing affidavit_id from a fetcher-written filename."""
    stem = Path(filename).stem
    if "__" not in stem:
        return ""
    return stem.rsplit("__", 1)[1]


def main():
    if not SOURCE.exists():
        sys.exit(f"Source folder missing: {SOURCE}")

    pdfs = sorted(p for p in SOURCE.glob("*.pdf"))
    if not pdfs:
        sys.exit(f"No PDFs in {SOURCE}")

    # Group by candidate name prefix
    by_candidate: dict[str, list[Path]] = defaultdict(list)
    for p in pdfs:
        name = candidate_key(p.name)
        if not name:
            # Filename was '__NNNN.pdf' (empty name from early smoke test)
            continue
        by_candidate[name].append(p)

    # Reset output PDFs folder
    if OUT_PDFS.exists():
        shutil.rmtree(OUT_PDFS)
    OUT_PDFS.mkdir(parents=True, exist_ok=True)

    rows = []
    seq = 0
    for name, files in sorted(by_candidate.items()):
        seq += 1
        # Canonical = largest file (most pages = most complete affidavit)
        files_sorted = sorted(files, key=lambda f: f.stat().st_size, reverse=True)
        canonical = files_sorted[0]
        affid = affidavit_id(canonical.name)

        new_name = f"{seq:03d}_{name}__{affid}.pdf"
        target = OUT_PDFS / new_name
        shutil.copy2(canonical, target)

        rows.append({
            "seq": seq,
            "candidate_name": name.replace("_", " "),
            "filename": new_name,
            "canonical_source": canonical.name,
            "affidavit_id": affid,
            "size_bytes": canonical.stat().st_size,
            "size_mb": round(canonical.stat().st_size / 1e6, 2),
            "other_affidavits": [
                f.name for f in files_sorted[1:]
            ],
            "other_affidavit_count": len(files_sorted) - 1,
        })

    # Write index.csv (flat for quick scanning)
    OUT.mkdir(parents=True, exist_ok=True)
    csv_path = OUT / "index.csv"
    with csv_path.open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow([
            "seq", "candidate_name", "filename", "affidavit_id",
            "size_mb", "other_affidavit_count", "other_affidavits",
        ])
        for r in rows:
            w.writerow([
                r["seq"], r["candidate_name"], r["filename"],
                r["affidavit_id"], r["size_mb"],
                r["other_affidavit_count"],
                "; ".join(r["other_affidavits"]),
            ])

    # Write index.json (richer, for programmatic use)
    json_path = OUT / "index.json"
    json_path.write_text(json.dumps(rows, indent=2, ensure_ascii=False))

    # Stats
    multi = [r for r in rows if r["other_affidavit_count"] > 0]
    print(f"Wrote {len(rows)} canonical PDFs to {OUT_PDFS}", file=sys.stderr)
    print(f"  {len(multi)} candidates had multiple affidavits (canonical = largest)",
          file=sys.stderr)
    print(f"Wrote {csv_path}", file=sys.stderr)
    print(f"Wrote {json_path}", file=sys.stderr)


if __name__ == "__main__":
    main()
