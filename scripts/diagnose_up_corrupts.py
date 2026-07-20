"""One-off diagnostic: for every entry in the corrupt_pdfs.jsonl scan log
that belongs to UP 2022, check the actual file on disk right now.

Answers: how many were already re-downloaded (should be skipped by
refetch), how many are still corrupt (need refetch), how many are
missing entirely.

Run:
    python scripts/diagnose_up_corrupts.py
"""
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCAN = ROOT / "data" / "eci" / "errors" / "corrupt_pdfs.jsonl"

entries = [json.loads(l) for l in SCAN.read_text().splitlines() if l.strip()]
up = [e for e in entries if e["cycle"] == "uttarpradesh-2022"]

still_bad = now_valid = missing = 0
sample_valid: list[str] = []
sample_bad: list[str] = []
for e in up:
    p = Path(e["path"])
    if not p.exists():
        missing += 1
        continue
    if p.stat().st_size < 1024:
        still_bad += 1
        if len(sample_bad) < 3:
            sample_bad.append(f"{p.stat().st_size}B  {p.name}")
        continue
    with p.open("rb") as f:
        head = f.read(8)
    if head.startswith(b"%PDF-"):
        now_valid += 1
        if len(sample_valid) < 3:
            sample_valid.append(f"{p.stat().st_size}B  {p.name}")
    else:
        still_bad += 1
        if len(sample_bad) < 3:
            sample_bad.append(f"bad-magic  {p.name}")

print(f"UP 2022 entries in scan log: {len(up):,}")
print(f"  now valid on disk: {now_valid:,}  <- should be SKIPPED by refetch")
print(f"  still bad on disk: {still_bad:,}")
print(f"  file missing:      {missing:,}")
if sample_valid:
    print("\n  Sample already-fixed:")
    for s in sample_valid:
        print(f"    {s}")
if sample_bad:
    print("\n  Sample still-bad:")
    for s in sample_bad:
        print(f"    {s}")

# Folder totals for context
raw = ROOT / "data" / "eci" / "raw_pdfs" / "uttarpradesh-2022"
big = [p for p in raw.rglob("*.pdf") if p.stat().st_size >= 1024]
small = [p for p in raw.rglob("*.pdf") if p.stat().st_size < 1024]
print(f"\nTotal PDFs in UP 2022 folder:")
print(f"  >=1KB (likely valid): {len(big):,}")
print(f"  <1KB (stubs):         {len(small):,}")
