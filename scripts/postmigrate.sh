#!/usr/bin/env bash
#
# post-migrate: re-apply everything the migration wipes.
#
# scripts/migrate_to_eci_only.py DELETEs every canonical table and
# repopulates from `eci_candidates_provisional`. That leaves:
#   - Phase-0 NULL for all financial/criminal fields
#   - won=False for every row
#   - votes_received=NULL, vote_share_pct=NULL
#   - match_score=NULL, match_method=NULL
#
# The LLM extractions AND winner data need to be re-applied.
# This script does that in the right order for every state we currently
# have data for.
#
# Order of operations:
#   1. apply_llm_extraction.py  — fills wealth/cases/education fields
#   2. load_eci_results.py      — marks winners + votes (preferred; uses
#                                 ECI Statistical Report JSON)
#   3. Per-state Wikipedia loaders (fallback for states without an ECI
#                                    JSON yet)
#
# Usage:
#   bash scripts/postmigrate.sh
#
# Add a new state:
#   - If you have an ECI JSON at data/eci/results/<slug>_<year>_eci_results.json,
#     it's picked up automatically by the loop in Step 2 — no code change.
#   - If it's still on the Wikipedia loader, add a line under Step 3.

set -euo pipefail
cd "$(dirname "$0")/.."

echo "=========================================="
echo "POST-MIGRATE: applying LLM + winner data"
echo "=========================================="

# ─────────────────────────────────────────────────────────────────────────
# Step 1 — LLM extractions (financials + criminal cases + education)
# ─────────────────────────────────────────────────────────────────────────
echo ""
echo "-- Step 1: LLM extractions (financials + criminal cases) --"
python scripts/apply_llm_extraction.py

# ─────────────────────────────────────────────────────────────────────────
# Step 2 — ECI Statistical Report winner loads (preferred)
#
# Auto-discovers every data/eci/results/*_eci_results.json file. If a new
# state appears there, no code change needed here — just drop the JSON.
# The loader is idempotent + safe to re-run.
# ─────────────────────────────────────────────────────────────────────────
echo ""
echo "-- Step 2: ECI Statistical Report winners --"
shopt -s nullglob
for f in data/eci/results/*_eci_results.json; do
  # Extract "<state> <year>" for the log line
  base=$(basename "$f" _eci_results.json)   # e.g. "rajasthan_2023"
  state_slug=${base%_*}
  year=${base##*_}
  echo ""
  echo "  ${state_slug} (${year})"
  python scripts/load_eci_results.py --results "$f" || true
done

# ─────────────────────────────────────────────────────────────────────────
# Step 3 — Wikipedia winner loaders (fallback for states with no ECI JSON yet)
#
# When a state graduates to an ECI Statistical Report parse
# (data/eci/results/<slug>_<year>_eci_results.json exists), remove its
# line from below — Step 2's loop will handle it.
# ─────────────────────────────────────────────────────────────────────────
echo ""
echo "-- Step 3: Wikipedia winner loaders (fallback) --"

echo ""
echo "  Delhi (2020 + 2025)"
python scripts/load_delhi_election_results.py || true

echo ""
echo "  Punjab (2022)"
python scripts/load_punjab_election_results.py --year 2022 || true

echo ""
echo "  Puducherry (2021)"
python scripts/load_puducherry_election_results.py --year 2021 || true

echo ""
echo "  Goa (2022)"
python scripts/load_goa_election_results.py --year 2022 || true

echo ""
echo "  Sikkim (2019 + 2024)"
python scripts/load_sikkim_election_results.py --year 2019 || true
python scripts/load_sikkim_election_results.py --year 2024 || true

echo ""
echo "  Mizoram (2023)"
python scripts/load_mizoram_election_results.py --year 2023 || true

echo ""
echo "  Haryana (2019 + 2024)"
python scripts/load_haryana_election_results.py --year 2019 || true
python scripts/load_haryana_election_results.py --year 2024 || true

echo ""
echo "  Arunachal Pradesh (2024)"
python scripts/load_arunachal_election_results.py --year 2024 || true

echo ""
echo "  Manipur (2022)"
python scripts/load_manipur_election_results.py --year 2022 || true

echo ""
echo "  Uttarakhand (2022)"
python scripts/load_uttarakhand_election_results.py --year 2022 || true

echo ""
echo "  Jharkhand (2024)"
python scripts/load_jharkhand_election_results.py --year 2024 || true

echo ""
echo "  Jammu and Kashmir (2024)"
python scripts/load_jk_election_results.py --year 2024 || true

echo ""
echo "  Chhattisgarh (2023)"
python scripts/load_chhattisgarh_election_results.py --year 2023 || true

echo ""
echo "  Telangana (2023)"
python scripts/load_telangana_election_results.py --year 2023 || true

echo ""
echo "  Assam (2026)"
python scripts/load_assam_election_results.py --year 2026 || true

echo ""
echo "  Kerala (2026)"
python scripts/load_kerala_election_results.py --year 2026 || true

echo ""
echo "  Tamil Nadu (2026)"
python scripts/load_tamilnadu_election_results.py --year 2026 || true

# ─────────────────────────────────────────────────────────────────────────
# Verification
# ─────────────────────────────────────────────────────────────────────────
echo ""
echo "=========================================="
echo "POST-MIGRATE DONE"
echo "=========================================="
echo ""
echo "Per-state coverage snapshot:"
python3 - <<'PYEOF'
import sqlite3
c = sqlite3.connect('lokvani.db').cursor()
print(f"  {'State':22s} {'total':>7s} {'winners':>8s} {'wealth':>7s} {'cases':>7s} {'exact':>7s} {'fuzzy':>7s}")
print(f"  {'-'*22} {'-'*7} {'-'*8} {'-'*7} {'-'*7} {'-'*7} {'-'*7}")
for row in c.execute("""
    SELECT s.name,
      COUNT(*) as total,
      SUM(CASE WHEN ea.won = 1 THEN 1 ELSE 0 END) as winners,
      SUM(CASE WHEN ea.total_assets_inr IS NOT NULL THEN 1 ELSE 0 END) as wealth,
      SUM(CASE WHEN ea.criminal_cases_count IS NOT NULL THEN 1 ELSE 0 END) as cases,
      SUM(CASE WHEN ea.match_method='exact' THEN 1 ELSE 0 END) as exact_,
      SUM(CASE WHEN ea.match_method='fuzzy' THEN 1 ELSE 0 END) as fuzzy
    FROM election_appearances ea
    JOIN elections e ON ea.election_id = e.id
    JOIN states s ON e.state_id = s.id
    GROUP BY s.name ORDER BY s.name
"""):
    state, total, wins, wl, cs, ex, fz = row
    print(f"  {state:22s} {total:>7d} {wins:>8d} {wl:>7d} {cs:>7d} {ex:>7d} {fz:>7d}")
PYEOF

echo ""
echo "Pipeline error summary since last run:"
python scripts/pipeline_errors.py 2>&1 | head -20 || true
