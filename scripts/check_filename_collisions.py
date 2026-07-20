"""Check whether any UP 2022 candidates share BOTH name AND affidavit_id
(which would cause pdf_path collisions in the manifest).
"""
import json
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
MANIFEST = ROOT / "data" / "eci" / "raw_pdfs" / "uttarpradesh-2022" / "manifest.jsonl"

by_key: dict[tuple[str, str], list[dict]] = defaultdict(list)
for line in MANIFEST.read_text().splitlines():
    try:
        r = json.loads(line)
    except json.JSONDecodeError:
        continue
    key = (r.get("name", ""), r.get("affidavit_id", ""))
    by_key[key].append(r)

collisions = {k: v for k, v in by_key.items() if len(v) > 1}
print(f"Rows sharing BOTH name AND affidavit_id: {len(collisions)}")

if collisions:
    print(f"\n(these would collide at the same pdf_path — check if they're"
          f" truly the same candidate or if we have a fetch bug)\n")
    for (name, aff), rows in list(collisions.items())[:10]:
        print(f"  {name} / aff={aff}  x{len(rows)}")
        for r in rows:
            print(f"     party={r.get('party')}  const={r.get('constituency')}")
else:
    print("\nNo pdf_path collisions — every (name, aff_id) pair is unique.")

# Also check pdf_path uniqueness directly
paths = defaultdict(list)
for line in MANIFEST.read_text().splitlines():
    try:
        r = json.loads(line)
    except json.JSONDecodeError:
        continue
    p = r.get("pdf_path")
    if p:
        paths[Path(p).name].append(r)
dup_paths = {p: rs for p, rs in paths.items() if len(rs) > 1}
print(f"\nDistinct pdf_path basenames sharing >=2 manifest rows: {len(dup_paths)}")
for p, rs in list(dup_paths.items())[:5]:
    print(f"  {p}  x{len(rs)}")
    for r in rs:
        print(f"     {r.get('name')}  {r.get('party')}  {r.get('constituency')}")
