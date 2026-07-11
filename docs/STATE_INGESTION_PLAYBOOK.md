# State Ingestion Playbook (v3)

Definitive step-by-step guide for adding a new Indian state's assembly election data to Lokvani. Reflects all the case-handling and template-leak bugs we've hit and fixed.

**Read this once. Then follow it top-to-bottom for every state.**

---

## Preflight (once per session)

```bash
cd "/Users/gurneetbedi/Desktop/Claude/Project 1/Politicians Project"
source .venv-eci/bin/activate
export GOOGLE_APPLICATION_CREDENTIALS=secrets/lokvani-501706-a3b68700fa4a.json
/Applications/Google\ Chrome.app/Contents/MacOS/Google\ Chrome --remote-debugging-port=9222 &
```

Set these per-state variables (used throughout):

```bash
STATE=Gujarat                        # TitleCase — passed to --state flags
STATE_LC=gujarat                     # lowercase — file paths and directory names
YEAR=2022
ECI_URL="https://affidavit.eci.gov.in/CandidateCustomFilter?...SXX..."
```

**Multi-word states** (Uttar Pradesh, Jammu and Kashmir, etc.): use full TitleCase for `STATE`, lowercase-with-underscores for `STATE_LC`.

---

## Step 1 — Create the loader script

```bash
cp scripts/load_chhattisgarh_election_results.py \
   scripts/load_${STATE_LC}_election_results.py

# Three-case sed (uppercase, TitleCase, lowercase)
sed -i '' "s/CHHATTISGARH/$(echo $STATE_LC | tr a-z A-Z)/g; \
           s/Chhattisgarh/$STATE/g; \
           s/chhattisgarh/$STATE_LC/g" \
  scripts/load_${STATE_LC}_election_results.py
```

**Then manually edit the file:**
- Update `WIKI_URLS` dict with the correct Wikipedia URL (verify by opening in browser).
- Reset `<STATE>_<YEAR>_COLS` to:
  ```python
  { "table_index": None, "header_rows": 2, "cols": {} }
  ```

**Verify template didn't leak:**
```bash
grep -i chhattisgarh scripts/load_${STATE_LC}_election_results.py   # should return nothing
grep STATE_NAME scripts/load_${STATE_LC}_election_results.py        # should show TitleCase state name
grep -A2 "if not expected" scripts/load_${STATE_LC}_election_results.py  # must have the bypass clause
```

The bypass clause must look like:
```python
if not expected and not (args.dump_tables or args.dry_run):
    sys.exit(...)
```

If missing, apply this fix:
```bash
python3 -c "
import re
p = 'scripts/load_${STATE_LC}_election_results.py'
s = open(p).read()
s = re.sub(r'if not expected:\s*\n(\s+)sys\.exit',
           r'if not expected and not (args.dump_tables or args.dry_run):\n\1sys.exit', s)
open(p, 'w').write(s)
"
```

Also verify the row filter is not overly strict:
```bash
grep "not in expected" scripts/load_${STATE_LC}_election_results.py
```
Must be `if expected and const_norm not in expected:` — NOT `if not const_norm or const_norm not in expected:`.

---

## Step 2 — Fill the column map

**Auto-fill (fast path):**
```bash
python scripts/auto_fill_column_map.py --state $STATE_LC --year $YEAR
```

Look for `Parsed N ✓`. If it says "Rescued" or a low count, verify manually.

**Manual fallback (3 min):**

1. Dump all tables and find the one matching your assembly size:
   ```bash
   python scripts/load_${STATE_LC}_election_results.py --year $YEAR \
     --dump-tables --refetch 2>&1 | grep -E "^  Table [0-9]+:"
   ```

2. Pick the table whose row count matches assembly size (±2) AND whose header 0 is like `['District', 'Constituency', 'Winner', 'Runner up', 'Margin']`.

3. Look at sample rows via:
   ```bash
   python scripts/load_${STATE_LC}_election_results.py --year $YEAR \
     --dump-tables --refetch 2>&1 | grep -A5 "^  Table <N>:"
   ```

4. For the standard **NE-13-cell layout** (90% of states — subsequent rows have 13 cells), use:
   ```python
   <STATE>_<YEAR>_COLS = {
       "table_index": <N>,
       "header_rows": 2,
       "cols": {
           "constituency": -12,
           "winner_name":  -11,
           "winner_party":  -9,
           "winner_votes":  -8,
           "winner_pct":    -7,
           "runner_name":   -6,
           "runner_party":  -4,
           "runner_votes":  -3,
           "runner_pct":    -2,
       },
   }
   ```

5. **Variants** (check header row 1):
   - **Turnout column** (Haryana, Sikkim 2019): shift `constituency` to `-13`; other offsets unchanged.
   - **Two Votes/% pairs at end** (Goa 2022, Jharkhand 2024): shift `constituency` to `-13`; other offsets unchanged.

**Verify:**
```bash
python scripts/load_${STATE_LC}_election_results.py --year $YEAR --dry-run --refetch
```
Expect `Parsed <assembly-size> constituencies (0 skipped)`. If not, offsets are wrong — recount from the right on a 13-cell sample row.

---

## Step 3 — Fetch all PDFs (30-60 min)

```bash
mkdir -p data/eci/raw_pdfs/${STATE_LC}-${YEAR}

python scripts/fetch_eci_affidavits.py \
  --listing-url "$ECI_URL" \
  --output data/eci/raw_pdfs/${STATE_LC}-${YEAR} \
  --concurrent-tabs 3 --dedupe-listing
```

If Chrome dies mid-run, re-run same command (resume-safe via manifest.jsonl).

**Flatten if the fetcher put PDFs in a subdirectory:**
```bash
[ -d data/eci/raw_pdfs/${STATE_LC}-${YEAR}/raw_pdfs ] && \
  mv data/eci/raw_pdfs/${STATE_LC}-${YEAR}/raw_pdfs/*.pdf \
     data/eci/raw_pdfs/${STATE_LC}-${YEAR}/
```

---

## Step 4 — Corrupt PDF sweep (only if > 2% corruption)

```bash
find data/eci/raw_pdfs/${STATE_LC}-${YEAR} -name "*_corrupt.txt" | wc -l
```

If under 2% of total, skip. Otherwise:
```bash
find data/eci/raw_pdfs/${STATE_LC}-${YEAR} -name "*_corrupt.txt" -delete
python scripts/refetch_corrupt_pdfs.py --state ${STATE_LC}-${YEAR}
python scripts/fetch_eci_affidavits.py --listing-url "$ECI_URL" \
  --output data/eci/raw_pdfs/${STATE_LC}-${YEAR} \
  --concurrent-tabs 1 --dedupe-listing
```

---

## Step 5 — Build top-4 allowlist

```bash
mkdir -p data/allowlists

python scripts/build_top_n_allowlist.py \
  --results data/eci/results/${STATE_LC}_${YEAR}_results.json \
  --manifest data/eci/raw_pdfs/${STATE_LC}-${YEAR}/manifest.jsonl \
  --output data/allowlists/${STATE_LC}_${YEAR}_top4.txt \
  --top-n 4
```

---

## Step 6 — OCR only the allowlist (~10-15 min)

```bash
python scripts/cloud_vision_preprocess.py \
  --pdf-dir data/eci/raw_pdfs/${STATE_LC}-${YEAR} \
  --out-dir data/eci/for_ai/preprocessed_${STATE_LC}_${YEAR} \
  --pdf-allowlist data/allowlists/${STATE_LC}_${YEAR}_top4.txt \
  --workers 4
```

---

## Step 7 — Structured extract → CSV

```bash
python scripts/extract_structured.py \
  --in-dir data/eci/for_ai/preprocessed_${STATE_LC}_${YEAR} \
  --out-dir data/eci/for_ai/extracted \
  --prefix ${STATE_LC}_${YEAR}
```

---

## Step 8 — Load to provisional table

```bash
python scripts/load_eci_to_db.py \
  --csv data/eci/for_ai/extracted/${STATE_LC}_${YEAR}_structured.csv \
  --state "$STATE" \
  --election-year $YEAR \
  --election-type Assembly \
  --manifest data/eci/raw_pdfs/${STATE_LC}-${YEAR}/manifest.jsonl
```

The script now normalizes `--state` to TitleCase internally (multi-word states included), so any case works.

---

## Step 9 — Migrate provisional → canonical

```bash
env -u DATABASE_URL python scripts/migrate_to_eci_only.py
```

This creates the `constituencies` and initial `election_appearances` rows for your state.

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

Idempotent — re-run to pick up any PDFs that failed. Pass `--refresh` to force re-extraction.

---

## Step 11 — Apply LLM extraction to canonical DB

```bash
python scripts/apply_llm_extraction.py --cycles ${STATE_LC}_${YEAR}
```

---

## Step 12 — Run the loader (marks winners + vote counts)

```bash
python scripts/load_${STATE_LC}_election_results.py --year $YEAR
```

Expect `Winners set: ~<assembly-size>, Runners-up: ~<assembly-size>, Unmatched: <few>`.

---

## Step 13 — Register state in the app

**Edit `app/states.py`:**
- Add your state entry to `ALL_STATES` (the visible dict), NOT `_ALL_STATES_HISTORICAL` (internal-only).
- Remove from `_HIDDEN_STATES` if present.

**Edit `scripts/postmigrate.sh`** — append before the verification block:
```bash
echo ""
echo "  $STATE ($YEAR)"
python scripts/load_${STATE_LC}_election_results.py --year $YEAR || true
```

---

## Step 14 — Re-run postmigrate (verifies everything is wired)

```bash
bash scripts/postmigrate.sh
```

Bottom shows coverage table — confirm your state has non-zero `winners` and `verified_LLM`.

---

## Step 15 — Verify locally

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

All three should be non-zero. If any is zero, DO NOT proceed to sync.

**Then restart the server (uses local SQLite):**
```bash
pkill -9 -f uvicorn
uvicorn app.main:app --reload
```

Hard-refresh the browser (Cmd+Shift+R), hover the state on the map — should show MLA count and financial data.

---

## Step 16 — Sync to Neon + deploy

```bash
# Neon connection needed for sync only
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
| Goa | 40 | Small (done) |
| Mizoram | 40 | Small (done) |
| Sikkim | 32 | Small (done) |
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
| **Assam** | **126** | Mid (done) |
| **Kerala** | **140** | Mid |
| **Odisha** | **147** | Mid |
| **Andhra Pradesh** | **175** | Mid |
| **Gujarat** | **182** | Mid |
| **Rajasthan** | **200** | Mid |
| **Karnataka** | **224** | Mid |
| **Madhya Pradesh** | **230** | Mid |
| **Tamil Nadu** | **234** | Large |
| **Bihar** | **243** | Large |
| **Maharashtra** | **288** | Large |
| **West Bengal** | **294** | Large |
| **Uttar Pradesh** | **403** | Large |

Do small first, UP last.

## Priority order for remaining states

Kerala → Odisha → Andhra Pradesh → Gujarat → Rajasthan → Karnataka → Madhya Pradesh → Tamil Nadu → Bihar → Maharashtra → West Bengal → Uttar Pradesh

## Script flag cheatsheet

| Script | Required flags |
|---|---|
| `auto_fill_column_map.py` | `--state --year` |
| `load_<state>_election_results.py` | `--year` (add `--dry-run --dump-tables --refetch` for testing) |
| `fetch_eci_affidavits.py` | `--listing-url --output` |
| `refetch_corrupt_pdfs.py` | `--state` (hyphenated form: `gujarat-2022`) |
| `build_top_n_allowlist.py` | `--results --manifest --output --top-n` |
| `cloud_vision_preprocess.py` | `--pdf-dir --out-dir` (opt: `--pdf-allowlist --workers --limit`) |
| `extract_structured.py` | `--in-dir --out-dir --prefix` |
| `load_eci_to_db.py` | `--csv --state --election-year --election-type --manifest` |
| `migrate_to_eci_only.py` | none (prepend `env -u DATABASE_URL`) |
| `llm_extract_via_gemini.py` | `--in-dir --out-dir --state --year --workers` |
| `apply_llm_extraction.py` | `--cycles` (underscored form: `gujarat_2022`) |
| `sqlite_to_postgres.py` | `--reset` (requires `DATABASE_URL` set inline) |

## Naming conventions

Four name forms coexist:

| Form | Where | Example |
|---|---|---|
| **TitleCase** | `--state` flag values, `states.name` in DB, LLM JSON's state field, loader's `STATE_NAME` | `Gujarat`, `Uttar Pradesh` |
| **lowercase** | Directory suffixes for OCR/allowlist, filenames, ALL_STATES key | `gujarat`, `uttar_pradesh` |
| **hyphenated** | Directory suffix under `raw_pdfs/`, `--state` for `refetch_corrupt_pdfs.py` | `gujarat-2022` |
| **underscored** | Directory suffix under `preprocessed_/llm_extracted/`, `--cycles` for `apply_llm_extraction.py` | `gujarat_2022` |

## ECI state codes

For the URL's `states=SXX` parameter:

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
| Delhi | U05 | Telangana | S29 |
| Puducherry | U06 | | |

---

# Common failures & fixes

| Symptom | Cause | Fix |
|---|---|---|
| Loader: `Parsed 0 (N skipped)` | Column map wrong | Recount from right in Step 2 |
| Loader: `Parsed 0 (N unrecognized)` | Filter rejects when DB empty | Fix line: `if expected and const_norm not in expected:` |
| Loader: `<state> constituencies in DB: 0` (with --dump-tables) | Missing bypass clause | Fix: `if not expected and not (args.dump_tables or args.dry_run):` |
| Loader: `Saved 0 to chhattisgarh_...json` | sed missed lowercase state | Three-case sed with `s/chhattisgarh/$STATE_LC/g` |
| Fetcher: "Frame has been detached" | Chrome instability | Re-run — auto-retries |
| Fetcher: 5-15% corrupt PDFs | `--concurrent-tabs` too high | Step 4 with `--concurrent-tabs 1` |
| OCR: `No PDFs in <dir>` | Fetcher put files in subdir | `mv <dir>/raw_pdfs/*.pdf <dir>/` |
| Gemini: 403 | Wrong GCP creds | Re-export `GOOGLE_APPLICATION_CREDENTIALS` |
| Migrate: `YOUR_USER` errors | `.env` has placeholders | Fix `secrets/.env` with real Neon URL |
| Postmigrate coverage shows 0 winners | Loader missing from postmigrate.sh | Add loader line (Step 13) |
| Map shows "Data Not Available" locally | State not in `ALL_STATES` or server not restarted | Verify Step 13 + `pkill -9 -f uvicorn` |
| Map shows "0 MLAs" locally | `DATABASE_URL` in shell → server hits Neon | `unset DATABASE_URL` before uvicorn |
| Map shows 0 MLAs on prod | Neon not synced | Step 16 |
| `Gujarat Pradesh` in URLs/vars | Template leak from *Pradesh loader | `sed -i '' 's/Gujarat Pradesh/Gujarat/g' <file>` |
| `WIKI_URLS = {}` empty | Manual edit skipped | Add `2022: "https://en.wikipedia.org/wiki/2022_Gujarat_Legislative_Assembly_election"` |
| `STATE_NAME = "gujarat"` lowercase | sed only did lowercase | `sed -i '' 's/STATE_NAME   = "gujarat"/STATE_NAME   = "Gujarat"/' <file>` |
| Loader: cache path has old state name | Missed lowercase replacement | `sed -i '' 's/<old>/<new>/g' <file>` — verify with `grep -i <old>` |

---

# Key rules

1. **`--state` value = TitleCase** everywhere: `Gujarat`, `Uttar Pradesh`, `Jammu and Kashmir`. The scripts normalize internally, but stay consistent for readability.
2. **File paths + directory suffixes = lowercase**: `gujarat-2022`, `gujarat_2022`.
3. **Add to `ALL_STATES`, not `_ALL_STATES_HISTORICAL`** in `app/states.py`.
4. **postmigrate.sh must contain your state's loader line**, otherwise migrate wipes winners on next run.
5. **After DB changes**: hard-kill uvicorn (`pkill -9 -f uvicorn`) then restart. `--reload` isn't reliable for DB changes.
6. **`DATABASE_URL` is now shielded from local dev** by `app/database.py`. Local always uses SQLite. Escape hatch: `USE_NEON=1 uvicorn app.main:app`.
7. **Verify locally before syncing**: winners > 0 AND with_assets > 0 in SQLite. If not, don't push.
8. **Sync command needs DATABASE_URL inline**:
   ```bash
   (set -a; source secrets/.env; set +a; python scripts/sqlite_to_postgres.py --reset)
   ```

---

# Post-Ingestion Sanity Checks

After committing + Render deploy completes:

1. Open `https://lokvani.example.com/state/${STATE_LC}` — should render with correct constituency count.
2. Click 3 random constituencies — winner names should match Wikipedia.
3. Check the India map — state should be shaded appropriately (green/tan) with correct MLA count.
4. Check hero-strip stats page — MLA count, avg wealth, avg cases should all be non-zero.

If any check fails, roll back the sync:
```bash
(set -a; source secrets/.env; set +a; python scripts/sqlite_to_postgres.py --reset)
# followed by another sync once fixed locally
```
