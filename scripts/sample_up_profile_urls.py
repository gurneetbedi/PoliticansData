"""Print 3 sample profile URLs from UP 2022 corrupt list so you can
test them manually in the Chrome window on port 9222.
"""
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
scan = json.loads(next(open(ROOT / "data/eci/errors/corrupt_pdfs.jsonl")).strip()) \
       if False else None  # placeholder

entries = [json.loads(l) for l in
           (ROOT / "data/eci/errors/corrupt_pdfs.jsonl").read_text().splitlines()
           if l.strip()]
up = [e for e in entries if e["cycle"] == "uttarpradesh-2022"]

mf = {}
for line in (ROOT / "data/eci/raw_pdfs/uttarpradesh-2022/manifest.jsonl") \
            .read_text().splitlines():
    try:
        r = json.loads(line)
    except json.JSONDecodeError:
        continue
    if r.get("pdf_path"):
        mf[Path(r["pdf_path"]).name] = r

print(f"UP 2022: {len(up)} corrupt entries, {len(mf)} manifest rows\n")
print("=" * 78)
print("Copy each URL into your Chrome window (port 9222) and see what loads:")
print("=" * 78)
shown = 0
for e in up:
    basename = Path(e["path"]).name
    row = mf.get(basename)
    if not row:
        continue
    url = row.get("profile_url", "")
    name = row.get("name", "")
    if not url:
        continue
    print(f"\n[{shown+1}] {name}")
    print(f"    {url}")
    shown += 1
    if shown >= 3:
        break
print()
