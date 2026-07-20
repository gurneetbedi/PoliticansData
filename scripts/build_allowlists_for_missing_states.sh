#!/usr/bin/env bash
# For each state cycle that has:
#   - a results JSON at data/eci/results/<slug_year>_results.json
#   - a manifest.jsonl at data/eci/raw_pdfs/<cycle-slug>/manifest.jsonl
#   - but NO allowlist at data/allowlists/<slug_year>[_topN].txt
#
# Generate a top-2 allowlist. Sequential, safe to Ctrl+C between states.
#
# Usage:
#   cd "/Users/gurneetbedi/Desktop/Claude/Project 1/Politicians Project"
#   bash scripts/build_allowlists_for_missing_states.sh
#   bash scripts/build_allowlists_for_missing_states.sh 4   # top-4 instead of top-2

set -e
cd "$(dirname "$0")/.."

TOP_N="${1:-2}"

# cycle-slug : results-slug   (results-slug is what the results JSON uses)
STATES=(
    "arunachal-2024:arunachal_2024"
    "chhattisgarh-2023:chhattisgarh_2023"
    "delhi-2020:delhi_2020"
    "delhi-2025:delhi_2025"
    "goa-2022:goa_2022"
    "haryana-2019:haryana_2019"
    "haryana-2024:haryana_2024"
    "himachal-2022:himachal_2022"
    "jharkhand-2024:jharkhand_2024"
    "jk-2024:jk_2024"
    "manipur-2022:manipur_2022"
    "meghalaya-2023:meghalaya_2023"
    "mizoram-2023:mizoram_2023"
    "nagaland-2023:nagaland_2023"
    "puducherry-2021:puducherry_2021"
    "punjab-2022:punjab_2022"
    "sikkim-2019:sikkim_2019"
    "sikkim-2024:sikkim_2024"
    "tripura-2023:tripura_2023"
    "uttarakhand-2022:uttarakhand_2022"
)

echo "Building top-${TOP_N} allowlists for ${#STATES[@]} states..."
echo ""

built=0
skipped=0
missing=0
for spec in "${STATES[@]}"; do
    IFS=':' read -r cycle slug <<< "$spec"

    results="data/eci/results/${slug}_results.json"
    manifest="data/eci/raw_pdfs/${cycle}/manifest.jsonl"
    output="data/allowlists/${slug}_top${TOP_N}.txt"

    # Check inputs exist
    if [ ! -f "$results" ]; then
        echo "  ✗ ${slug}: no results JSON at ${results}"
        missing=$((missing + 1))
        continue
    fi
    if [ ! -f "$manifest" ]; then
        echo "  ✗ ${slug}: no manifest at ${manifest}"
        missing=$((missing + 1))
        continue
    fi
    if [ -f "$output" ] || ls "data/allowlists/${slug}_top"*.txt 2>/dev/null 1>&2; then
        echo "  ↷ ${slug}: allowlist already exists (skipping)"
        skipped=$((skipped + 1))
        continue
    fi

    echo "▶ ${slug}..."
    python scripts/build_top_n_allowlist.py \
        --results "$results" \
        --manifest "$manifest" \
        --output "$output" \
        --top-n "$TOP_N"
    built=$((built + 1))
done

echo ""
echo "══════════════════════════════════════════════════════════════"
echo "Built:   $built"
echo "Skipped: $skipped  (already had allowlist)"
echo "Missing: $missing  (no results JSON or manifest)"
echo "══════════════════════════════════════════════════════════════"
echo ""
echo "Next: python scripts/state_status_report.py  (verify)"
