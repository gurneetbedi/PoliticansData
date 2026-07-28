"""Deep-verify CAG PDFs and purge corrupt/truncated ones.

is_valid_pdf() in the downloader only checks magic bytes ('%PDF'). Truncated
downloads (partial writes, dropped connections) leave files that pass magic-
byte check but can't be opened by pypdf. This script tries to actually PARSE
each PDF and deletes anything that fails, so a re-run of the downloader
fetches them fresh.

Also flags suspiciously small files (< 30 KB — most CAG PAs are 0.5-30 MB).

Usage:
    python scripts/gpi_verify_cag_pdfs.py --dir data/cag/pdfs
    python scripts/gpi_verify_cag_pdfs.py --dir data/cag/pdfs --delete
    python scripts/gpi_verify_cag_pdfs.py --dir data/cag/pdfs/punjab --dry-run
"""
from __future__ import annotations
import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

MIN_SIZE_KB = 30   # CAG PDFs below this are almost certainly truncated


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                  formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dir", default="data/cag/pdfs",
                    help="Directory to scan (default: data/cag/pdfs)")
    ap.add_argument("--delete", action="store_true",
                    help="Delete corrupt/truncated files (default: dry-run report only)")
    ap.add_argument("--min-kb", type=int, default=MIN_SIZE_KB,
                    help=f"Min file size in KB (default {MIN_SIZE_KB})")
    args = ap.parse_args()

    try:
        from pypdf import PdfReader
        from pypdf.errors import PdfReadError
    except ImportError:
        raise SystemExit("pip install pypdf")

    scan_dir = ROOT / args.dir if not Path(args.dir).is_absolute() else Path(args.dir)
    if not scan_dir.exists():
        raise SystemExit(f"No such directory: {scan_dir}")

    pdfs = sorted(scan_dir.rglob("*.pdf"))
    print(f"Scanning {len(pdfs)} PDFs under {scan_dir}")
    print()

    stats = {"ok": 0, "too_small": [], "unparseable": [], "no_pdf_header": []}

    for pdf in pdfs:
        # Check size
        size_kb = pdf.stat().st_size / 1024
        if size_kb < args.min_kb:
            stats["too_small"].append((pdf, size_kb))
            continue

        # Check magic bytes
        with pdf.open("rb") as f:
            head = f.read(4)
        if head != b"%PDF":
            stats["no_pdf_header"].append(pdf)
            continue

        # Try to actually parse
        try:
            r = PdfReader(str(pdf))
            _ = len(r.pages)   # forces parse
            stats["ok"] += 1
        except Exception as e:
            stats["unparseable"].append((pdf, type(e).__name__))

    # Report
    print(f"OK:               {stats['ok']}")
    print(f"Too small:        {len(stats['too_small'])}  (<{args.min_kb}KB)")
    print(f"No PDF header:    {len(stats['no_pdf_header'])}")
    print(f"Unparseable:      {len(stats['unparseable'])}  (pypdf can't open)")

    corrupt = (
        [(p, f"too_small_{k:.0f}KB") for p, k in stats["too_small"]]
        + [(p, "no_pdf_header") for p in stats["no_pdf_header"]]
        + [(p, f"unparseable_{e}") for p, e in stats["unparseable"]]
    )

    if corrupt:
        print(f"\nCorrupt files ({len(corrupt)}):")
        for path, reason in corrupt[:30]:
            rel = path.relative_to(ROOT) if str(path).startswith(str(ROOT)) else path
            print(f"  [{reason:<28s}] {rel}")
        if len(corrupt) > 30:
            print(f"  ... and {len(corrupt) - 30} more")

    if args.delete and corrupt:
        print(f"\n Deleting {len(corrupt)} corrupt files...")
        for path, _ in corrupt:
            try:
                path.unlink()
            except Exception as e:
                print(f"  ✗ Couldn't delete {path.name}: {e}")
        print(f"Done. Re-run the downloader to re-fetch the deleted PDFs.")
    elif corrupt:
        print(f"\nDry-run — pass --delete to remove these {len(corrupt)} files.")


if __name__ == "__main__":
    main()
