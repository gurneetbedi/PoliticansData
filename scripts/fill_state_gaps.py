"""Fill the coverage gaps for one state cycle by:

  1. Reading data/reports/gaps_<slug>_<year>.csv (produced by
     diagnose_state_gaps_sqlite.py).
  2. Grouping by failure type.
  3. For each type, writing a mini-allowlist and printing the exact
     commands to run to fix that bucket. Order matters:
        no_pdf          → sync_allowlist_pdfs (refetch via profile_url)
        no_cloud_vision → cloud_vision_preprocess (Hindi/etc auto-hinted)
        no_gemini       → llm_extract_via_gemini
     Then a final apply that writes to LOCAL SQLite (never Postgres).

By default this prints a plan without executing anything. Pass
--commit to actually run the pipeline steps in sequence.

NOTE: apply_llm_extraction.py uses lokvani.db (SQLite) by default —
that's the file the site reads. We do NOT set DATABASE_URL for any
step in this script, so all writes land in SQLite.

Usage:
    # Plan only (default)
    python scripts/fill_state_gaps.py --state "Rajasthan" --year 2023

    # Execute end-to-end
    python scripts/fill_state_gaps.py --state "Rajasthan" --year 2023 \
      --commit --cdp 9222
"""
from __future__ import annotations
import argparse
import csv
import os
import subprocess
import sys
from collections import Counter, defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def run(cmd: list[str], commit: bool):
    display = " ".join(cmd if isinstance(cmd, list) else [cmd])
    print(f"\n$ {display}")
    if not commit:
        return
    # Explicitly clear DATABASE_URL for the subprocess so apply
    # writes to SQLite even if the parent shell has DATABASE_URL set.
    env = {**os.environ}
    env.pop("DATABASE_URL", None)
    rc = subprocess.call(cmd, env=env)
    if rc != 0:
        sys.exit(f"✗ step failed with exit code {rc}: {display}")


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                  formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--state", required=True)
    ap.add_argument("--year", type=int, required=True)
    ap.add_argument("--commit", action="store_true")
    ap.add_argument("--cdp", type=int, default=9222)
    ap.add_argument("--tabs", type=int, default=1,
                    help="Refetch tabs (only used for no_pdf gaps). Default 1.")
    ap.add_argument("--delay", type=float, default=2.5)
    args = ap.parse_args()

    SLUG_OVERRIDES = {
        "jammu and kashmir": "jk",
        "jammu & kashmir":   "jk",
    }
    state_lower = args.state.lower()
    slug = SLUG_OVERRIDES.get(state_lower, state_lower.replace(" ", ""))
    slug_year = f"{slug}_{args.year}"
    cycle_name = f"{slug}-{args.year}"

    gaps_csv = ROOT / f"data/reports/gaps_{slug_year}.csv"
    if not gaps_csv.exists():
        sys.exit(f"Gaps CSV missing: {gaps_csv.relative_to(ROOT)}\n"
                  f"Run: python scripts/diagnose_state_gaps_sqlite.py "
                  f"--state \"{args.state}\" --year {args.year}")

    rows = list(csv.DictReader(gaps_csv.open()))
    by_failure = defaultdict(list)
    for r in rows:
        by_failure[r["failure"]].append(r["pdf"])

    print(f"State:   {args.state} {args.year}")
    print(f"Gaps:    {len(rows)}")
    print(f"Breakdown:")
    for k, v in Counter(r["failure"] for r in rows).most_common():
        print(f"  {len(by_failure[k]):>4d}  {k}")

    # Write mini-allowlists per failure type
    tmp_dir = Path(f"/tmp/gaps_{slug_year}")
    tmp_dir.mkdir(exist_ok=True)
    per_type_paths = {}
    for failure, pdfs in by_failure.items():
        p = tmp_dir / f"{failure}.txt"
        p.write_text("\n".join(sorted(pdfs)) + "\n")
        per_type_paths[failure] = p

    print(f"\nMini-allowlists written to: {tmp_dir}")

    # STAGE 1: refetch no_pdf + corrupt_pdf entries.
    # For corrupt_pdf we first delete the damaged file so sync_allowlist
    # sees it as missing. We also delete stale PP/LX so downstream
    # re-processes cleanly.
    corrupt_pdfs = by_failure.get("corrupt_pdf", [])
    if corrupt_pdfs:
        print(f"\n  (corrupt_pdf) Deleting {len(corrupt_pdfs)} damaged PDFs "
              f"+ their PP/LX so sync will refetch them")
        cycle_dir = ROOT / "data/eci/raw_pdfs" / cycle_name
        raw_dir = cycle_dir / "raw_pdfs" if (cycle_dir / "raw_pdfs").exists() else cycle_dir
        pp_root = ROOT / "data/eci/for_ai" / f"preprocessed_{slug_year}"
        lx_root = ROOT / "data/eci/for_ai/llm_extracted" / slug_year
        for name in corrupt_pdfs:
            stem = name[:-4] if name.endswith(".pdf") else name
            for p in [raw_dir / name,
                      pp_root / (stem + ".json"),
                      lx_root / (stem + ".json")]:
                if p.exists() and args.commit:
                    p.unlink()

    if (by_failure.get("no_pdf") or corrupt_pdfs):
        n_refetch = len(by_failure.get("no_pdf", [])) + len(corrupt_pdfs)
        print(f"\n═══ STAGE 1: refetch {n_refetch} missing/corrupt PDFs ═══")
        run([sys.executable, "scripts/sync_allowlist_pdfs.py",
             "--state", args.state, "--year", str(args.year),
             "--commit", "--cdp", str(args.cdp),
             "--tabs", str(args.tabs), "--delay", str(args.delay)],
            args.commit)

    # STAGE 2: Cloud Vision for no_cloud_vision + no_pdf + empty_ocr.
    # For empty_ocr we need to DELETE the stale PP JSON first so Cloud
    # Vision doesn't skip it as "already processed". Same for empty_ocr's
    # downstream Gemini JSON — it'll be regenerated in stage 3.
    needs_cv = set(by_failure.get("no_cloud_vision", []))
    needs_cv.update(by_failure.get("no_pdf", []))
    needs_cv.update(by_failure.get("corrupt_pdf", []))

    # Handle empty_ocr — delete stale PP + LX so they get re-processed
    empty_ocr_files = by_failure.get("empty_ocr", [])
    if empty_ocr_files:
        pp_root = ROOT / "data/eci/for_ai" / f"preprocessed_{slug_year}"
        lx_root = ROOT / "data/eci/for_ai/llm_extracted" / slug_year
        deleted = 0
        for name in empty_ocr_files:
            stem = name[:-4] if name.endswith(".pdf") else name
            for p in [pp_root / (stem + ".json"), lx_root / (stem + ".json")]:
                if p.exists() and args.commit:
                    p.unlink()
                    deleted += 1
        print(f"\n  (empty_ocr) Would delete {deleted // 2 if not args.commit else deleted} stale PP+LX JSONs so Cloud Vision re-processes with Hindi hint")
        needs_cv.update(empty_ocr_files)

    if needs_cv:
        print(f"\n═══ STAGE 2: Cloud Vision on {len(needs_cv)} PDFs ═══")
        stage2_list = tmp_dir / "stage2_cv.txt"
        stage2_list.write_text("\n".join(sorted(needs_cv)) + "\n")

        cycle_dir = ROOT / "data/eci/raw_pdfs" / cycle_name
        raw_dir = cycle_dir / "raw_pdfs" if (cycle_dir / "raw_pdfs").exists() else cycle_dir
        run([sys.executable, "scripts/cloud_vision_preprocess.py",
             "--pdf-dir", str(raw_dir),
             "--out-dir", str(ROOT / "data/eci/for_ai" / f"preprocessed_{slug_year}"),
             "--pdf-allowlist", str(stage2_list)],
            args.commit)

    # STAGE 3: Gemini for no_gemini (+ newly-CV'd files from stage 2)
    needs_gem = set(by_failure.get("no_gemini", []))
    needs_gem.update(needs_cv)
    if needs_gem:
        print(f"\n═══ STAGE 3: Gemini on {len(needs_gem)} extractions ═══")
        # llm_extract processes ALL preprocessed files it hasn't cached.
        # Deleting old LX JSONs for these stems ensures they get re-done.
        lx_dir = ROOT / "data/eci/for_ai/llm_extracted" / slug_year
        removed = 0
        for name in needs_gem:
            stem = name[:-4] if name.endswith(".pdf") else name
            p = lx_dir / (stem + ".json")
            if p.exists():
                if args.commit:
                    p.unlink()
                removed += 1
        print(f"  Would delete {removed} stale Gemini JSONs so they get re-extracted")
        if args.commit and removed:
            print(f"  ✓ Deleted")

        run([sys.executable, "scripts/llm_extract_via_gemini.py",
             "--in-dir", str(ROOT / "data/eci/for_ai" / f"preprocessed_{slug_year}"),
             "--out-dir", str(lx_dir),
             "--state", args.state, "--year", str(args.year)],
            args.commit)

    # gemini_ok_but_apply — extraction is fine, but the DB matcher failed.
    # These need matcher fixes (aff_id-based join instead of name+const)
    # not reprocessing. Flag them separately.
    apply_gaps = by_failure.get("gemini_ok_but_apply", [])
    if apply_gaps:
        print(f"\n  ⚠ {len(apply_gaps)} 'gemini_ok_but_apply' cases won't be "
              f"recovered by this pipeline — they're (name, constituency) "
              f"collisions in the apply matcher. Fix requires switching "
              f"apply to affidavit_id-based join. See task #33.")
        for s in apply_gaps[:5]:
            print(f"    • {s}")
        if len(apply_gaps) > 5:
            print(f"    … + {len(apply_gaps) - 5} more")

    # STAGE 4: apply to SQLite
    print(f"\n═══ STAGE 4: apply to LOCAL SQLite ═══")
    run([sys.executable, "scripts/apply_llm_extraction.py",
         "--cycles", slug_year, "--allowlist-scope"],
        args.commit)

    if not args.commit:
        print(f"\n--- PLAN ONLY. Re-run with --commit to execute. ---")
    else:
        print(f"\n✓ Done. Verify with:")
        print(f"    python scripts/query_local_sqlite.py")


if __name__ == "__main__":
    main()
