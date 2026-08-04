"""
One-shot rebuild of the canonical tables from provisional data +
LLM extractions + ECI results.

Runs, in order:
  1. migrate_to_eci_only.py  — wipes canonical, inserts elections /
     politicians / election_appearances from eci_candidates_provisional
  2. apply_llm_extraction.py  — updates election_appearances with
     assets, liabilities, criminal case counts, and inserts detail
     rows into `assets` / `liabilities` / `criminal_cases`
  3. populate_winners_from_eci_results.py  — updates won /
     votes_received / vote_share_pct

Why this exists
---------------
Step 1 DELETEs and re-inserts every row in election_appearances (and
child detail tables). Any state left in place by steps 2 and 3 is
lost. Before this wrapper existed, ingesting a new state cycle meant
manually re-running steps 2 and 3 for EVERY previously-ingested cycle
or losing all their LLM + winner data.

USAGE
-----
    # Full rebuild after loading new provisional data
    python scripts/refresh_canonical.py

    # Point at a non-default DB (e.g. /tmp copy)
    python scripts/refresh_canonical.py --db /tmp/scratch.db

    # Skip a stage (e.g. you only want winners refreshed)
    python scripts/refresh_canonical.py --skip-migrate --skip-apply
"""
from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def _run(cmd: list[str], label: str) -> None:
    print(f"\n{'=' * 72}\n{label}\n  {' '.join(cmd)}\n{'=' * 72}",
          file=sys.stderr)
    r = subprocess.run(cmd, cwd=str(PROJECT_ROOT))
    if r.returncode != 0:
        sys.exit(f"\n✗ {label} failed with exit code {r.returncode}. "
                 f"Stopping — fix the failing step, then re-run.")


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                  formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db", default=str(PROJECT_ROOT / "lokvani.db"))
    ap.add_argument("--skip-migrate", action="store_true",
                    help="Skip migrate_to_eci_only.py (steps 2+3 only)")
    ap.add_argument("--skip-apply", action="store_true",
                    help="Skip apply_llm_extraction.py")
    ap.add_argument("--skip-winners", action="store_true",
                    help="Skip populate_winners_from_eci_results.py")
    ap.add_argument("--skip-backup", action="store_true",
                    help="Passed through to migrate_to_eci_only.py")
    args = ap.parse_args()

    py = sys.executable

    if not args.skip_migrate:
        cmd = [py, "scripts/migrate_to_eci_only.py", "--db", args.db]
        if args.skip_backup:
            cmd.append("--skip-backup")
        _run(cmd, "STEP 1/3  migrate_to_eci_only.py")

    if not args.skip_apply:
        _run([py, "scripts/apply_llm_extraction.py", "--db", args.db],
             "STEP 2/3  apply_llm_extraction.py (all cycles)")

    if not args.skip_winners:
        _run([py, "scripts/populate_winners_from_eci_results.py",
              "--db", args.db],
             "STEP 3/3  populate_winners_from_eci_results.py (all cycles)")

    print(f"\n✓ Canonical rebuild complete for {args.db}", file=sys.stderr)


if __name__ == "__main__":
    main()
