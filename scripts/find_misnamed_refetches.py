"""Find PDFs in UP 2022 raw_pdfs that were saved by the (buggy) refetch
script under a wrong filename (state code as aff_id instead of the real
candidate id). Match each back to its intended target from the corrupt
scan log + manifest, and (with --commit) rename it into place.

The bug: refetch_corrupt_pdfs.py was deriving aff_id from
`re.search(r"/(\\d+)/", download_url)` — which on ECI URLs picks up the
STATE CODE (e.g. 17 for UP), not the unique candidate id. So all
refetched files landed at `<SAFE_NAME>__17.pdf`, colliding when two
candidates share a normalized name.

Usage:
    python scripts/find_misnamed_refetches.py                   # dry-run report
    python scripts/find_misnamed_refetches.py --commit           # actually rename
"""
from __future__ import annotations
import argparse
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CYCLE = "uttarpradesh-2022"
CYCLE_DIR = ROOT / "data" / "eci" / "raw_pdfs" / CYCLE
RAW_DIR = CYCLE_DIR / "raw_pdfs" if (CYCLE_DIR / "raw_pdfs").exists() else CYCLE_DIR
MANIFEST = CYCLE_DIR / "manifest.jsonl"
SCAN = ROOT / "data" / "eci" / "errors" / "corrupt_pdfs.jsonl"


def safe_name_from(name: str) -> str:
    """Same normalisation the buggy refetch used."""
    return re.sub(r"[^A-Za-z0-9]+", "_", name).strip("_")


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                  formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--commit", action="store_true",
                    help="Actually rename files (default: dry-run report only)")
    ap.add_argument("--state-code", default="17",
                    help="The wrong aff_id that appears in misnamed files "
                         "(default 17 = UP)")
    args = ap.parse_args()

    if not MANIFEST.exists():
        sys.exit(f"Missing: {MANIFEST}")
    if not SCAN.exists():
        sys.exit(f"Missing: {SCAN}")

    # Build safe_name → list of (target_basename, name) from the corrupt list
    # (only entries that are still recorded as corrupt matter — those are
    # the intended landing spots).
    corrupt_entries = []
    for line in SCAN.read_text().splitlines():
        if not line.strip():
            continue
        try:
            r = json.loads(line)
        except json.JSONDecodeError:
            continue
        if r.get("cycle") != CYCLE:
            continue
        corrupt_entries.append(Path(r["path"]).name)
    corrupt_set = set(corrupt_entries)
    print(f"Corrupt-log entries for {CYCLE}: {len(corrupt_set):,}",
          file=sys.stderr)

    # Manifest: basename -> row
    mf: dict[str, dict] = {}
    for line in MANIFEST.read_text().splitlines():
        try:
            r = json.loads(line)
        except json.JSONDecodeError:
            continue
        p = r.get("pdf_path") or ""
        if p:
            mf[Path(p).name] = r

    # For each corrupt basename, compute what safe_name would have been used
    #   safe_name = re.sub(r"[^A-Za-z0-9]+", "_", name).strip("_")
    # Build reverse map: safe_name -> [target_basename, ...]
    by_safe: dict[str, list[str]] = {}
    for bn in corrupt_set:
        row = mf.get(bn)
        if not row:
            continue
        sn = safe_name_from(row.get("name", ""))
        if not sn:
            continue
        by_safe.setdefault(sn, []).append(bn)

    # Enumerate all `<SAFE>__<STATE_CODE>.pdf` files in raw_dir
    pattern = re.compile(rf"^(?P<safe>.+)__{re.escape(args.state_code)}\.pdf$")
    misnamed = []
    for p in RAW_DIR.iterdir():
        if not p.is_file() or p.suffix != ".pdf":
            continue
        m = pattern.match(p.name)
        if not m:
            continue
        misnamed.append((p, m.group("safe")))
    print(f"Misnamed candidates on disk (*__{args.state_code}.pdf): "
          f"{len(misnamed):,}", file=sys.stderr)

    # Match each misnamed file to its intended target
    rename_plan: list[tuple[Path, Path]] = []
    ambiguous = []
    orphans = []
    for src, safe in misnamed:
        targets = by_safe.get(safe, [])
        if not targets:
            orphans.append(src.name)
            continue
        if len(targets) > 1:
            # Multiple candidates normalise to the same safe_name — we
            # cannot know which one this file is. Overwrites already
            # happened between them during the buggy refetch anyway.
            ambiguous.append((src.name, targets))
            continue
        dst = RAW_DIR / targets[0]
        if dst.exists():
            # Someone or something already put a file there — don't clobber
            ambiguous.append((src.name, [f"target exists: {targets[0]}"]))
            continue
        rename_plan.append((src, dst))

    print(f"\nRename plan:      {len(rename_plan):,}  (safe to rename 1:1)",
          file=sys.stderr)
    print(f"Ambiguous:        {len(ambiguous):,}  (needs re-download)",
          file=sys.stderr)
    print(f"Orphans:          {len(orphans):,}  (no matching corrupt entry — "
          f"leave alone)", file=sys.stderr)

    if rename_plan[:3]:
        print("\nSample renames:", file=sys.stderr)
        for src, dst in rename_plan[:3]:
            print(f"  {src.name}  ->  {dst.name}", file=sys.stderr)
    if ambiguous[:3]:
        print("\nSample ambiguous:", file=sys.stderr)
        for src, tgts in ambiguous[:3]:
            print(f"  {src}  ->  {tgts}", file=sys.stderr)

    if not args.commit:
        print(f"\n--- DRY RUN — nothing renamed.  Re-run with --commit to apply.",
              file=sys.stderr)
        return

    renamed = 0
    for src, dst in rename_plan:
        try:
            src.rename(dst)
            renamed += 1
        except Exception as e:
            print(f"  ✗ {src.name}: {e}", file=sys.stderr)
    print(f"\n✓ Renamed {renamed:,} files.  "
          f"Now re-run: python scripts/scan_corrupt_pdfs.py --cycle {CYCLE} --fast",
          file=sys.stderr)


if __name__ == "__main__":
    main()
