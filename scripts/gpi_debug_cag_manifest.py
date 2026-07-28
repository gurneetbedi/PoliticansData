"""Quick diagnostic — inspect Punjab rows in the CAG catalog manifest."""
import csv
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
p = ROOT / "data" / "cag" / "catalog" / "reports_manifest.csv"

with p.open() as f:
    rows = list(csv.DictReader(f))

pb = [r for r in rows if r["state"] == "Punjab"]
print(f"Total rows in catalog:  {len(rows)}")
print(f"Punjab rows:            {len(pb)}")
print()

print("First 5 Punjab rows:")
for r in pb[:5]:
    print(f"  title:       {r['title'][:70]}")
    print(f"  report_type: [{r['report_type']}]")
    print(f"  sector:      [{r['sector']}]")
    print(f"  audit_year:  [{r['audit_year']}]")
    print(f"  pub_date:    [{r['publication_date']}]")
    print()

blank_years = sum(1 for r in pb if not r["audit_year"])
print(f"Punjab rows with blank audit_year:  {blank_years} / {len(pb)}")
blank_types = sum(1 for r in pb if not r["report_type"])
print(f"Punjab rows with blank report_type: {blank_types} / {len(pb)}")

print()
print("Distinct report_type values (across all states):")
for t, n in Counter(r["report_type"] for r in rows).most_common(20):
    print(f"  [{t}]  {n}")

print()
print("Distinct sector values seen in Punjab rows:")
for s, n in Counter(r["sector"] for r in pb).most_common(15):
    print(f"  [{s[:60]}]  {n}")
