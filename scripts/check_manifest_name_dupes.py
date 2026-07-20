"""Check whether the UP 2022 manifest has candidates sharing the same
name but with different profile URLs (expected), and whether any
profile URLs are duplicated across candidates (would be a bug).

Also samples 3 same-name candidates so we can inspect their profile
pages and see whether ECI actually serves distinct affidavits.
"""
import json
from collections import Counter, defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
MANIFEST = ROOT / "data" / "eci" / "raw_pdfs" / "uttarpradesh-2022" / "manifest.jsonl"

rows = []
for line in MANIFEST.read_text().splitlines():
    try:
        rows.append(json.loads(line))
    except json.JSONDecodeError:
        continue

print(f"Manifest rows: {len(rows):,}\n")

# 1. Names shared by multiple candidates
by_name: dict[str, list[dict]] = defaultdict(list)
for r in rows:
    by_name[r.get("name", "")].append(r)
shared_names = {n: rs for n, rs in by_name.items() if len(rs) > 1}
print(f"Names shared by >=2 candidates: {len(shared_names)}")
top_shared = sorted(shared_names.items(), key=lambda kv: -len(kv[1]))[:10]
for name, rs in top_shared:
    print(f"  {name:35s}  x {len(rs)}")

# 2. Profile URLs shared by multiple candidates — would be a BUG
url_counts = Counter(r.get("profile_url", "") for r in rows if r.get("profile_url"))
dup_urls = {u: n for u, n in url_counts.items() if n > 1}
print(f"\nProfile URLs shared by >=2 candidates: {len(dup_urls)}")
if dup_urls:
    print("  *** BUG: these should be unique per candidate! ***")
    for u, n in list(dup_urls.items())[:5]:
        print(f"    x{n}  {u[:80]}...")

# 3. affidavit_id uniqueness
aff_counts = Counter(r.get("affidavit_id", "") for r in rows if r.get("affidavit_id"))
dup_aff = {a: n for a, n in aff_counts.items() if n > 1 and a != "noid"}
print(f"\naffidavit_id values shared by >=2 candidates: {len(dup_aff)}")

# 4. Deep look at one ambiguous name
if top_shared:
    focus_name = top_shared[0][0]
    print(f"\n{'='*78}")
    print(f"Deep look at {focus_name!r}:")
    print(f"{'='*78}")
    for r in shared_names[focus_name]:
        print(f"\n  Party:         {r.get('party', '')}")
        print(f"  Constituency:  {r.get('constituency', '')}")
        print(f"  Affidavit ID:  {r.get('affidavit_id', '')}")
        print(f"  Profile URL:   {r.get('profile_url', '')[:90]}...")
        pdf = r.get("pdf_path", "")
        print(f"  Local PDF:     {Path(pdf).name if pdf else '(none)'}")

# 5. Ambiguous cases from the corrupt list
print(f"\n{'='*78}")
print("Suggested inspection commands (compare 2 same-name profile pages):")
print(f"{'='*78}")
if top_shared:
    focus = top_shared[0][1][:2]  # first 2 candidates with the top-shared name
    for r in focus:
        pdf_name = Path(r.get("pdf_path", "")).name
        if pdf_name:
            print(f"\npython scripts/inspect_profile_page.py \\")
            print(f"    --basename {pdf_name} \\")
            print(f"    --cycle uttarpradesh-2022 \\")
            print(f"    --cdp 9222")
