"""
Delete preprocessed JSON files whose canonical PDF no longer exists.

The structured extractor walks data/eci/for_ai/preprocessed/*.json, so
stale JSONs (from earlier runs of build_ai_extraction_package.py when the
sequence numbering was different) would otherwise inflate the row count
and create phantom duplicate candidates.

Idempotent. Safe to re-run.

USAGE
-----
    python scripts/clean_stale_preprocessed.py
    python scripts/clean_stale_preprocessed.py --dry-run    # show what would be deleted
"""
import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PDF_DIR = ROOT / "data/eci/for_ai/pdfs"
JSON_DIR = ROOT / "data/eci/for_ai/preprocessed"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true",
                    help="Show what would be deleted without deleting")
    args = ap.parse_args()

    if not PDF_DIR.exists():
        sys.exit(f"Canonical PDF folder not found: {PDF_DIR}")
    if not JSON_DIR.exists():
        sys.exit(f"Preprocessed folder not found: {JSON_DIR}")

    pdf_stems = {p.stem for p in PDF_DIR.glob("*.pdf")}
    stale = [
        j for j in JSON_DIR.glob("*.json")
        if not j.name.startswith("_") and j.stem not in pdf_stems
    ]

    if not stale:
        print(f"Nothing to clean. {len(pdf_stems)} canonical PDFs, "
              f"all preprocessed JSONs aligned.")
        return

    verb = "Would delete" if args.dry_run else "Deleting"
    print(f"{verb} {len(stale)} stale preprocessed JSONs:")
    for s in sorted(stale)[:10]:
        print(f"  {s.name}")
    if len(stale) > 10:
        print(f"  ... and {len(stale) - 10} more")

    if not args.dry_run:
        for s in stale:
            s.unlink()
        print(f"\nDeleted {len(stale)} files.")
        remaining = len(list(JSON_DIR.glob("*.json"))) - len(
            list(JSON_DIR.glob("_*.json"))
        )
        print(f"Preprocessed JSONs remaining: {remaining}  "
              f"(should match {len(pdf_stems)} canonical PDFs)")


if __name__ == "__main__":
    main()
