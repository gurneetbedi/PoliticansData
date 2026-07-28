"""Analyze the residual gaps to see the honest blocker distribution.

For each state's remaining gaps, classifies:
  - gemini_has_wealth:      Gemini extracted wealth but matcher/DB blocked
  - gemini_no_wealth:       Gemini file exists but wealth fields all null
  - no_gemini_file:         No Gemini JSON at all
  - not_in_manifest:        Not in the state's manifest

This tells us the theoretical maximum recovery vs what's truly missing.
"""
import csv
import json
import sqlite3
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

STATES = [
    ("Andhra Pradesh", 2024, "andhrapradesh_2024"),
    ("Bihar", 2025, "bihar_2025"),
    ("Delhi", 2020, "delhi_2020"),
    ("Gujarat", 2022, "gujarat_2022"),
    ("Haryana", 2019, "haryana_2019"),
    ("Jammu and Kashmir", 2024, "jk_2024"),
    ("Karnataka", 2023, "karnataka_2023"),
    ("Madhya Pradesh", 2023, "madhyapradesh_2023"),
    ("Maharashtra", 2024, "maharashtra_2024"),
    ("Rajasthan", 2023, "rajasthan_2023"),
    ("Tamil Nadu", 2026, "tamilnadu_2026"),
    ("Uttar Pradesh", 2022, "uttarpradesh_2022"),
    ("West Bengal", 2026, "westbengal_2026"),
]

grand = Counter()
print(f"{'State':<22s}  {'Gaps':>5s}  {'Wealth':>7s}  {'NoWealth':>9s}  {'NoFile':>7s}  {'Recoverable':>11s}")
print("-" * 78)
for state, year, slug_year in STATES:
    gap_csv = ROOT / f"data/reports/gaps_{slug_year}.csv"
    if not gap_csv.exists():
        continue
    lx_dir = ROOT / f"data/eci/for_ai/llm_extracted/{slug_year}"
    counts = Counter()
    for row in csv.DictReader(gap_csv.open()):
        stem = row["pdf"][:-4] if row["pdf"].endswith(".pdf") else row["pdf"]
        lx_path = lx_dir / (stem + ".json")
        if not lx_path.exists():
            counts["no_gemini_file"] += 1
            continue
        try:
            g = json.loads(lx_path.read_text())
        except Exception:
            counts["no_gemini_file"] += 1
            continue
        ext = g.get("extraction") or {}
        if "_raw" in ext:
            counts["gemini_no_wealth"] += 1
            continue
        am = ext.get("assets_movable") or {}
        ai = ext.get("assets_immovable") or {}
        mov = am.get("total_movable_assets_inr")
        imm = ai.get("total_immovable_assets_inr")
        if mov or imm:
            counts["gemini_has_wealth"] += 1
        else:
            counts["gemini_no_wealth"] += 1

    total = sum(counts.values())
    print(f"{state:<22s}  {total:>5d}  {counts['gemini_has_wealth']:>7d}  "
          f"{counts['gemini_no_wealth']:>9d}  {counts['no_gemini_file']:>7d}  "
          f"{counts['gemini_has_wealth']:>11d}")
    for k, v in counts.items():
        grand[k] += v

print("-" * 78)
print(f"{'TOTAL':<22s}  {sum(grand.values()):>5d}  {grand['gemini_has_wealth']:>7d}  "
      f"{grand['gemini_no_wealth']:>9d}  {grand['no_gemini_file']:>7d}  "
      f"{grand['gemini_has_wealth']:>11d}")

print(f"\nHonest floor:")
print(f"  Recoverable (Gemini has wealth): {grand['gemini_has_wealth']}")
print(f"  Structurally unrecoverable:       {grand['gemini_no_wealth'] + grand['no_gemini_file']}")
