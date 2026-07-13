#!/usr/bin/env bash
#
# post-migrate: re-apply everything the migration wipes.
#
# scripts/migrate_to_eci_only.py DELETEs every canonical table and
# repopulates from provisional. That leaves:
#   - Phase-0 NULL for all financial/criminal fields
#   - won=False for every row
#   - votes_received=NULL, vote_share_pct=NULL
#
# The LLM extractions and Wikipedia winner data need to be re-applied.
# This script does that in the right order for every state we currently
# have data for.
#
# Usage:
#   bash scripts/postmigrate.sh
#
# Add new states as we ingest them (Sikkim, Mizoram, etc.).

set -euo pipefail
cd "$(dirname "$0")/.."

echo "=========================================="
echo "POST-MIGRATE: applying LLM + winner data"
echo "=========================================="

echo ""
echo "-- Step 1: LLM extractions (financials + criminal cases) --"
python scripts/apply_llm_extraction.py

echo ""
echo "-- Step 2: Wikipedia winner flags + vote counts --"
echo ""
echo "  Delhi (2020 + 2025)"
python scripts/load_delhi_election_results.py

echo ""
echo "  Punjab (2022)"
python scripts/load_punjab_election_results.py --year 2022

echo ""
echo "  Puducherry (2021)"
python scripts/load_puducherry_election_results.py --year 2021

echo ""
echo "  Goa (2022)"
python scripts/load_goa_election_results.py --year 2022

echo ""
echo "  Sikkim (2019)"
# Loader falls back gracefully with a warning if SIKKIM_2019_COLS is
# not yet tuned — so this line is safe to include even before we've
# run --dump-tables and filled in the column map.
python scripts/load_sikkim_election_results.py --year 2019 || true
python scripts/load_sikkim_election_results.py --year 2024 || true

echo ""
echo "  Mizoram (2023)"
python scripts/load_mizoram_election_results.py --year 2023 || true

echo ""
echo "  Nagaland (2023)"
python scripts/load_nagaland_election_results.py --year 2023 || true

echo ""
echo "  Himachal Pradesh (2022)"
python scripts/load_himachal_election_results.py --year 2022 || true

echo ""
echo "  Haryana (2019)"
python scripts/load_haryana_election_results.py --year 2019 || true
python scripts/load_haryana_election_results.py --year 2024 || true

echo ""
echo "  Arunachal Pradesh (2024)"
python scripts/load_arunachal_election_results.py --year 2024 || true

echo ""
echo "  Manipur (2022)"
python scripts/load_manipur_election_results.py --year 2022 || true

echo ""
echo "  Tripura (2023)"
python scripts/load_tripura_election_results.py --year 2023 || true

echo ""
echo "  Meghalaya (2023)"
python scripts/load_meghalaya_election_results.py --year 2023 || true

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


echo "  Telangana (2023)"
python scripts/load_telangana_election_results.py --year 2023 || true


echo ""
echo "  Assam (2026)"
python scripts/load_assam_election_results.py --year 2026 || true


echo ""
echo "  Kerala (2026)"
python scripts/load_kerala_election_results.py --year 2026 || true

echo ""
echo "  Gujarat (2022)"
python scripts/load_gujarat_election_results.py --year 2022 || true


echo ""
echo "  Tamil Nadu (2026)"
python scripts/load_tamilnadu_election_results.py --year 2026 || true



echo ""
echo "  West Bengal (2026)"
python scripts/load_westbengal_election_results.py --year 2026 || true

echo ""
echo "  Odisha (2024)"
python scripts/load_odisha_election_results.py --year 2024 || true



echo ""
echo "  Andra Pardesh (2024)"
python scripts/load_odisha_election_results.py --year 2024 || true

echo ""
echo "  Rajasthan (2023)"
python scripts/load_rajasthan_election_results.py --year 2023 || true

echo ""
echo "=========================================="
echo "POST-MIGRATE DONE"
echo "=========================================="
echo ""
echo "Verify per-state coverage:"
python3 - <<'PYEOF'
import sqlite3
c = sqlite3.connect('lokvani.db').cursor()
print(f"  {'State':12s} {'total':>7s} {'winners':>8s} {'verified_LLM':>13s}")
print(f"  {'-'*12} {'-'*7} {'-'*8} {'-'*13}")
for row in c.execute("""
    SELECT s.name,
      COUNT(*) as total,
      SUM(CASE WHEN ea.won = 1 THEN 1 ELSE 0 END) as winners,
      SUM(CASE WHEN ea.criminal_cases_count IS NOT NULL THEN 1 ELSE 0 END) as verified
    FROM election_appearances ea
    JOIN elections e ON ea.election_id = e.id
    JOIN states s ON e.state_id = s.id
    GROUP BY s.name ORDER BY s.name
"""):
    state, total, wins, ver = row
    print(f"  {state:12s} {total:>7d} {wins:>8d} {ver:>13d}")
PYEOF
