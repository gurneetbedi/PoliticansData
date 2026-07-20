"""One-shot cleanup — consolidates scattered diagnostic reports.

  - bad_extractions_<slug>.txt  → data/reports/bad_extractions/
  - missing_<state>_<year>.csv  → data/reports/missing_data/
  - corrupt_pdfs.jsonl (already in errors/) — leave alone.

Allowlist files are LEFT ALONE — their `_topN` suffix is intentionally
preserved so `ls data/allowlists/` tells you at a glance what coverage
each state has (top-2 vs top-4). Downstream scripts glob for both
canonical and top-N patterns.

Dry-run by default. --commit applies the changes.

Usage:
    python scripts/cleanup_project_structure.py            # report only
    python scripts/cleanup_project_structure.py --commit
"""
from __future__ import annotations
import argparse
import re
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ALLOWLISTS = ROOT / "data" / "allowlists"
REPORTS = ROOT / "data" / "reports"


def plan_allowlist_renames() -> list[tuple[Path, Path]]:
    """Find every {slug}_{year}_topN.txt and pair with canonical
    {slug}_{year}.txt destination."""
    pattern = re.compile(r"^(?P<slug>.+?)_(?P<year>\d{4})_top\d+\.txt$")
    moves = []
    for src in sorted(ALLOWLISTS.glob("*_top*.txt")):
        m = pattern.match(src.name)
        if not m:
            continue
        dst = ALLOWLISTS / f"{m['slug']}_{m['year']}.txt"
        if dst.exists() and dst.samefile(src):
            continue
        moves.append((src, dst))
    return moves


def plan_report_moves() -> list[tuple[Path, Path]]:
    """Move loose diagnostic files under data/reports/ into typed subdirs."""
    moves = []
    bad_dir  = REPORTS / "bad_extractions"
    miss_dir = REPORTS / "missing_data"

    # bad_extractions_<slug>.txt at top of data/reports/
    for f in sorted(REPORTS.glob("bad_extractions_*.txt")):
        moves.append((f, bad_dir / f.name))
    for f in sorted(REPORTS.glob("bad_extractions_*.json")):
        moves.append((f, bad_dir / f.name))

    # missing_<state>_<year>.csv — currently already under missing_data/
    # but sweep any strays at top of data/reports/ into it.
    for f in sorted(REPORTS.glob("missing_*.csv")):
        moves.append((f, miss_dir / f.name))

    return moves


def apply(moves: list[tuple[Path, Path]], commit: bool, label: str) -> int:
    if not moves:
        print(f"  ({label}: nothing to do)")
        return 0
    for src, dst in moves:
        rel_src = src.relative_to(ROOT)
        rel_dst = dst.relative_to(ROOT)
        if commit:
            dst.parent.mkdir(parents=True, exist_ok=True)
            if dst.exists() and not dst.samefile(src):
                # Existing canonical file — don't overwrite; skip.
                print(f"  ↷ skip (exists): {rel_dst}")
                continue
            try:
                shutil.move(str(src), str(dst))
                print(f"  ✓ {rel_src}  →  {rel_dst}")
            except Exception as e:
                print(f"  ✗ {rel_src}: {e}")
        else:
            print(f"  would move: {rel_src}  →  {rel_dst}")
    return len(moves)


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                  formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--commit", action="store_true")
    args = ap.parse_args()

    report_moves = plan_report_moves()

    print(f"Reports — consolidate under data/reports/<type>/")
    print(f"(Allowlists left alone — their _topN suffix stays as coverage marker)")
    apply(report_moves, args.commit, "reports")

    if not args.commit:
        print(f"\n--- DRY RUN. Re-run with --commit to apply. ---")
    else:
        print(f"\n✓ Cleanup complete.")


if __name__ == "__main__":
    main()
