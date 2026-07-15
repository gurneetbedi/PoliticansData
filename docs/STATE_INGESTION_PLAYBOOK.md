# State Ingestion Playbook (v5)

Definitive step-by-step guide for adding a new Indian state's assembly election data to Lokvani. Reflects every bug, edge case, and refactor we've hit through Delhi → West Bengal → Rajasthan → MP.

**Read this once. Then follow it top-to-bottom for every state.**

---

## What changed in v5

- **ECI Statistical Report is now the primary results source.** Where the ECI results portal isn't available (older cycles, or portals that have been retired), we parse the "10-Detailed-Results" PDF or Excel directly via `parse_eci_statistical_report_pdf.py` / `parse_eci_statistical_report.py`. These are same-output-schema replacements for `fetch_eci_results.py`. Skips the fragile Cloud Vision → regex chain that lost data for Gujarat / HP / Karnataka / Meghalaya / Tripura.
- **Parser emits full candidate record.** Every candidate now has `name, party, gender, age, category, evm_votes, postal_votes, total_votes, vote_pct` — matches the "required fields" of the pipeline standard.
- **Top-3 is the new default for OCR + Gemini** (was top-2). Covers winner + runner-up + 3rd place — captures competitive-race context (JD(S) in Karnataka, AAP in Punjab/Delhi, BSP in UP) at ~30% of the "all candidates" cost.
- **Unified pipeline error log** at `data/eci/errors/pipeline_errors.jsonl`. Every stage writes to it via `scripts/pipeline_errors.py`. Query with `python scripts/pipeline_errors.py [--stage --state --year --details]`.
- **Match confidence persisted.** `election_appearances` now has `match_score` (0-100) + `match_method` (`exact`|`fuzzy`|`no_match`). `load_eci_results.py` auto-adds the columns on first run and prints an exact/strong/uncertain breakdown at the end.
- **NOTA is skipped explicitly** by `load_eci_results.py` — no more noise in the unmatched list.
- **Duplicate-match bug fixed.** Candidates are now processed in rank order (winners first) with a `used_aids` set, so a runner-up can't overwrite a winner's `won=True` if both fuzzy-match to the same DB politician.
- **Cloud Vision preprocess uses `--pdf-allowlist`** (name differs from the local EasyOCR script's `--allowlist`). Existing 232 Rajasthan / 456 total preprocessed files came from this path; use it not the local script.
- **`--cdp` requires a port number.** Correct: `--cdp 9222` (previously docs said `--cdp` alone).

## What changed in v4

- **ECI official results replace Wikipedia scraping.** New scripts `fetch_eci_results.py` + `load_eci_results.py` pull vote counts for every candidate per constituency (not just top 2), directly from `results.eci.gov.in`. The per-state Wikipedia loaders (`load_<state>_election_results.py`) are no longer used for winner marking on new states — they're kept only to generate the `--dry-run` diagnostic JSON if you need it as a sanity check.
- **DB renamed** from `politrack.db` → `lokvani.db`. Every script now defaults to the new name.
- **Local dev locked to SQLite.** `app/database.py` ignores any stray `DATABASE_URL` in your shell unless `USE_NEON=1`. Prod continues using Neon via Render's `RENDER=true` env var.
- **Multi-word state names now work everywhere.** `resolve_state` in `app/main.py` was rewritten to match against `ALL_STATES` config, and `load_eci_to_db.py` + `llm_extract_via_gemini.py` normalize `--state` to TitleCase internally.
- **Chrome CDP launcher.** For any script that needs to bypass Akamai (fetch_eci_affidavits, fetch_eci_results), use the dedicated launcher `python scripts/phase1_test_cdp_attach.py --launch` — this opens a fresh Chrome with a separate profile that Playwright can attach to.
- **`build_top_n_allowlist.py` accepts both schemas.** Wikipedia-style flat list AND ECI-results-style dict. Auto-detects.

---

## Canonical pipeline (14 steps)

```
1. ECI results  →  PDF / Excel / results.eci.gov.in
                    │
                    ▼
2. Parse         →  data/eci/results/{state}_{year}_eci_results.json   (all candidates, gender/age/party/votes)
                    │
                    ▼
3. Affidavit fetch →  data/eci/raw_pdfs/{state}-{year}/*.pdf + manifest.jsonl
                    │
                    ▼
4. Top-N allowlist →  data/allowlists/{state}_{year}_top3.txt   (default: top-3)
                    │
                    ▼
5. Cloud Vision OCR (allowlisted only)  →  data/eci/for_ai/preprocessed_{state}_{year}/
                    │
                    ▼
6. Regex extract → CSV                  →  data/eci/for_ai/extracted/{state}_{year}_structured.csv
                    │
                    ▼
7. Load to provisional (staging)        →  eci_candidates_provisional table
                    │
                    ▼
8. Migrate → canonical                  →  states/politicians/constituencies/election_appearances
                    │
                    ▼
9. Load winners (marks won=1 + votes)   →  election_appearances.won / votes_received / match_score
                    │
                    ▼
10. Gemini extract (top-N only)         →  data/eci/for_ai/llm_extracted/{state}_{year}/
                    │
                    ▼
11. Apply LLM extraction                →  election_appearances.total_assets_inr etc.
                    │
                    ▼
12. Register state in app/states.py + postmigrate.sh
                    │
                    ▼
13. Verify locally (winners + wealth non-zero + confidence breakdown)
                    │
                    ▼
14. Sync to Neon + git push
```

**Every stage writes failures to `data/eci/errors/pipeline_errors.jsonl`.** Check it whenever a step reports fewer records than expected:

```bash
python scripts/pipeline_errors.py --state Rajasthan --year 2023            # counts
python scripts/pipeline_errors.py --state Rajasthan --year 2023 --details  # each entry
```

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

## Step 1 — Get structured election results

Pick the source in this order:

**A. ECI Statistical Report PDF** (preferred — most reliable, ~5-10 sec parse). Download `10-Detailed-Results.pdf` from `https://www.eci.gov.in/statistical-report/ae/<year>/<num>` and drop it in `data/Results/`. Then:

```bash
mkdir -p data/eci/results

python scripts/parse_eci_statistical_report_pdf.py \
  --pdf "data/Results/${STATE}_${YEAR}.pdf" \
  --state "$STATE" --year $YEAR --state-code $STATE_CODE \
  --out data/eci/results/${STATE_LC}_${YEAR}_eci_results.json
```

**B. ECI Statistical Report Excel** (`10-Detailed-Results.xlsx`) — use if the PDF isn't available AND the Excel actually contains data. Some ECI Excels are empty (Gujarat, HP, Karnataka, Meghalaya, Tripura) or mislabeled (Rajasthan 2023 Excel contained MP data). Always sanity-check with `python scripts/audit_statistical_reports.py` first.

```bash
python scripts/parse_eci_statistical_report.py \
  --excel "data/Results/10-Detailed-Results_${STATE}_${YEAR}.xlsx" \
  --state "$STATE" --year $YEAR --state-code $STATE_CODE \
  --out data/eci/results/${STATE_LC}_${YEAR}_eci_results.json
```

**C. `fetch_eci_results.py` from `results.eci.gov.in`** — only for very recent cycles where the ECI results portal is still live. Needs CDP-attached Chrome.

```bash
python scripts/fetch_eci_results.py \
  --state "$STATE" --year $YEAR --state-code $STATE_CODE \
  --results-base $RESULTS_BASE \
  --out data/eci/results/${STATE_LC}_${YEAR}_eci_results.json
```

**Verify** (regardless of source):
```bash
python3 -c "
import json
d = json.load(open('data/eci/results/${STATE_LC}_${YEAR}_eci_results.json'))
c = sum(len(x['candidates']) for x in d['constituencies'])
w = [w for x in d['constituencies'] for w in x['candidates'] if w['won']]
have_g = sum(1 for x in w if x.get('gender'))
print(f'Constituencies: {len(d[\"constituencies\"])} · Candidates: {c}')
print(f'Winners: {len(w)} · with gender/age: {have_g}/{len(w)}')
"
```

Expected: constituency count = assembly size (postponed elections may drop 1-3 seats — Karanpur was missing from Rajasthan 2023 for this reason). Every winner should have gender and age populated.

**Common PDF-parse issues (all fixed in current parser, listed for awareness):**
- Multi-line names with rank prefix (`1 Kuldeep / Singh MALE... / Pathania`) — captured correctly
- Names wrapping AFTER data line (`Pathania` on the next row) — captured via post-name slot
- 2-word symbols that leak into name (AITC "Flowers and grass" in Meghalaya) — heuristic-stripped
- 1-word party+symbol like `NPEP Book` → party="NPEP" — fixed (was giving party="")
- Symbol tokens starting with `-` like `Auto-` → skipped as name continuation

---

## Step 2 — Fetch all candidate affidavits from ECI (30-60 min, up to 2000-cap)

The affidavit fetcher needs a CDP-attached Chrome (via `--cdp 9222` — port number required).

```bash
mkdir -p data/eci/raw_pdfs/${STATE_LC}-${YEAR}

python scripts/fetch_eci_affidavits.py \
  --cdp 9222 \
  --listing-url "$AFFIDAVIT_URL" \
  --output data/eci/raw_pdfs/${STATE_LC}-${YEAR} \
  --concurrent-tabs 3 --dedupe-listing
```

**Known behaviour:** ECI paginates at 20 pages × 100 per page = 2000-cap regardless of the state's actual candidate count. Large states (Karnataka 2500+, Bihar 2400+, UP 4400+, MH 4100+) will not fetch the full list from a single URL. That's OK — top-3 will still be captured because those are always in the first 1500 by name-alphabetical listing order. For 100% coverage, iterate the URL per-district (rare need).

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

## Step 4 — Build top-3 allowlist (NEW default)

The builder auto-detects the ECI-results JSON schema and skips NOTA entries. **Top-3 is the new standard** — covers winner + runner-up + notable third-place challenger. Top-2 misses competitive-race context (JD(S), AAP, BSP swings) at only marginal cost savings.

```bash
mkdir -p data/allowlists

python scripts/build_top_n_allowlist.py \
  --results data/eci/results/${STATE_LC}_${YEAR}_eci_results.json \
  --manifest data/eci/raw_pdfs/${STATE_LC}-${YEAR}/manifest.jsonl \
  --output data/allowlists/${STATE_LC}_${YEAR}_top3.txt \
  --top-n 3
```

Expected size: ~3 × assembly_size, minus fuzzy-match misses against manifest (usually 5-10% miss rate).

Expected log line: `(detected ECI-results schema, converted N constituencies)`.

---

## Step 5 — Cloud Vision OCR only the allowlist (~15-25 min, ~$18 for top-3 of 200 seats)

```bash
python scripts/cloud_vision_preprocess.py \
  --pdf-dir data/eci/raw_pdfs/${STATE_LC}-${YEAR}/raw_pdfs \
  --out-dir data/eci/for_ai/preprocessed_${STATE_LC}_${YEAR} \
  --pdf-allowlist data/allowlists/${STATE_LC}_${YEAR}_top3.txt \
  --workers 4
```

**Note:** flag is `--pdf-allowlist` (not `--allowlist`, which is the local EasyOCR script's variant). Do NOT use `preprocess_eci_pdfs.py` — it runs OCR locally (slow, hot laptop, no cost tracking); we use Cloud Vision.

Requires `pip install google-cloud-vision pdf2image pdfplumber` + `brew install poppler`. Auth is via `GOOGLE_APPLICATION_CREDENTIALS` (already in `secrets/.env` — `source secrets/.env` once per shell).

Idempotent — skips PDFs already preprocessed. `cost_estimate_usd` printed per PDF; total at end.

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

Fuzzy-matches candidate names in the DB to the ECI results and sets `won`, `votes_received`, `vote_share_pct`, plus the new `match_score` (0-100) and `match_method` (`exact`|`fuzzy`) columns.

**Expected output:**
```
Updated appearances: <top-N × contested-seats>
Winners flagged:     <contested-seats>
NOTA skipped:        <one per constituency>
Unmatched candidates: <rest of ECI list, mostly non-top-N>
Match confidence — exact:187  strong(≥85):11  uncertain(75-84):1
```

The loader:
- **Skips NOTA** explicitly (ballot option, no politician row exists)
- **Processes candidates in rank order** (winners first) with a `used_aids` set — so a runner-up can't accidentally overwrite a winner's `won=True` if both fuzzy-match to the same DB politician
- **Auto-adds `match_score` + `match_method` columns** on first run (idempotent)
- **Writes every unmatched candidate to the pipeline error log** with `error_type: fuzzy_no_match`

Check the log for spot cleanup:
```bash
python scripts/pipeline_errors.py --stage match_results --state "$STATE" --year $YEAR --details
```

Uncertain matches (score < 85) worth manual review:
```bash
python3 -c "
import sqlite3
c = sqlite3.connect('lokvani.db').cursor()
for r in c.execute('''
    SELECT c.name, p.name, ea.match_score, ea.match_method
    FROM election_appearances ea
    JOIN elections e ON ea.election_id=e.id
    JOIN states s ON e.state_id=s.id
    JOIN constituencies c ON ea.constituency_id=c.id
    JOIN politicians p ON ea.politician_id=p.id
    WHERE s.name=? AND e.year=? AND ea.match_score < 85
    ORDER BY ea.match_score ASC LIMIT 20''', ('$STATE', $YEAR)):
    print(r)
"
```

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
| `fetch_eci_affidavits.py: error: argument --cdp: expected one argument` | Missing port | `--cdp 9222` — port number required |
| `Could not attach to Chrome on port 9222` | Chrome not launched with debug port | Kill Chrome, run `phase1_test_cdp_attach.py --launch` |
| Fetcher: "Frame has been detached" | Chrome instability | Re-run — auto-retries |
| Fetcher: 5-15% corrupt PDFs | `--concurrent-tabs` too high | Step 3 with `--concurrent-tabs 1` |
| Fetcher stops at ~2000 rows | ECI server-side pagination cap | Expected. Top-3 winners still captured. For 100% run per-district iterations. |
| Allowlist: `'str' object has no attribute 'get'` | Old build_top_n_allowlist.py without schema detection | Update to latest — check line 121 has `# Detect schema` |
| OCR: laptop very hot, no cost readout | Running `preprocess_eci_pdfs.py` (local EasyOCR) not `cloud_vision_preprocess.py` | Ctrl+C, switch to Cloud Vision |
| OCR: `ModuleNotFoundError: google.cloud` | Vision SDK not installed | `pip install google-cloud-vision` |
| OCR: `Unable to get page count. Is poppler installed?` | Missing system dep | `brew install poppler` |
| OCR: `No PDFs in <dir>` | Fetcher put files in subdir | Point `--pdf-dir` at `raw_pdfs/{state}-{year}/raw_pdfs` (nested) OR flatten with Step 2 command |
| Gemini: 403 | Wrong GCP creds | `source secrets/.env` — sets `GOOGLE_APPLICATION_CREDENTIALS` |
| Gemini: `ImportError: from google import genai` | New SDK not installed | `pip install google-genai` |
| `load_eci_results.py`: `No appearances in DB` | Haven't run Steps 7-8 yet | Complete affidavit pipeline first |
| `load_eci_results.py`: `disk I/O error` on ALTER TABLE | Read-only filesystem or SQLite journal issue | Free space / restart terminal / check DB permissions |
| Loader flags 197 winners but audit sees only 161 | Duplicate fuzzy-match: runner-up overwrote winner | Fixed in v5 — `used_aids` set + rank-order processing |
| NOTA appearing in unmatched list | Old loader didn't skip | Fixed in v5 — `nota_skipped` counter |
| Loader/results: 0 matches | State-name case mismatch | Verify DB with query; update `states.name` if needed |
| Excel parses to 0 constituencies | PDF-to-Excel conversion produced empty file | Use `parse_eci_statistical_report_pdf.py` on the original PDF instead |
| Excel STATE column doesn't match filename | Mislabeled file (e.g. Rajasthan Excel had MP data) | Run `python scripts/audit_statistical_reports.py`; re-download from ECI |
| Rajasthan / other state missing 1 constituency | Election postponed to a separate bulletin (Karanpur AC-3 → Jan 2024 by-poll) | Manually add the by-poll winner or leave as known-missing |
| Parsed winner has surname missing (Mukul Sangma → DR. MUKUL) | AITC "Flowers and grass" symbol column overlapped name column in PDF layout | Known limitation for AITC in Meghalaya; cosmetic only |
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
10. **`top-N` in the allowlist** controls OCR + Gemini cost. **Top-3 is the standard.** Top-2 misses competitive-race context (JD(S), AAP, BSP swings). Top-4 costs ~30% more without meaningful editorial gain. You can re-run Step 4-11 later with a higher N — everything is idempotent.
11. **Pipeline error log is the source of truth for failures.** Every stage writes to `data/eci/errors/pipeline_errors.jsonl`. Whenever a count seems off (fewer winners than expected, wealth data sparser than expected), start with:
    ```bash
    python scripts/pipeline_errors.py --state "$STATE" --year $YEAR
    ```
12. **Match confidence < 85 is worth reviewing.** After Step 9, spot-check any `fuzzy` matches below 85. Common cause: DB politician name from a different election cycle got fuzzy-matched loosely.
13. **`load_eci_results.py` skips NOTA and dedupes matches.** Do not re-add those rows manually — they're intentionally omitted.
14. **CDP flag needs a port.** `--cdp 9222`, not bare `--cdp`.

---

## Cost cheat sheet (per state, top-3 default)

| Assembly size | Cloud Vision | Gemini | Total | Wall time |
|---|---|---|---|---|
| 40 (Goa)          | ~$4  | ~$2  | ~$6   | ~30 min |
| 60 (Tripura)      | ~$5  | ~$3  | ~$8   | ~40 min |
| 70 (Delhi)        | ~$6  | ~$3  | ~$9   | ~45 min |
| 90 (Chhattisgarh) | ~$8  | ~$4  | ~$12  | ~1 hr |
| 117 (Punjab)      | ~$10 | ~$5  | ~$15  | ~1.25 hr |
| 200 (Rajasthan)   | ~$18 | ~$7  | ~$25  | ~2 hr |
| 230 (MP)          | ~$20 | ~$8  | ~$28  | ~2.5 hr |
| 288 (Maharashtra) | ~$26 | ~$10 | ~$36  | ~3 hr |
| 403 (UP)          | ~$36 | ~$15 | ~$51  | ~4.5 hr |

Add ~$0 for the ECI Statistical Report parse (local, free) and ~5-10 min per state for the DB load + migrate steps.

---

# Post-Ingestion Sanity Checks

After committing + Render deploy completes:

1. Open `https://lokvani.example.com/state/${STATE_LC}` — should render with correct constituency count.
2. Click 3 random constituencies — winner names should match ECI results portal.
3. Check the India map — state should be shaded appropriately (green/tan) with correct MLA count.
4. Hover the state on the map — tooltip should show correct MLA count + election year.
5. Check the coverage snapshot — party-wise coverage table should populate.

If any check fails, fix locally, re-run the affected step, re-sync, re-push.
