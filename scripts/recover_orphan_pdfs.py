"""
Scan the user's Downloads folder for ECI affidavit PDFs that got
misfiled there (instead of being saved to a state's raw_pdfs/ dir),
match them to the correct state cycle via the manifest.jsonl files,
and move them into place.

Runs completely locally — no network / API calls.

Usage:
    python scripts/recover_orphan_pdfs.py                       # dry-run report
    python scripts/recover_orphan_pdfs.py --commit                # actually move
    python scripts/recover_orphan_pdfs.py --downloads ~/Downloads --commit
"""
from __future__ import annotations
import argparse
import json
import shutil
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DOWNLOADS = Path.home() / "Downloads"
RAW_PDFS_ROOT = ROOT / "data" / "eci" / "raw_pdfs"


def build_pdf_index() -> dict[str, tuple[Path, Path]]:
    """Return {pdf_basename: (state_cycle_dir, expected_final_path)} by
    scanning every manifest.jsonl. If the same basename appears in
    multiple cycles, first-win (extremely unlikely — affidavit_ids
    include the numeric ID which is state-unique).
    """
    index: dict[str, tuple[Path, Path]] = {}
    for manifest in RAW_PDFS_ROOT.glob("*/manifest.jsonl"):
        cycle_dir = manifest.parent            # e.g. .../uttarpradesh-2022
        for line in manifest.read_text().splitlines():
            if not line.strip():
                continue
            try:
                r = json.loads(line)
            except json.JSONDecodeError:
                continue
            pdf_path = r.get("pdf_path") or ""
            if not pdf_path:
                continue
            basename = Path(pdf_path).name
            if basename not in index:
                index[basename] = (cycle_dir, Path(pdf_path))
    return index


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                  formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--downloads", default=str(DEFAULT_DOWNLOADS),
                    help="Folder to scan (default: ~/Downloads)")
    ap.add_argument("--commit", action="store_true",
                    help="Actually move the files. Default is dry-run.")
    args = ap.parse_args()

    downloads = Path(args.downloads).expanduser()
    if not downloads.exists():
        sys.exit(f"Downloads folder not found: {downloads}")

    print(f"Scanning: {downloads}", file=sys.stderr)
    print(f"Manifest index root: {RAW_PDFS_ROOT}", file=sys.stderr)

    index = build_pdf_index()
    print(f"Built index of {len(index):,} PDFs across "
          f"{len(list(RAW_PDFS_ROOT.glob('*/manifest.jsonl')))} cycles",
          file=sys.stderr)

    moved, missed_target, orphaned, already_there = 0, 0, 0, 0
    would_move = []
    for pdf in sorted(downloads.glob("*.pdf")):
        # ECI affidavits are named CANDIDATE_NAME__ID.pdf — match on
        # basename against the manifest index.
        info = index.get(pdf.name)
        if not info:
            orphaned += 1
            continue
        cycle_dir, target_path = info
        # Ensure raw_pdfs/ subfolder exists (some manifests use a nested layout)
        target_path.parent.mkdir(parents=True, exist_ok=True)
        if target_path.exists() and target_path.stat().st_size > 1024:
            already_there += 1
            continue
        would_move.append((pdf, target_path))

    print(f"\nAlready in place:      {already_there:>4d}", file=sys.stderr)
    print(f"Would move:            {len(would_move):>4d}", file=sys.stderr)
    print(f"Unknown / not in index:{orphaned:>4d}", file=sys.stderr)

    if not would_move:
        print("\nNothing to do.", file=sys.stderr)
        return

    if not args.commit:
        print("\n--- DRY RUN — nothing moved ---", file=sys.stderr)
        for src, dst in would_move[:15]:
            print(f"  {src.name}  →  {dst.relative_to(ROOT)}", file=sys.stderr)
        if len(would_move) > 15:
            print(f"  … and {len(would_move) - 15} more", file=sys.stderr)
        print(f"\nRun with --commit to actually move.", file=sys.stderr)
        return

    for src, dst in would_move:
        try:
            shutil.move(str(src), str(dst))
            moved += 1
        except Exception as e:
            print(f"  ✗ failed to move {src.name}: {e}", file=sys.stderr)

    print(f"\n✓ Moved {moved} PDFs into their proper cycle folders.",
          file=sys.stderr)


if __name__ == "__main__":
    main()
