"""Audit every state cycle for the "PDFs split across two folders" issue.

Some fetch runs saved PDFs directly under `data/eci/raw_pdfs/<cycle>/`
(flat layout), others under `data/eci/raw_pdfs/<cycle>/raw_pdfs/`
(nested layout). Cloud Vision + Gemini expect the nested layout;
anything at the top level gets ignored by downstream tools.

This script:
  1. Reports per-cycle: total flat PDFs, total nested PDFs, duplicates.
  2. With --cleanup: moves flat-only PDFs INTO the nested folder, and
     deletes flat copies that are duplicates of nested ones (keeping
     the nested version).

Usage:
    # Report only (default)
    python scripts/audit_cycle_layouts.py

    # Cleanup — consolidate all cycles into the nested layout
    python scripts/audit_cycle_layouts.py --cleanup

    # Cleanup one cycle
    python scripts/audit_cycle_layouts.py --cleanup --cycle rajasthan-2023
"""
from __future__ import annotations
import argparse
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
RAW_ROOT = ROOT / "data" / "eci" / "raw_pdfs"


def audit_cycle(cycle_dir: Path) -> dict:
    flat_pdfs = {p.name: p for p in cycle_dir.iterdir()
                 if p.is_file() and p.suffix == ".pdf"}
    nested_dir = cycle_dir / "raw_pdfs"
    nested_pdfs: dict[str, Path] = {}
    if nested_dir.exists():
        nested_pdfs = {p.name: p for p in nested_dir.iterdir()
                       if p.is_file() and p.suffix == ".pdf"}

    flat_names   = set(flat_pdfs)
    nested_names = set(nested_pdfs)

    return {
        "cycle": cycle_dir.name,
        "flat":       flat_pdfs,       # dict name → Path
        "nested":     nested_pdfs,
        "flat_only":   flat_names - nested_names,   # need to MOVE into nested
        "duplicates":  flat_names & nested_names,   # need to DELETE flat copy
        "nested_only": nested_names - flat_names,   # already fine
    }


def cleanup_cycle(a: dict, commit: bool) -> tuple[int, int]:
    """Move flat_only files into nested/, delete duplicates from flat.
    Returns (moved, deleted)."""
    moved = deleted = 0
    cycle_dir = RAW_ROOT / a["cycle"]
    nested_dir = cycle_dir / "raw_pdfs"
    nested_dir.mkdir(exist_ok=True)

    # Move flat_only files INTO nested/
    for name in sorted(a["flat_only"]):
        src = a["flat"][name]
        dst = nested_dir / name
        if commit:
            try:
                shutil.move(str(src), str(dst))
                moved += 1
            except Exception as e:
                print(f"  ✗ move failed {name}: {e}", file=sys.stderr)
        else:
            moved += 1

    # Delete duplicates from flat (nested copy is authoritative)
    for name in sorted(a["duplicates"]):
        src = a["flat"][name]
        # Sanity: only delete if nested version is at least as big
        try:
            nested_size = a["nested"][name].stat().st_size
            flat_size   = src.stat().st_size
        except Exception:
            nested_size = flat_size = 0
        if nested_size >= flat_size and nested_size > 0:
            if commit:
                try:
                    src.unlink()
                    deleted += 1
                except Exception as e:
                    print(f"  ✗ delete failed {name}: {e}", file=sys.stderr)
            else:
                deleted += 1
        else:
            print(f"  ⚠ skip delete {name}: nested copy is smaller "
                  f"({nested_size}B) than flat ({flat_size}B)",
                  file=sys.stderr)

    return moved, deleted


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                  formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--cleanup", action="store_true",
                    help="Actually consolidate. Default is report-only.")
    ap.add_argument("--cycle", default="",
                    help="Restrict to one cycle (e.g. rajasthan-2023)")
    args = ap.parse_args()

    cycle_dirs = sorted(d for d in RAW_ROOT.iterdir()
                        if d.is_dir() and (d / "manifest.jsonl").exists())
    if args.cycle:
        cycle_dirs = [d for d in cycle_dirs if d.name == args.cycle]
    if not cycle_dirs:
        sys.exit(f"No cycle dirs matched")

    audits = [audit_cycle(d) for d in cycle_dirs]

    print(f"{'Cycle':<32s}  {'Flat':>6s}  {'Nested':>6s}  {'Flat-only':>9s}  "
          f"{'Duplicates':>10s}  {'Nested-only':>11s}")
    print("-" * 84)

    total_flat_only = total_dupes = 0
    for a in audits:
        n_flat  = len(a["flat"])
        n_nest  = len(a["nested"])
        n_flonly = len(a["flat_only"])
        n_dupe   = len(a["duplicates"])
        n_nsonly = len(a["nested_only"])
        marker = ""
        if n_flonly or n_dupe:
            marker = "  ⚠"
        print(f"{a['cycle']:<32s}  {n_flat:>6d}  {n_nest:>6d}  "
              f"{n_flonly:>9d}  {n_dupe:>10d}  {n_nsonly:>11d}{marker}")
        total_flat_only += n_flonly
        total_dupes += n_dupe

    print("-" * 84)
    print(f"{'TOTALS':<32s}  {'':>6s}  {'':>6s}  "
          f"{total_flat_only:>9d}  {total_dupes:>10d}")

    if not (total_flat_only or total_dupes):
        print("\n✓ All cycles are already in the nested layout — nothing to do.")
        return

    if not args.cleanup:
        print("\n--- REPORT ONLY. Re-run with --cleanup to consolidate. ---")
        return

    # Execute cleanup
    print("\n[CLEANUP] Moving flat-only into nested/, deleting duplicates…")
    total_moved = total_deleted = 0
    for a in audits:
        if not (a["flat_only"] or a["duplicates"]):
            continue
        moved, deleted = cleanup_cycle(a, commit=True)
        total_moved += moved
        total_deleted += deleted
        print(f"  {a['cycle']:<32s}  moved={moved:>4d}  deleted={deleted:>4d}")

    print(f"\n✓ Moved {total_moved}, deleted {total_deleted} duplicates.")
    print(f"\nNext: rerun sync_allowlist_pdfs.py per state to find any truly "
          f"missing PDFs. Then Cloud Vision → Gemini → apply.")


if __name__ == "__main__":
    main()
