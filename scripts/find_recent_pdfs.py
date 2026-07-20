"""List PDFs in UP 2022 modified in the last N hours (default 3) so we
can see what filename pattern the recent refetch actually produced.
"""
import argparse
import re
import time
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CYCLE_DIR = ROOT / "data" / "eci" / "raw_pdfs" / "uttarpradesh-2022"
RAW_DIR = CYCLE_DIR / "raw_pdfs" if (CYCLE_DIR / "raw_pdfs").exists() else CYCLE_DIR

ap = argparse.ArgumentParser()
ap.add_argument("--hours", type=float, default=3.0)
args = ap.parse_args()

cutoff = time.time() - args.hours * 3600
recent = [p for p in RAW_DIR.iterdir()
          if p.is_file() and p.suffix == ".pdf" and p.stat().st_mtime >= cutoff]

print(f"Scanning: {RAW_DIR}")
print(f"PDFs modified in last {args.hours}h: {len(recent):,}\n")

if not recent:
    raise SystemExit("Nothing recent. Bump --hours if you need to look further back.")

# Show 15 sample filenames
print("Sample recent filenames:")
for p in sorted(recent, key=lambda x: -x.stat().st_mtime)[:15]:
    age_min = (time.time() - p.stat().st_mtime) / 60
    print(f"  ({age_min:5.0f}m ago) {p.stat().st_size:>7,d}B  {p.name}")

# Analyze the trailing __<num>.pdf suffix — what numbers actually appear?
suffix_counts: Counter = Counter()
pat = re.compile(r"__(\d+)\.pdf$")
for p in recent:
    m = pat.search(p.name)
    if m:
        suffix_counts[m.group(1)] += 1
    else:
        suffix_counts["<no numeric suffix>"] += 1

print(f"\nTop trailing __<id>.pdf values across the {len(recent):,} recent files:")
for val, n in suffix_counts.most_common(10):
    print(f"  {val:>15s}  {n:>5d}")
