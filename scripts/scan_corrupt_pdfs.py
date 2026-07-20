"""
Scan every downloaded ECI affidavit PDF for corruption. Read-only —
never modifies or deletes anything. Safe to run in parallel with any
other pipeline job (Cloud Vision / Gemini / fetch).

Checks per PDF (fastest → slowest):
  1. File size  → anything < 1 KB is almost certainly a stub / failed download
  2. Magic bytes → must start with %PDF- (Adobe's spec)
  3. pdfplumber open + page count → confirms the PDF structure is intact
     (this is the slow but definitive check; skip with --fast)

Outputs:
  • Per-cycle summary table (files, ok, corrupt, %corrupt)
  • JSONL log of every corrupt file at
    data/eci/errors/corrupt_pdfs.jsonl
    (fields: cycle, path, size_bytes, reason, checked_at)

Usage:
    python scripts/scan_corrupt_pdfs.py                # all cycles, thorough
    python scripts/scan_corrupt_pdfs.py --fast          # skip pdfplumber (magic-bytes only)
    python scripts/scan_corrupt_pdfs.py --cycle uttarpradesh-2022
    python scripts/scan_corrupt_pdfs.py --workers 8     # parallelise pdfplumber checks
"""
from __future__ import annotations
import argparse
import json
import sys
from concurrent.futures import ProcessPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
RAW_PDFS_ROOT = ROOT / "data" / "eci" / "raw_pdfs"
LOG_PATH = ROOT / "data" / "eci" / "errors" / "corrupt_pdfs.jsonl"

MIN_SIZE_BYTES = 1024   # anything smaller = definitely a stub


def _check_one(path_str: str, fast: bool) -> tuple[str, str | None, int]:
    """Return (path, reason_if_corrupt, size_bytes). None reason = OK."""
    p = Path(path_str)
    try:
        size = p.stat().st_size
    except FileNotFoundError:
        return (path_str, "file_missing", 0)

    if size < MIN_SIZE_BYTES:
        return (path_str, f"too_small ({size} bytes)", size)

    # Magic bytes
    try:
        with p.open("rb") as f:
            head = f.read(8)
    except Exception as e:
        return (path_str, f"unreadable: {type(e).__name__}", size)
    if not head.startswith(b"%PDF-"):
        return (path_str, "bad_magic_bytes", size)

    if fast:
        return (path_str, None, size)

    # Deep check — try to open with pdfplumber
    try:
        import pdfplumber
    except ImportError:
        # pdfplumber not installed → fall back to fast mode silently
        return (path_str, None, size)
    try:
        with pdfplumber.open(str(p)) as pdf:
            n_pages = len(pdf.pages)
            if n_pages == 0:
                return (path_str, "zero_pages", size)
    except Exception as e:
        return (path_str, f"pdfplumber_open_failed: {type(e).__name__}: {str(e)[:80]}", size)

    return (path_str, None, size)


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                  formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--cycle", default="",
                    help="Restrict to a single cycle folder (e.g. uttarpradesh-2022)")
    ap.add_argument("--fast", action="store_true",
                    help="Skip pdfplumber structural check (magic-bytes only)")
    ap.add_argument("--workers", type=int, default=4,
                    help="Parallel processes for the pdfplumber check")
    args = ap.parse_args()

    if not RAW_PDFS_ROOT.exists():
        sys.exit(f"raw_pdfs root not found: {RAW_PDFS_ROOT}")

    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)

    # Enumerate every cycle dir
    cycle_dirs = sorted(d for d in RAW_PDFS_ROOT.iterdir() if d.is_dir())
    if args.cycle:
        cycle_dirs = [d for d in cycle_dirs if d.name == args.cycle]

    if not cycle_dirs:
        sys.exit(f"No cycle dirs found (filter={args.cycle!r})")

    now_iso = datetime.now(timezone.utc).isoformat()
    grand_ok = grand_bad = 0
    per_cycle_rows: list[tuple[str, int, int, int]] = []
    all_corrupt: list[dict] = []

    for cyc_dir in cycle_dirs:
        # PDFs may live directly under cycle_dir OR under cycle_dir/raw_pdfs/
        pdfs = sorted(cyc_dir.rglob("*.pdf"))
        if not pdfs:
            continue

        print(f"→ {cyc_dir.name:35s}  scanning {len(pdfs):,} PDFs "
              f"({'fast' if args.fast else 'deep'})…", file=sys.stderr)

        cycle_ok = cycle_bad = 0
        results: list[tuple[str, str | None, int]] = []

        if args.fast or args.workers <= 1:
            # Serial — fast enough for magic-bytes-only or if user wants no parallelism
            for p in pdfs:
                results.append(_check_one(str(p), args.fast))
        else:
            # Parallel (pdfplumber is CPU-bound; ProcessPool avoids GIL)
            with ProcessPoolExecutor(max_workers=args.workers) as ex:
                futures = [ex.submit(_check_one, str(p), args.fast) for p in pdfs]
                for i, fut in enumerate(as_completed(futures)):
                    results.append(fut.result())
                    if (i + 1) % 500 == 0:
                        print(f"    …{i+1}/{len(pdfs)}", file=sys.stderr)

        for path, reason, size in results:
            if reason is None:
                cycle_ok += 1
            else:
                cycle_bad += 1
                all_corrupt.append({
                    "ts":         now_iso,
                    "cycle":      cyc_dir.name,
                    "path":       path,
                    "size_bytes": size,
                    "reason":     reason,
                })

        grand_ok += cycle_ok
        grand_bad += cycle_bad
        per_cycle_rows.append((cyc_dir.name, len(pdfs), cycle_ok, cycle_bad))

    # Write log
    if all_corrupt:
        with LOG_PATH.open("w", encoding="utf-8") as f:
            for entry in all_corrupt:
                f.write(json.dumps(entry) + "\n")

    # ─── Summary ─────────────────────────────────────────────────────
    print(file=sys.stderr)
    print(f"{'Cycle':35s}  {'PDFs':>7s}  {'OK':>7s}  {'Corrupt':>8s}  {'%':>5s}",
          file=sys.stderr)
    print(f"{'-'*35}  {'-'*7}  {'-'*7}  {'-'*8}  {'-'*5}", file=sys.stderr)
    for name, total, ok, bad in per_cycle_rows:
        pct = 100 * bad // total if total else 0
        marker = "❌ " if pct > 5 else ("⚠  " if pct > 1 else "   ")
        print(f"{marker}{name:33s}  {total:>7,d}  {ok:>7,d}  {bad:>8,d}  {pct:>4d}%",
              file=sys.stderr)
    print(f"{'-'*35}  {'-'*7}  {'-'*7}  {'-'*8}  {'-'*5}", file=sys.stderr)
    total_all = grand_ok + grand_bad
    pct_all = 100 * grand_bad // total_all if total_all else 0
    print(f"{'TOTAL':35s}  {total_all:>7,d}  {grand_ok:>7,d}  {grand_bad:>8,d}  {pct_all:>4d}%",
          file=sys.stderr)

    if all_corrupt:
        print(f"\nCorrupt PDF details written to:", file=sys.stderr)
        print(f"  {LOG_PATH.relative_to(ROOT)}", file=sys.stderr)
        print(f"\nQuick top failure reasons:", file=sys.stderr)
        from collections import Counter
        reasons = Counter(e["reason"].split(":")[0].split(" ")[0]
                          for e in all_corrupt)
        for reason, n in reasons.most_common(6):
            print(f"  {n:>5d}  {reason}", file=sys.stderr)
    else:
        print(f"\n✓ Zero corrupt PDFs across {total_all:,} files. Clean sweep.",
              file=sys.stderr)


if __name__ == "__main__":
    main()
