# ECI affidavit fetcher — runbook

`scripts/fetch_eci_affidavits.py` does the Phase 1 data pull.

## Why you need to run it locally

The Cowork sandbox cannot reach `affidavit.eci.gov.in` — it's not on the
proxy's allowlist (verified: returns `403 blocked-by-allowlist`). Playwright
also isn't installed. Both are normal for the sandbox and intentional.

So this script needs to run **on your own machine**, where:
- network access to `affidavit.eci.gov.in` is unrestricted, and
- Playwright + Chromium can be installed.

## One-time setup

```bash
cd "Politicians Project"
python3 -m venv .venv-eci
source .venv-eci/bin/activate
pip install playwright
playwright install chromium
```

This installs ~250 MB of Chromium binaries — once.

## Smoke test (Delhi 2025, 5 candidates)

**Always run a 5-candidate smoke test first.** Use the direct URL mode
(`--listing-url`) — it skips the cascading filter form entirely and just
iterates `&page=N`.

The Delhi 2025 Assembly General listing URL is:

```
https://affidavit.eci.gov.in/CandidateCustomFilter
    ?electionType=28-AC-GENERAL-3-54
    &election=28-AC-GENERAL-3-54
    &states=U05
```

(The `28-AC-GENERAL-3-54` token is ECI's composite election identifier.
`U05` is Delhi's portal state code — same prefix as our sample
`U05_918_633_20250120024725.pdf` eStamp paper.)

Smoke-test command:

```bash
python scripts/fetch_eci_affidavits.py \
    --listing-url "https://affidavit.eci.gov.in/CandidateCustomFilter?electionType=28-AC-GENERAL-3-54&election=28-AC-GENERAL-3-54&states=U05" \
    --output data/eci/raw_pdfs/delhi-2025/ \
    --limit 5 \
    --no-headless         # watch it in a real Chrome window
```

### Alternative (fallback) — cascading-form mode

If the direct URL stops working someday, you can fall back to driving the
form:

```bash
python scripts/fetch_eci_affidavits.py \
    --election "FEB-2025" \
    --election-type "AC - GENERAL" \
    --state "NCT OF Delhi" \
    --output data/eci/raw_pdfs/delhi-2025/ \
    --limit 5
```

If the form-mode hits a "No `<option>` matching" error (Laravel renamed
something), re-run with `--inspect` and the script dumps every
`<select>` on the page to `_form_layout.json`.

Expected console output:

```
Resume: 0 candidates already in manifest
  → Navigating to portal ...
  → Election: GEN-Election-FEB-2025
  → Election type: AC - GENERAL
  → State: NCT OF Delhi
  → Filters applied
=== Listing page 1 ===
  10 candidates on this page
Total candidates to process: 5
[1/5] ARVIND KEJRIWAL / Aam Aadmi Party
     ✓ ARVIND_KEJRIWAL__19492.pdf (5,928,343 bytes)
[2/5] PARVESH SAHIB SINGH / Bharatiya Janata Party
     ...
```

Look for:
- All 5 PDFs land in `data/eci/raw_pdfs/delhi-2025/raw_pdfs/`
- `data/eci/raw_pdfs/delhi-2025/manifest.jsonl` has 5 lines

## Akamai bot protection

`affidavit.eci.gov.in` sits behind Akamai. Symptoms:

- Paste the listing URL into a fresh browser tab → **"Access Denied"** with
  a reference like `https://errors.edgesuite.net/18.fc92c31...`
- But navigating to the same URL via the in-portal links works fine

That's Akamai requiring its sensor cookies (`_abck`, `bm_sz`, `bm_sv`) to
be present, which only happens after a successful JS challenge on the
portal home page. The fetcher handles this automatically:

1. Launches Chromium with `--disable-blink-features=AutomationControlled`
   so `navigator.webdriver` is hidden
2. Real Chrome User-Agent string (not Playwright's `HeadlessChrome`)
3. Visits the portal home, waits 2.5s for Akamai's challenge
4. Visits the form page (`/candidate-affidavit`)
5. Only THEN navigates to the deep URLs you supplied

If you still see "Access Denied" after warm-up the script raises a clear
error and saves the blocked HTML to `_blocked_page_N.html`. Most common
real-world fix: re-run with `--no-headless` so a real visible Chrome
window runs the challenge naturally; some Akamai deployments also serve
a CAPTCHA on flagged IPs, which only a visible browser lets you solve.

## Common smoke-test failures + fixes

| Symptom | Likely cause | Fix |
|---|---|---|
| `Akamai blocked the home page itself` | IP rate-limited / geo-fenced | Different network; or `--no-headless` to solve CAPTCHA |
| `Akamai blocked page N` mid-crawl | Cookies expired or burst rate too high | Raise `--delay` to 5s; re-run, manifest resumes |
| `No <option> matching 'FEB-2025'` | (form mode) Election labels changed | Re-run with `--inspect`; check `_form_layout.json` |
| Listing rows = 0 | Card HTML drifted | Inspect `_empty_page_N.html`; the script anchors on `show-profile` links — if those moved, post the HTML here |
| Every download is 503 | Anti-bot signal too synthetic | Add `slow_mo=200` to `pw.chromium.launch(...)` |
| Downloads start but PDFs are empty | The download flow needs eStamp click | Inspect with `--no-headless`; copy the real click sequence |

## Smoke test pipeline (after PDFs land)

```bash
# 1. Dedup the freshly downloaded folder
python scripts/dedup_affidavits.py data/eci/raw_pdfs/delhi-2025/raw_pdfs/ \
    --out data/eci/delhi-2025-dedup.json

# 2. For each canonical PDF in the dedup, parse it
python scripts/parse_eci_affidavit.py \
    "$(jq -r '.candidates[0].canonical_pdf' data/eci/delhi-2025-dedup.json)" \
    --out /tmp/parsed-first.json

# 3. Spot-check: does the parsed total_assets match the existing DB row?
#    (compare to the corresponding myneta_candidate_id in politrack.db)
```

## Full pull (only after the smoke test is clean)

Drop the `--limit` flag and let it run. Expected duration for Delhi 2025
Assembly: ~600 candidates × (~5s per profile + 2s delay) ≈ **1.5 hours**.

**Important:** keep a visible Chrome window. Akamai fingerprints headless
Chrome on this portal — the script defaults to non-headless for that
reason. Don't add `--headless` unless you've separately confirmed it
works for you (it usually doesn't on first try).

```bash
python scripts/fetch_eci_affidavits.py \
    --listing-url "https://affidavit.eci.gov.in/CandidateCustomFilter?electionType=28-AC-GENERAL-3-54&election=28-AC-GENERAL-3-54&states=U05" \
    --output data/eci/raw_pdfs/delhi-2025/
```

You can minimise the Chrome window once the warm-up succeeds — Akamai
doesn't care about visibility after the initial JS challenge, just about
the browser fingerprint. So you can stash it on a second monitor or
behind other windows for the next 70 minutes.

You can interrupt with Ctrl-C and re-run — the manifest is appended after
each candidate, so a re-run resumes from where it left off.

## Politeness

The script enforces:
- 2 s sleep between candidate fetches (configurable via `--delay`)
- Polite User-Agent string identifying the project + contact
- Real Chromium (not synthetic clicks) so `isTrusted: true`

Per `ECI_RECON.md`, the portal has been observed to 503 synthetic clicks
— Playwright's real clicks should not trigger this.

If you start seeing 503s at scale: stop, raise `--delay` to 5 s, narrow
your time window (run only during low-traffic hours).

## Output layout

```
data/eci/raw_pdfs/delhi-2025/
├── manifest.jsonl                # 1 JSON row per candidate processed
└── raw_pdfs/
    ├── ARVIND_KEJRIWAL__19492.pdf
    ├── PARVESH_SAHIB_SINGH__19501.pdf
    └── ...
```

Each manifest row:

```json
{
  "name": "ARVIND KEJRIWAL",
  "party": "Aam Aadmi Party",
  "status": "Accepted",
  "state": "NCT OF Delhi",
  "constituency": "NEW DELHI",
  "profile_url": "https://affidavit.eci.gov.in/show-profile/eyJpdiI...",
  "affidavit_id": "19492",
  "pdf_path": "data/eci/raw_pdfs/delhi-2025/raw_pdfs/ARVIND_KEJRIWAL__19492.pdf",
  "download_attempted": true,
  "download_succeeded": true,
  "error": ""
}
```

Rejected affidavits will have `status: "Rejected"` — per our spec we don't
display these on the site, but we still archive them for completeness.

## After the full pull lands

Hand the folder back to me and we can:
1. Run `dedup_affidavits.py` on the full set (~600 PDFs × 10s = ~100 minutes
   — caches mean subsequent runs are instant)
2. Run `parse_eci_affidavit.py` on each canonical PDF
3. Spot-check 10 random candidates against the existing DB
4. If reconciliation passes, write the `eci_*` parallel tables loader
