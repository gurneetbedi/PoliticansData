"""Cross-state summary: for every cycle we've fetched, report where each
candidate stands in the pipeline. Written both as a printed table and as
data/reports/state_status.csv.

Columns per cycle:
  cycle              — folder name (e.g. rajasthan-2023)
  allowlist          — candidates in the top-N allowlist (0 = none defined)
  on_disk            — allowlist PDFs actually present in the cycle folder
  missing            — allowlist entries with no PDF on disk
  preprocessed       — Cloud Vision JSONs present for allowlist entries
  extracted          — Gemini JSONs present
  bad_extraction     — Gemini JSONs that are _raw / empty name / empty const
  ready_to_apply     — extracted − bad_extraction (upper bound of what DB apply would take)
"""
from __future__ import annotations
import argparse
import csv
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
RAW_ROOT = ROOT / "data" / "eci" / "raw_pdfs"
ALLOW_DIR = ROOT / "data" / "allowlists"
PP_ROOT = ROOT / "data" / "eci" / "for_ai"
LX_ROOT = ROOT / "data" / "eci" / "for_ai" / "llm_extracted"
OUT_CSV = ROOT / "data" / "reports" / "state_status.csv"


def find_allowlist(slug_year: str) -> Path | None:
    """slug_year like 'rajasthan_2023'. Match either canonical (post-cleanup)
    or top-N variants."""
    # Post-cleanup canonical
    p = ALLOW_DIR / f"{slug_year}.txt"
    if p.exists():
        return p
    # Legacy top-N patterns
    for f in ALLOW_DIR.glob(f"{slug_year}_top*.txt"):
        return f
    return None


def audit_cycle(cycle_dir: Path) -> dict:
    """One row per cycle folder. Returns dict with all counts."""
    cycle_name = cycle_dir.name             # e.g. rajasthan-2023
    slug_year  = cycle_name.replace("-", "_")   # rajasthan_2023 (dir naming for preprocessed_/llm_extracted_)

    # Locate the raw_pdfs folder (nested if it exists, otherwise flat)
    raw_dir = cycle_dir / "raw_pdfs" if (cycle_dir / "raw_pdfs").exists() else cycle_dir
    # Walk both flat and nested to be safe
    on_disk_all = {p.name for p in cycle_dir.rglob("*.pdf")}

    allow_path = find_allowlist(slug_year)
    allowlist = []
    if allow_path:
        allowlist = [ln.strip() for ln in allow_path.read_text().splitlines()
                     if ln.strip() and not ln.startswith("#")]

    allow_set = set(allowlist)
    on_disk_allow = allow_set & on_disk_all
    missing = allow_set - on_disk_all

    # Preprocessed + extracted counts
    pp_dir = PP_ROOT / f"preprocessed_{slug_year}"
    lx_dir = LX_ROOT / slug_year

    # Every count below is SCOPED to the top-N allowlist. Candidates
    # outside the allowlist (3rd/4th-place fringe candidates we chose
    # not to process) are intentionally ignored — a "bad" extraction of
    # someone we never cared about isn't worth fixing.
    allow_stems = {name[:-4] for name in allowlist}   # strip .pdf

    pp_stems = set()
    if pp_dir.exists():
        pp_stems = {p.stem for p in pp_dir.glob("*.json")
                    if not p.name.startswith("_")}
    pp_in_allow = pp_stems & allow_stems

    lx_in_allow: set[str] = set()
    lx_bad = 0
    if lx_dir.exists():
        for p in lx_dir.glob("*.json"):
            if p.name.startswith("_") or p.stem not in allow_stems:
                continue
            lx_in_allow.add(p.stem)
            try:
                r = json.loads(p.read_text())
            except Exception:
                lx_bad += 1
                continue
            ext = r.get("extraction") or {}
            has_raw = "_raw" in ext
            name = (ext.get("identity") or {}).get("name_in_english") or ""
            const = (ext.get("political") or {}).get("constituency_name") or ""
            if has_raw or not name or not const:
                lx_bad += 1

    return {
        "cycle":            cycle_name,
        "allowlist":        len(allowlist),
        "on_disk":          len(on_disk_allow),
        "missing":          len(missing),
        "preprocessed":     len(pp_in_allow),
        "extracted":        len(lx_in_allow),
        "bad_extraction":   lx_bad,   # counted across whole extracted set (not scoped to allowlist)
        "ready_to_apply":   max(0, len(lx_in_allow) - lx_bad),
    }


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                  formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--cycles", nargs="*", default=[],
                    help="Restrict to specific cycles (e.g. rajasthan-2023). "
                         "Default: all cycles under data/eci/raw_pdfs/")
    args = ap.parse_args()

    cycle_dirs = sorted(d for d in RAW_ROOT.iterdir()
                        if d.is_dir() and (d / "manifest.jsonl").exists())
    if args.cycles:
        wanted = set(args.cycles)
        cycle_dirs = [d for d in cycle_dirs if d.name in wanted]
    if not cycle_dirs:
        sys.exit("No cycles matched.")

    rows = [audit_cycle(d) for d in cycle_dirs]

    # Print table
    print(f"{'Cycle':<32s}  {'Allow':>6s}  {'On disk':>7s}  {'Missing':>7s}  "
          f"{'PP':>4s}  {'LX':>4s}  {'Bad':>4s}  {'Ready':>5s}")
    print("-" * 84)
    for r in rows:
        marker = ""
        if r["missing"] and r["allowlist"]:
            marker += "  ⚠miss"
        if r["extracted"] < r["preprocessed"]:
            marker += "  ⚠xtr"
        if r["bad_extraction"] > 10:
            marker += "  ⚠bad"
        print(f"{r['cycle']:<32s}  {r['allowlist']:>6d}  {r['on_disk']:>7d}  "
              f"{r['missing']:>7d}  {r['preprocessed']:>4d}  {r['extracted']:>4d}  "
              f"{r['bad_extraction']:>4d}  {r['ready_to_apply']:>5d}{marker}")
    print("-" * 84)

    tot = {k: sum(r[k] for r in rows if isinstance(r[k], int))
           for k in ("allowlist","on_disk","missing","preprocessed",
                     "extracted","bad_extraction","ready_to_apply")}
    print(f"{'TOTAL':<32s}  {tot['allowlist']:>6d}  {tot['on_disk']:>7d}  "
          f"{tot['missing']:>7d}  {tot['preprocessed']:>4d}  {tot['extracted']:>4d}  "
          f"{tot['bad_extraction']:>4d}  {tot['ready_to_apply']:>5d}")

    # Write CSV
    OUT_CSV.parent.mkdir(parents=True, exist_ok=True)
    with OUT_CSV.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)
    print(f"\nCSV: {OUT_CSV.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
