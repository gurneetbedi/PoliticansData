# State Ingestion Playbook (v4)

Definitive step-by-step guide for adding a new Indian state's assembly election data to Lokvani. Reflects every bug, edge case, and refactor we've hit through Delhi → West Bengal.

**Read this once. Then follow it top-to-bottom for every state.**

---

## What changed in v4

- **ECI official results replace Wikipedia scraping.** New scripts `fetch_eci_results.py` + `load_eci_results.py` pull vote counts for every candidate per constituency (not just top 2), directly from `results.eci.gov.in`. The per-state Wikipedia loaders (`load_<state>_election_results.py`) are no longer used for winner marking on new states — they're kept only to generate the `--dry-run` diagnostic JSON if you need it as a sanity check.
- **DB renamed** from `politrack.db` → `lokvani.db`. Every script now defaults to the new name.
- **Local dev locked to SQLite.** `app/database.py` ignores any stray `DATABASE_URL` in your shell unless `USE_NEON=1`. Prod continues using Neon via Render's `RENDER=true` env var.
- **Multi-word state names now work everywhere.** `resolve_state` in `app/main.py` was rewritten to match against `ALL_STATES` config, and `load_eci_to_db.py` + `llm_extract_via_gemini.py` normalize `--state` to TitleCase internally.
- **Chrome CDP launcher.** For any script that needs to bypass Akamai (fetch_eci_affidavits, fetch_eci_results), use the dedicated launcher `python scripts/phase1_test_cdp_attach.py --launch` — this opens a fresh Chrome with a separate profile that Playwright can attach to.
- **`build_top_n_allowlist.py` accepts both schemas.** Wikipedia-style flat list AND ECI-results-style dict. Auto-detects.

---

## Preflight (once per session)

```bash
cd "/Users/gurneetbedi/Desktop/Claude/Project 1/Politicians Project"
source .venv-eci/bin/activate
export GOOGLE_APPLICATION_CREDENTIALS=secrets/lokvani-501706-a3b68700fa4a.json

# Launch dedicated Chrome (only needed if you don't already have it running).
# Opens a separate profile so it doesn't clash with your everyday Chrome.
python scripts/phase1_test_cdp_attach.py --launch
```

**Manually in that Chrome window:** navigate to `https://affidavit.eci.gov.in/` once to warm Akamai. Minimize the window; leave it running.

Set these per-state variables (used throughout):

```bash
STATE="West Bengal"          # TitleCase, quoted if multi-word
STATE_LC=westbengal           # lowercase single-word — file paths + dir names
YEAR=2026
STATE_CODE=S25                # ECI state code (see registry below)
AFFIDAVIT_URL="https://affidavit.eci.gov.in/CandidateCustomFilter?electionType=32-AC-GENERAL-3-60&election=32-AC-GENERAL-3-60&states=S25&submitName=100&page=2"
RESULTS_BASE="https://results.eci.gov.in/ResultAcGenMay2026/"
```

**Multi-word states** (Uttar Pradesh, Jammu and Kashmir, Andhra Pradesh, Madhya Pradesh, Tamil Nadu, Himachal Pradesh, Arunachal Pradesh, West Bengal): use quoted TitleCase for `STATE`, lowercase-with-no-spaces for `STATE_LC` (e.g. `uttarpradesh`, `westbengal`).

---

## Step 1 — Scrape official ECI results (NEW)

Directly replaces the old Wikipedia loader. Pulls all 3220-ish candidates for West Bengal (or whatever fits your state) with real vote counts.

```bash
mkdir -p data/eci/results

python scripts/fetch_eci_results.py \
  --state "$STATE" \
  --year $YEAR \
  --state-code $STATE_CODE \
  --results-base $RESULTS_BASE \
  --out data/eci/results/${STATE_LC}_${YEAR}_eci_results.json
```

Runs ~5 min (294 constituencies × ~1 sec each). Requires the dedicated Chrome from Preflight to be running. Output JSON has `constituencies[].candidates[]` with rank, party, EVM votes, postal votes, total votes, vote %, and a `won` flag.

**Verify:**
```bash
python3 -c "
import json
d = json.load(open('data/eci/results/${STATE_LC}_${YEAR}_eci_results.json'))
c = sum(len(x['candidates']) for x in d['constituencies'])
print(f'Constituencies: {len(d[\"constituencies\"])} · Candidates: {c}')
"
```

Expected: constituency count = assembly size; candidates = 3-8 × assembly size (varies by state).

---

## Step 2 — Fetch all candidate affidavits from ECI (30-60 min)

The affidavit fetcher uses the same dedicated Chrome as Step 1.

```bash
mkdir -p data/eci/raw_pdfs/${STATE_LC}-${YEAR}

python scripts/fetch_eci_affidavits.py \
  --listing-url "$AFFIDAVIT_URL" \
  --output data/eci/raw_pdfs/${STATE_LC}-${YEAR} \
  --concurrent-tabs 3 --dedupe-listing
```

If Chrome dies mid-run, re-run the same command (resume-safe via manifest.jsonl).

**Flatten if PDFs land in a subdirectory:**
```bash
[ -d data/eci/raw_pdfs/${STATE_LC}-${YEAR}/raw_pdfs ] && \
  mv data/eci/raw_pdfs/${STATE_LC}-${YEAR}/raw_pdfs/*.pdf \
     data/eci/raw_pdfs/${STATE_LC}-${YEAR}/
```

---

## Step 3 — Corrupt PDF sweep (only if > 2% corruption)

```bash
find data/eci/raw_pdfs/${STATE_LC}-${YEAR} -name "*_corrupt.txt" | wc -l
```

If under 2% of total, skip. Otherwise:
```bash
find data/eci/raw_pdfs/${STATE_LC}-${YEAR} -name "*_corrupt.txt" -delete
python scripts/refetch_corrupt_pdfs.py --state ${STATE_LC}-${YEAR}
python scripts/fetch_eci_affidavits.py --listing-url "$AFFIDAVIT_URL" \
  --output data/eci/raw_pdfs/${STATE_LC}-${YEAR} \
  --concurrent-tabs 1 --dedupe-listing
```

---

## Step 4 — Build top-N allowlist

The builder auto-detects the ECI-results JSON schema and skips NOTA entries.

```bash
mkdir -p data/allowlists

# For cost control: top-2 (winner + runner-up) → ~588 PDFs for WB.
python scripts/build_top_n_allowlist.py \
  --results data/eci/results/${STATE_LC}_${YEAR}_eci_results.json \
  --manifest data/eci/raw_pdfs/${STATE_LC}-${YEAR}/manifest.jsonl \
  --output data/allowlists/${STATE_LC}_${YEAR}_top2.txt \
  --top-n 2

# For richer coverage: top-4 → ~1176 PDFs. Use later if desired.
```

Expected log line: `(detected ECI-results schema, converted N constituencies)`.

---

## Step 5 — OCR only the allowlist (~10-15 min, ~$0.75 for top-2)

```bash
python scripts/cloud_vision_preprocess.py \
  --pdf-dir data/eci/raw_pdfs/${STATE_LC}-${YEAR} \
  --out-dir data/eci/for_ai/preprocessed_${STATE_LC}_${YEAR} \
  --pdf-allowlist data/allowlists/${STATE_LC}_${YEAR}_top2.txt \
  --workers 4
```

---

## Step 6 — Structured extract → CSV

```bash
python scripts/extract_structured.py \
  --in-dir data/eci/for_ai/preprocessed_${STATE_LC}_${YEAR} \
  --out-dir data/eci/for_ai/extracted \
  --prefix ${STATE_LC}_${YEAR}
```

---

## Step 7 — Load to provisional table

```bash
python scripts/load_eci_to_db.py \
  --csv data/eci/for_ai/extracted/${STATE_LC}_${YEAR}_structured.csv \
  --state "$STATE" \
  --election-year $YEAR \
  --election-type Assembly \
  --manifest data/eci/raw_pdfs/${STATE_LC}-${YEAR}/manifest.jsonl
```

`--state` is normalized to TitleCase internally, so any case works — but stay consistent by passing the exact `$STATE` variable.

---

## Step 8 — Migrate provisional → canonical

```bash
env -u DATABASE_URL python scripts/migrate_to_eci_only.py
```

Creates `constituencies` + initial `election_appearances` rows for your state.

**Verify DB state:**
```bash
python3 -c "
import sqlite3
c = sqlite3.connect('lokvani.db').cursor()
for r in c.execute('SELECT s.name, COUNT(c.id) FROM states s LEFT JOIN constituencies c ON c.state_id=s.id WHERE lower(s.name) LIKE \"%$STATE_LC%\" GROUP BY s.name'):
    print(r)
"
```
Should show `('$STATE', <assembly-size>)`.

---

## Step 9 — Apply ECI results (marks winners + votes)

```bash
python scripts/load_eci_results.py \
  --results data/eci/results/${STATE_LC}_${YEAR}_eci_results.json
```

Fuzzy-matches candidate names in the DB to the ECI results and sets `won`, `votes_received`, `vote_share_pct`. Expected: `Winners flagged: <assembly-size>, Updated: <top-N × assembly-size>`.

---

## Step 10 — Gemini LLM extraction (~15-30 min)

```bash
mkdir -p data/eci/for_ai/llm_extracted/${STATE_LC}_${YEAR}

python scripts/llm_extract_via_gemini.py \
  --in-dir data/eci/for_ai/preprocessed_${STATE_LC}_${YEAR} \
  --out-dir data/eci/for_ai/llm_extracted/${STATE_LC}_${YEAR} \
  --state "$STATE" \
  --year $YEAR \
  --workers 4
```

Idempotent — re-run to pick up any files that failed. Pass `--refresh` to force re-extraction.

---

## Step 11 — Apply LLM extraction

```bash
python scripts/apply_llm_extraction.py --cycles ${STATE_LC}_${YEAR}
```

Fills in financials (assets, liabilities, movable/immovable) + criminal case counts on the appearance rows.

---

## Step 12 — Register state in the app

**Edit `app/states.py`:**
- Add `"${STATE_LC}": ${STATE_UC}_CONFIG,` to the `ALL_STATES` dict (the visible one) — NOT `_ALL_STATES_HISTORICAL`.
- Remove from `_HIDDEN_STATES` if present.

**Edit `scripts/postmigrate.sh`** — append before the verification block:
```bash
echo ""
echo "  $STATE ($YEAR)"
python scripts/load_eci_results.py --results data/eci/results/${STATE_LC}_${YEAR}_eci_results.json || true
```

Note: replaces the old `load_<state>_election_results.py` line.

---

## Step 13 — Verify locally

```bash
python3 -c "
import sqlite3
c = sqlite3.connect('lokvani.db').cursor()
r = c.execute('''SELECT COUNT(*), SUM(CASE WHEN ea.won=1 THEN 1 ELSE 0 END),
    SUM(CASE WHEN ea.total_assets_inr IS NOT NULL THEN 1 ELSE 0 END)
    FROM election_appearances ea JOIN elections e ON ea.election_id=e.id
    JOIN states s ON e.state_id=s.id WHERE s.name=?''', ('$STATE',)).fetchone()
print(f'total={r[0]} winners={r[1]} with_assets={r[2]}')
"
```

All three should be non-zero. **If any is zero, DO NOT proceed to sync.**

Restart the server (uses local SQLite):
```bash
pkill -9 -f uvicorn
uvicorn app.main:app --reload
```

Hard-refresh the browser (Cmd+Shift+R), hover the state on the map — should show correct MLA count and financial data.

---

## Step 14 — Sync to Neon + deploy

```bash
# Neon connection needed for sync only. Sourced in subshell to avoid polluting.
(set -a; source secrets/.env; set +a; python scripts/sqlite_to_postgres.py --reset)

git add -A
git commit -m "Add $STATE $YEAR election data"
git push
```

Render auto-deploys. Wait ~2 min, then open the prod site and spot-check 3 constituencies.

---

# Reference Tables

## Assembly sizes

| State | Seats | Priority |
|---|---|---|
| Puducherry | 30 | Small (done) |
| Sikkim | 32 | Small (done) |
| Goa | 40 | Small (done) |
| Mizoram | 40 | Small (done) |
| Meghalaya | 60 | Small (done) |
| Nagaland | 60 | Small (done) |
| Manipur | 60 | Small (done) |
| Tripura | 60 | Small (done) |
| Arunachal | 60 | Small (done) |
| Delhi | 70 | Small (done) |
| Uttarakhand | 70 | Small (done) |
| Himachal | 68 | Small (done) |
| Haryana | 90 | Small (done) |
| J&K | 90 | Small (done) |
| Jharkhand | 81 | Small (done) |
| Chhattisgarh | 90 | Small (done) |
| Punjab | 117 | Small (done) |
| Telangana | 119 | Small (done) |
| Assam | 126 | Mid (done) |
| Kerala | 140 | Mid (done) |
| Gujarat | 182 | Mid (done) |
| **Tamil Nadu** | **234** | Large (in progress) |
| **West Bengal** | **294** | Large (in progress) |
| **Odisha** | **147** | Mid |
| **Andhra Pradesh** | **175** | Mid |
| **Rajasthan** | **200** | Mid |
| **Karnataka** | **224** | Mid |
| **Madhya Pradesh** | **230** | Mid |
| **Bihar** | **243** | Large |
| **Maharashtra** | **288** | Large |
| **Uttar Pradesh** | **403** | Large |

## ECI results portal URLs (registry)

| Election window | States held | Base URL |
|---|---|---|
| **May 2026** | Assam, Kerala, West Bengal, Tamil Nadu, Puducherry | `https://results.eci.gov.in/ResultAcGenMay2026/` |
| **Nov 2024** | Maharashtra, Jharkhand | `https://results.eci.gov.in/ResultAcGenNov2024/` (verify) |
| **Older** | To be probed as encountered | Grep from `results.eci.gov.in` archive |

## ECI state codes (for `--state-code` and affidavit URL `states=SXX`)

| State | Code | State | Code |
|---|---|---|---|
| Andhra Pradesh | S01 | Manipur | S14 |
| Arunachal Pradesh | S02 | Meghalaya | S15 |
| Assam | S03 | Mizoram | S16 |
| Bihar | S04 | Nagaland | S17 |
| Goa | S05 | Odisha | S18 |
| Gujarat | S06 | Punjab | S19 |
| Haryana | S07 | Rajasthan | S20 |
| Himachal Pradesh | S08 | Sikkim | S21 |
| J&K | S09 | Tamil Nadu | S22 |
| Karnataka | S10 | Tripura | S23 |
| Kerala | S11 | Uttar Pradesh | S24 |
| MP | S12 | West Bengal | S25 |
| Maharashtra | S13 | Chhattisgarh | S26 |
| Jharkhand | S27 | Uttarakhand | S28 |
| Telangana | S29 | Delhi | U05 |
| Puducherry | U06 | | |

## Script flag cheatsheet

| Script | Required flags |
|---|---|
| `fetch_eci_results.py` | `--state --year --state-code --results-base --out` |
| `load_eci_results.py` | `--results` |
| `fetch_eci_affidavits.py` | `--listing-url --output` |
| `refetch_corrupt_pdfs.py` | `--state` (hyphenated: `westbengal-2026`) |
| `build_top_n_allowlist.py` | `--results --manifest --output --top-n` (auto-detects schema) |
| `cloud_vision_preprocess.py` | `--pdf-dir --out-dir` (opt: `--pdf-allowlist --workers --limit`) |
| `extract_structured.py` | `--in-dir --out-dir --prefix` |
| `load_eci_to_db.py` | `--csv --state --election-year --election-type --manifest` |
| `migrate_to_eci_only.py` | none (prepend `env -u DATABASE_URL`) |
| `llm_extract_via_gemini.py` | `--in-dir --out-dir --state --year --workers` |
| `apply_llm_extraction.py` | `--cycles` (underscored: `westbengal_2026`) |
| `sqlite_to_postgres.py` | `--reset` (requires `DATABASE_URL` set inline) |

## Naming conventions

| Form | Where | Example |
|---|---|---|
| **TitleCase quoted** | `--state` values, `states.name`, LLM JSON's state field, loader's STATE_NAME, ECI results scraper | `"West Bengal"`, `"Uttar Pradesh"` |
| **lowercase single-word** | Directory suffixes, filenames, ALL_STATES key | `westbengal`, `uttarpradesh` |
| **hyphenated** | Directory suffix under `raw_pdfs/`, `--state` for `refetch_corrupt_pdfs.py` | `westbengal-2026` |
| **underscored** | Directory suffix under `preprocessed_/llm_extracted/`, `--cycles` for `apply_llm_extraction.py` | `westbengal_2026` |

---

# Common failures & fixes

| Symptom | Cause | Fix |
|---|---|---|
| `fetch_eci_results.py`: 403 Forbidden | Akamai TLS-fingerprinting | Use CDP-attached Chrome via `phase1_test_cdp_attach.py --launch`, warm it manually |
| `Could not attach to Chrome on port 9222` | Chrome not launched with debug port | Kill Chrome, run `phase1_test_cdp_attach.py --launch` |
| Fetcher: "Frame has been detached" | Chrome instability | Re-run — auto-retries |
| Fetcher: 5-15% corrupt PDFs | `--concurrent-tabs` too high | Step 3 with `--concurrent-tabs 1` |
| Allowlist: `'str' object has no attribute 'get'` | Old build_top_n_allowlist.py without schema detection | Update to latest — check line 121 has `# Detect schema` |
| OCR: `No PDFs in <dir>` | Fetcher put files in subdir | Run flatten command from Step 2 |
| Gemini: 403 | Wrong GCP creds | Re-export `GOOGLE_APPLICATION_CREDENTIALS` |
| `load_eci_results.py`: `No appearances in DB` | Haven't run Steps 7-8 yet | Complete affidavit pipeline first |
| Loader/results: 0 matches | State-name case mismatch | Verify DB with query; update `states.name` if needed |
| Site shows "Data Not Available" locally | State not in `ALL_STATES` or server not restarted | Verify Step 12 + `pkill -9 -f uvicorn` |
| Site queries hit Neon locally | `DATABASE_URL` in shell | Fixed automatically by `app/database.py` — no manual unset needed |
| Site shows 0 MLAs on prod | Neon not synced | Step 14 |
| DB has `states.name = 'Tamilnadu'` but loader queries `'Tamil Nadu'` | `load_eci_to_db.py` TitleCased single-word input | `UPDATE states SET name='Tamil Nadu' WHERE name='Tamilnadu'`; always pass quoted TitleCase |

---

# Key rules

1. **`--state` value = TitleCase, quoted if multi-word.** Never `--state westbengal` or `--state west bengal`.
2. **Assembly cycle URL** discoveries go into the registry — commit the update as you learn them.
3. **The dedicated Chrome must stay running** during any step that touches ECI portals (Steps 1, 2, 3).
4. **`app/states.py`** — add to `ALL_STATES`, not `_ALL_STATES_HISTORICAL`.
5. **`postmigrate.sh`** must contain your state's `load_eci_results.py` line, not the old Wikipedia loader.
6. **`lokvani.db`** is the DB file. Never manually create `politrack.db` — that's the old name.
7. **After DB changes**: hard-kill uvicorn (`pkill -9 -f uvicorn`) then restart.
8. **Verify locally before syncing**: winners > 0 AND with_assets > 0 in SQLite. If not, don't push.
9. **Sync command needs DATABASE_URL inline**:
   ```bash
   (set -a; source secrets/.env; set +a; python scripts/sqlite_to_postgres.py --reset)
   ```
10. **`top-N` in the allowlist** controls OCR + Gemini cost. Top-2 is cheapest (~$0.75 for WB). Top-4 costs ~2x and gives more candidates on the site. You can re-run Step 4-11 later with a higher N.

---

# Post-Ingestion Sanity Checks

After committing + Render deploy completes:

1. Open `https://lokvani.example.com/state/${STATE_LC}` — should render with correct constituency count.
2. Click 3 random constituencies — winner names should match ECI results portal.
3. Check the India map — state should be shaded appropriately (green/tan) with correct MLA count.
4. Hover the state on the map — tooltip should show correct MLA count + election year.
5. Check the coverage snapshot — party-wise coverage table should populate.

If any check fails, fix locally, re-run the affected step, re-sync, re-push.
