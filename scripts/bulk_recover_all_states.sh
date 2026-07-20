#!/usr/bin/env bash
# Bulk recovery loop — for each state with bad extractions, run:
#   1. recover_bad_ocr_state.py --commit  (deletes empty PP+LX JSONs, writes mini-allowlist)
#   2. cloud_vision_preprocess.py         (Hindi/Malayalam/Telugu/etc hint auto-applied)
#   3. llm_extract_via_gemini.py          (fills in structured data)
#
# Sequential per state — safe to Ctrl+C between states.
# Dry-run per state first via "recover_bad_ocr_state.py --state X --year Y" (no --commit)
# to see how many will be touched before committing to the full loop.
#
# Usage:
#   cd "/Users/gurneetbedi/Desktop/Claude/Project 1/Politicians Project"
#   source secrets/.env
#   bash scripts/bulk_recover_all_states.sh
#
# Ordered by "bad" count descending — biggest impact first.

set -e   # exit on any command failure so we notice problems mid-loop
cd "$(dirname "$0")/.."

# Sanity: env vars set?
if [ -z "$GCP_PROJECT" ] || [ -z "$GOOGLE_APPLICATION_CREDENTIALS" ]; then
    echo "✗ Run 'source secrets/.env' first — GCP env vars missing"
    exit 1
fi

# state:year:cycle_slug:llm_slug   (llm_slug is state.lower().replace(" ",""))
STATES=(
    "Madhya Pradesh:2023:madhyapradesh-2023:madhyapradesh_2023"
    "Uttar Pradesh:2022:uttarpradesh-2022:uttarpradesh_2022"
    "Gujarat:2022:gujarat-2022:gujarat_2022"
    "Maharashtra:2024:maharashtra-2024:maharashtra_2024"
    "Bihar:2025:bihar-2025:bihar_2025"
    "Karnataka:2023:karnataka-2023:karnataka_2023"
    "Tamil Nadu:2026:tamilnadu-2026:tamilnadu_2026"
    "Kerala:2026:kerala-2026:kerala_2026"
    "Andhra Pradesh:2024:andhrapradesh-2024:andhrapradesh_2024"
    "Odisha:2024:odisha-2024:odisha_2024"
    "West Bengal:2026:westbengal-2026:westbengal_2026"
    "Telangana:2023:telangana-2023:telangana_2023"
)

for spec in "${STATES[@]}"; do
    IFS=':' read -r state year cycle slug <<< "$spec"

    echo ""
    echo "══════════════════════════════════════════════════════════════"
    echo "▶ $state $year"
    echo "══════════════════════════════════════════════════════════════"

    mini_allow="/tmp/recover_${slug}.txt"

    # Step 1 — identify bads + delete their JSONs (idempotent)
    python scripts/recover_bad_ocr_state.py \
        --state "$state" --year "$year" --commit

    # If the mini-allowlist ended up empty (no bads for this state), skip
    if [ ! -s "$mini_allow" ]; then
        echo "  (no bads to recover for $state — skipping CV+Gemini)"
        continue
    fi

    # Step 2 — re-Cloud-Vision the bad set with the right language hint
    python scripts/cloud_vision_preprocess.py \
        --pdf-dir "data/eci/raw_pdfs/${cycle}/raw_pdfs" \
        --out-dir "data/eci/for_ai/preprocessed_${slug}" \
        --pdf-allowlist "$mini_allow"

    # Step 3 — re-Gemini the newly-OCR'd set
    python scripts/llm_extract_via_gemini.py \
        --in-dir "data/eci/for_ai/preprocessed_${slug}" \
        --out-dir "data/eci/for_ai/llm_extracted/${slug}" \
        --state "$state" --year "$year"

    echo "  ✓ $state $year done"
done

echo ""
echo "══════════════════════════════════════════════════════════════"
echo "All states processed. Verify with:"
echo "  python scripts/state_status_report.py"
echo "══════════════════════════════════════════════════════════════"
