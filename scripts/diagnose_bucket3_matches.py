"""For each candidate in the missing-data CSV who has a completed Gemini
extraction but no DB wealth data, print a side-by-side comparison of:
   - DB row     (from election_appearances / politicians / constituencies)
   - Manifest   (from raw_pdfs/<cycle>/manifest.jsonl)
   - Gemini     (from llm_extracted/<cycle>/<basename>.json)

Highlights what's mismatching (name spelling, constituency name, or
true collision) so we can decide the right matcher fix.

Usage:
    python scripts/diagnose_bucket3_matches.py --state "Rajasthan" --year 2023
"""
from __future__ import annotations
import argparse
import csv
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", (s or "").lower())


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--state", required=True)
    ap.add_argument("--year", type=int, required=True)
    ap.add_argument("--limit", type=int, default=25,
                    help="Max candidates to print")
    args = ap.parse_args()

    state_slug = args.state.lower().replace(" ", "_")
    csv_path = ROOT / f"data/reports/missing_data/missing_{state_slug}_{args.year}.csv"
    if not csv_path.exists():
        sys.exit(f"Report CSV not found: {csv_path.relative_to(ROOT)}\n"
                  f"Run: python scripts/report_missing_data.py")

    # Cycle folder discovery
    cycle_slug = args.state.lower().replace(" ", "")
    cycle_dir = None
    for d in sorted((ROOT / "data/eci/raw_pdfs").iterdir()):
        if d.name.startswith(cycle_slug) and d.name.endswith(str(args.year)):
            cycle_dir = d
            break
    if not cycle_dir:
        sys.exit(f"No cycle dir for {args.state} {args.year}")

    lx_slug = args.state.lower().replace(" ", "") + f"_{args.year}"
    lx_dir = ROOT / f"data/eci/for_ai/llm_extracted/{lx_slug}"

    # Manifest index by basename
    mf_by_name: dict[str, dict] = {}
    for line in (cycle_dir / "manifest.jsonl").read_text().splitlines():
        try:
            r = json.loads(line)
        except json.JSONDecodeError:
            continue
        if r.get("pdf_path"):
            mf_by_name[Path(r["pdf_path"]).name] = r

    # Bucket 3: rows in report where extraction JSON exists
    bucket3 = []
    for row in csv.DictReader(csv_path.open()):
        pdf_name = row.get("expected_pdf_name", "")
        if not pdf_name:
            continue
        stem = pdf_name[:-4] if pdf_name.endswith(".pdf") else pdf_name
        lx_path = lx_dir / (stem + ".json")
        if lx_path.exists():
            bucket3.append((row, stem, lx_path))

    print(f"Bucket 3 (Gemini done, apply failed): {len(bucket3)} candidates\n")

    for i, (row, stem, lx_path) in enumerate(bucket3[:args.limit], 1):
        try:
            g = json.loads(lx_path.read_text())
        except Exception:
            continue
        # Gemini schema has extraction.candidate_name and extraction.constituency
        ext = g.get("extraction") or {}
        g_name = (ext.get("candidate_name") or g.get("candidate_name") or "").strip()
        g_const = (ext.get("constituency") or g.get("constituency") or "").strip()
        g_party = (ext.get("party") or g.get("party") or "").strip()
        g_aff_id = g.get("affidavit_id") or ext.get("affidavit_id") or ""

        mf = mf_by_name.get(stem + ".pdf", {})
        mf_name  = mf.get("name", "")
        mf_const = mf.get("constituency", "")
        mf_party = mf.get("party", "")
        mf_aff   = mf.get("affidavit_id", "")

        # DB row values come from the missing-report CSV
        db_name  = row.get("candidate", "")
        db_const = row.get("constituency", "")
        db_party = row.get("party", "")

        # Compute differences
        name_diffs  = _norm(db_name)  != _norm(g_name)  or _norm(db_name)  != _norm(mf_name)
        const_diffs = _norm(db_const) != _norm(g_const) or _norm(db_const) != _norm(mf_const)

        marker = ""
        if name_diffs and const_diffs:
            marker = "  ⚠ name+const mismatch"
        elif name_diffs:
            marker = "  ⚠ name mismatch"
        elif const_diffs:
            marker = "  ⚠ const mismatch"

        print(f"[{i:>2d}] {stem}.pdf{marker}")
        print(f"     DB row:   name={db_name!r:<40s} const={db_const!r:<25s} party={db_party!r}")
        print(f"     Manifest: name={mf_name!r:<40s} const={mf_const!r:<25s} party={mf_party!r}  aff_id={mf_aff}")
        print(f"     Gemini:   name={g_name!r:<40s} const={g_const!r:<25s} party={g_party!r}")

    if len(bucket3) > args.limit:
        print(f"\n(+ {len(bucket3) - args.limit} more — pass --limit to see all)")


if __name__ == "__main__":
    main()
