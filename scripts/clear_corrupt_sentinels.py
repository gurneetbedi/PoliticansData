"""Delete corrupt-PDF sentinel JSONs (and their broken source PDFs) so
the refetch + Cloud Vision pipeline will re-attempt them from scratch.

A sentinel is any file in preprocessed_<state>/ with `"corrupt": true`
in its JSON. Cloud Vision skips these on re-run (that's why re-running
says "nothing to do"), so we clear them first.

Usage:
    # Dry run first
    python scripts/clear_corrupt_sentinels.py \\
        --preprocessed data/eci/for_ai/preprocessed_uttarpradesh_2022 \\
        --raw-dir data/eci/raw_pdfs/uttarpradesh-2022/raw_pdfs

    # Commit
    python scripts/clear_corrupt_sentinels.py \\
        --preprocessed data/eci/for_ai/preprocessed_uttarpradesh_2022 \\
        --raw-dir data/eci/raw_pdfs/uttarpradesh-2022/raw_pdfs \\
        --commit

Then re-run scan → refetch → cloud_vision_preprocess in that order.
"""
from __future__ import annotations
import argparse
import json
import sys
from pathlib import Path


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                  formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--preprocessed", required=True,
                    help="Path to preprocessed_<state>/ directory")
    ap.add_argument("--raw-dir", required=True,
                    help="Path to raw_pdfs/<state>/raw_pdfs/ (source PDFs)")
    ap.add_argument("--keep-pdf", action="store_true",
                    help="Delete only the sentinel JSON, keep the source PDF "
                         "(default: delete both so refetch will re-download)")
    ap.add_argument("--commit", action="store_true",
                    help="Actually delete files (default: dry-run report only)")
    args = ap.parse_args()

    pp = Path(args.preprocessed)
    raw = Path(args.raw_dir)
    if not pp.exists():
        sys.exit(f"Missing: {pp}")
    if not raw.exists():
        sys.exit(f"Missing: {raw}")

    sentinels: list[tuple[Path, Path]] = []   # (json_path, pdf_path)
    for jf in pp.iterdir():
        if jf.suffix != ".json" or jf.name.startswith("_"):
            continue
        try:
            r = json.loads(jf.read_text())
        except Exception:
            continue
        if r.get("corrupt") or r.get("skipped_corrupt"):
            # The source PDF has the same stem + .pdf, in raw_dir
            pdf = raw / (jf.stem + ".pdf")
            sentinels.append((jf, pdf))

    if not sentinels:
        sys.exit("No corrupt sentinels found. Nothing to clear.")

    print(f"Corrupt sentinels found in {pp.name}: {len(sentinels):,}",
          file=sys.stderr)
    pdf_present = sum(1 for _, p in sentinels if p.exists())
    print(f"  Source PDFs still on disk: {pdf_present:,}", file=sys.stderr)
    print(f"  Source PDFs already gone:  {len(sentinels) - pdf_present:,}",
          file=sys.stderr)

    print(f"\nSample (first 5):", file=sys.stderr)
    for j, p in sentinels[:5]:
        print(f"  json: {j.name}", file=sys.stderr)
        print(f"  pdf:  {p.name}  {'(exists)' if p.exists() else '(missing)'}",
              file=sys.stderr)

    if not args.commit:
        action = "sentinel JSON only" if args.keep_pdf else "sentinel JSON + source PDF"
        print(f"\n--- DRY RUN. Would delete: {action}", file=sys.stderr)
        print(f"Re-run with --commit to apply.", file=sys.stderr)
        return

    deleted_j = deleted_p = 0
    for j, p in sentinels:
        try:
            j.unlink()
            deleted_j += 1
        except Exception as e:
            print(f"  ✗ failed to delete {j.name}: {e}", file=sys.stderr)
        if not args.keep_pdf and p.exists():
            try:
                p.unlink()
                deleted_p += 1
            except Exception as e:
                print(f"  ✗ failed to delete {p.name}: {e}", file=sys.stderr)

    print(f"\n✓ Cleared:", file=sys.stderr)
    print(f"   Sentinel JSONs: {deleted_j:,}", file=sys.stderr)
    if not args.keep_pdf:
        print(f"   Source PDFs:    {deleted_p:,}", file=sys.stderr)

    print(f"\nNext:", file=sys.stderr)
    print(f"  1. python scripts/scan_corrupt_pdfs.py --cycle uttarpradesh-2022 --fast",
          file=sys.stderr)
    print(f"  2. python scripts/refetch_corrupt_pdfs.py --from-scan "
          f"--cycle uttarpradesh-2022 --cdp 9222 --tabs 4", file=sys.stderr)
    print(f"  3. python scripts/cloud_vision_preprocess.py \\", file=sys.stderr)
    print(f"       --pdf-dir data/eci/raw_pdfs/uttarpradesh-2022/raw_pdfs \\",
          file=sys.stderr)
    print(f"       --out-dir data/eci/for_ai/preprocessed_uttarpradesh_2022 \\",
          file=sys.stderr)
    print(f"       --pdf-allowlist data/allowlists/uttarpradesh_2022_top2.txt",
          file=sys.stderr)


if __name__ == "__main__":
    main()
