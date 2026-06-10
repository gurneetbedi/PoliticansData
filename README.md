# PolitiTrack India

An open-source transparency platform that surfaces declared net worth, criminal cases, education, and term-over-term history for Indian elected representatives — MLAs, Lok Sabha MPs, and Rajya Sabha members. All data is sourced from [myneta.info](https://myneta.info/) (ADR), which structures Election Commission of India affidavits.

**Current coverage (assembly):**

- **Punjab** — 2007, 2012, 2017, 2022 (117 seats each cycle)
- **Bihar** — 2005, 2010, 2015, 2020, 2025 (243 seats each cycle)
- **Goa** — 2007, 2012, 2017, 2022 (40 seats each cycle)

Lok Sabha and Rajya Sabha coverage for Punjab is also included. The architecture is state-agnostic — adding a new state means writing a small scraper module and adding an entry to `app/states.py`.

> **Status:** research-preview. Things will move. Issues and PRs welcome.

## Tech Stack

- Backend: Python 3.10+ with FastAPI
- Database: SQLite for dev, PostgreSQL for production
- ORM: SQLAlchemy 2.0
- Templates: Jinja2 (server-rendered HTML)
- Scraping: requests + BeautifulSoup, with on-disk response caching
- Frontend charts: Chart.js (loaded via CDN)

## Project Layout

```
Politicians Project/
  app/
    main.py              FastAPI application + routes
    models.py            SQLAlchemy models (Politician, Election, Asset, etc.)
    database.py          DB session setup
    ingest.py            CLI: scrape + load data into the DB
    scrapers/
      myneta_client.py   Polite HTTP client with caching + rate limiting
      generic_state.py   Shared list/detail parsers
      punjab.py          Punjab winners + LS + runner-ups
      bihar.py           Bihar winners + detail enrichment
      goa.py             Goa winners + detail enrichment
    states.py            Registry of supported states + cycles
    services.py          State-scoped DB queries powering every page
    templates/           Jinja2 HTML (home, browse, detail, heatmap, anomalies, funding)
    static/              CSS + GeoJSON + constituency coords
  data/
    cache/myneta/        Cached HTML responses (gitignored)
  scripts/               One-off geocoding / GeoJSON download helpers
  mockup.html            Standalone design mockup
  PROJECT_PLAN.md        Full architecture and roadmap
  LEGAL.md               Notes on data provenance and disclaimers
  requirements.txt
  README.md
```

## Local Setup

```bash
# Clone and enter the project
cd "Politicians Project"

# Create a virtualenv and install dependencies
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

# Scrape myneta and populate the DB. Each state takes ~10 min for winners-only,
# longer with the detail enricher. Targets are state-scoped:
python -m app.ingest punjab          # Punjab MLAs (winners across all cycles)
python -m app.ingest bihar           # Bihar MLAs
python -m app.ingest goa             # Goa MLAs
# Append `_all` to also pull runner-ups, or `_detail` for per-affidavit enrichment.
# See `python -m app.ingest --help` for the full list of targets.

# Download Punjab constituency boundaries for the interactive map
python scripts/download_geojson.py

# Run the server
uvicorn app.main:app --reload
# Then open http://localhost:8000
```

The first scraper run hits myneta.info live and caches each response under `data/cache/myneta/`. Subsequent runs are instant because they read from the cache. To force a refresh, delete the cache directory.

## Interactive map

The homepage features a Leaflet map of Punjab showing all 23 districts colored by Punjab's three traditional sub-regions (Majha, Doaba, Malwa). The GeoJSON file is downloaded by `scripts/download_geojson.py` from the [datta07/INDIAN-SHAPEFILES](https://github.com/datta07/INDIAN-SHAPEFILES) project on GitHub. It is saved to `app/static/punjab_districts.geojson`.

The map gives geographic context. Drill-down to individual MLAs happens through the search bar (with autocomplete), the leaderboards, and the constituency-level pages — all of which are constituency-level and rich.

If `punjab_districts.geojson` is missing, the homepage shows a friendly fallback explaining how to set it up — the rest of the page still works.

**Future upgrade:** when a 117-feature Punjab assembly-constituency GeoJSON becomes available, drop it at `app/static/punjab_ac.geojson` and we will extend the map to drill into AC-level data.

### Constituency dots overlay

To overlay clickable colored dots for every constituency (one per latest-cycle MLA), run:

```
python scripts/geocode_constituencies.py
```

This geocodes each constituency via OpenStreetMap Nominatim (1 request/second; the whole run takes a couple of minutes for ~117 constituencies) and saves the result to `app/static/constituency_coords.json`. The homepage map automatically picks it up on the next load. Dots are colored by party and sized by relative wealth; click any dot to open that MLA's profile.

If this file is missing, the map still renders fine — you just don't get the dot overlay.

## Scraper Behavior

The scraper is built to be a good citizen:

- **Rate limited**: 2 seconds between requests by default (override via `MYNETA_RATE_LIMIT`)
- **Cached**: every response is written to disk and re-used on subsequent runs
- **Identified**: sends a clear `User-Agent` identifying the project (edit `MYNETA_USER_AGENT` in `app/scrapers/myneta_client.py`)
- **Retried**: transient failures retry with exponential backoff
- **Idempotent**: ingestion can be re-run safely; existing records are updated rather than duplicated

Before scraping at scale, **edit `MYNETA_USER_AGENT`** in `app/scrapers/myneta_client.py` to include your real contact email. ADR runs myneta.info as a non-profit and they appreciate being able to reach you if anything goes wrong.

> **Note (2026):** ADR sent a formal cease-and-desist asking us to stop
> scraping myneta.info. The myneta scraper is now **disabled by default**
> (`ScrapeDisabledError` guard in `app/scrapers/myneta_client.py`). The
> existing data in the DB remains the production source while we migrate
> to the ECI affidavit pipeline described below. See
> [`docs/ECI_MIGRATION_PLAN.md`](docs/ECI_MIGRATION_PLAN.md) for the full
> roadmap.

## ECI Affidavit Pipeline (next-generation data source)

Form 26 affidavits are filed directly with the Election Commission of
India and made public at <https://affidavit.eci.gov.in>. They contain
*more* than what ADR/myneta surfaces — per-bank-account balances, per-
fund mutual-fund holdings, jewellery weights, vehicle registration
numbers, full 5-year income tax history, and detailed criminal case
records *including convictions* (which myneta sometimes misses).

The pipeline is four scripts, each with its own runbook in `docs/`:

```
PDFs on ECI portal
       │
       ▼
┌──────────────────────────────────┐
│ scripts/fetch_eci_affidavits.py  │  Playwright crawler, Akamai-aware
│                                    │  (warm-up + UA mask + non-headless),
│                                    │  status-filtered (--status Accepted)
└──────────────────────────────────┘
       │  data/eci/raw_pdfs/<state-year>/raw_pdfs/<NAME>__<id>.pdf
       ▼
┌──────────────────────────────────┐
│ scripts/build_ai_extraction_     │  Dedup multi-affidavit candidates,
│           package.py              │  pick canonical (largest) per
│                                    │  candidate, copy to organized folder
└──────────────────────────────────┘
       │  data/eci/for_ai/pdfs/<seq>_<NAME>__<id>.pdf  (one per candidate)
       ▼
┌──────────────────────────────────┐
│ scripts/preprocess_eci_pdfs.py   │  HSV stamp removal → 300-DPI render →
│                                    │  EasyOCR with bbox spatial sorting →
│                                    │  column-delimited cleaned text JSON.
│                                    │  pdfplumber fast-path on text-native
│                                    │  PDFs.
└──────────────────────────────────┘
       │  data/eci/for_ai/preprocessed/<seq>_<NAME>.json
       ▼
┌──────────────────────────────────┐
│ scripts/qc_preprocessed.py       │  Tags each candidate CLEAN / LIGHT /
│                                    │  FLAG / EMPTY based on key markers
│                                    │  (name, PAN, party, currency, Part B)
└──────────────────────────────────┘
       │  data/eci/for_ai/preprocessed/_qc_report.csv
       ▼
       │  (manual or LLM-assisted structured extraction step)
       │
       │  Hand the cleaned text + extraction_prompt.md to any LLM
       │  (Claude, ChatGPT, Gemini). Get JSON matching
       │  data/eci/for_ai/extraction_schema.json. See README in
       │  data/eci/for_ai/ for AI-agnostic usage notes.
       ▼
   data/eci/for_ai/output/<seq>_<NAME>.json     ← structured records
       │
       ▼
   (TODO) Loader → eci_* parallel tables in politrack.db
```

### Quick start

One-time deps (system + Python):

```bash
brew install poppler tesseract        # required by pdftoppm/easyocr
python3 -m venv .venv-eci
source .venv-eci/bin/activate
pip install playwright easyocr opencv-python pdf2image pdfplumber pillow
playwright install chromium
```

Then for any state/year:

```bash
# 1. Fetch (visible browser; Akamai blocks headless)
python scripts/fetch_eci_affidavits.py \
    --listing-url "https://affidavit.eci.gov.in/CandidateCustomFilter?electionType=28-AC-GENERAL-3-54&election=28-AC-GENERAL-3-54&states=U05" \
    --output data/eci/raw_pdfs/delhi-2025/

# 2. Organize into one-canonical-PDF-per-candidate
python scripts/build_ai_extraction_package.py

# 3. Preprocess with EasyOCR + CV
python scripts/preprocess_eci_pdfs.py

# 4. QC the output
python scripts/qc_preprocessed.py
```

### Why this architecture

- **ECI is the authoritative source.** myneta is a curated derivative.
  Going to ECI directly removes a middleman and avoids the politics of
  scraping a non-profit.
- **AI-agnostic structured extraction.** The `data/eci/for_ai/` folder is
  the bridge — you can hand its prompt + schema + example to Claude,
  ChatGPT, Gemini, or any model with PDF support. No vendor lock-in.
- **Hybrid OCR is cheap and fast.** Image sanitation (HSV stamp removal)
  plus EasyOCR's bounding-box-aware spatial sorting handles ~90% of
  affidavits without an LLM. Only the noisy 10% need to escalate to a
  vision LLM as a top-up. Total cost for all-of-Delhi-2025 is ~$5 in API
  spend, vs ~$30 for pure vision-LLM.
- **Resumable everywhere.** Fetcher manifests, preprocessing checkpoint
  per PDF, dedup cache invalidates poisoned entries automatically.

### Detailed runbooks

- [`docs/eci_fetcher_runbook.md`](docs/eci_fetcher_runbook.md) — fetcher details, Akamai workaround, CAPTCHA fallback
- [`docs/eci_preprocessing_runbook.md`](docs/eci_preprocessing_runbook.md) — preprocessing pipeline, QC tags, when to escalate to LLM
- [`docs/eci_dedup_method.md`](docs/eci_dedup_method.md) — multi-affidavit candidate dedup logic
- [`docs/ECI_MIGRATION_PLAN.md`](docs/ECI_MIGRATION_PLAN.md) — overall roadmap from myneta → ECI
- [`data/eci/for_ai/README.md`](data/eci/for_ai/README.md) — how to hand PDFs to any AI for structured extraction

## What Gets Scraped

For each Punjab assembly cycle (2007, 2012, 2017, 2022) the winners list provides per MLA:

- Name and stable myneta candidate ID
- Constituency
- Party
- Criminal case count
- Education
- Total declared assets (in INR)
- Total declared liabilities (in INR)

This populates ~117 winners per cycle × 4 cycles = ~468 election appearances. Many politicians appear in multiple cycles (re-contesters), so the unique politician count is lower — they're deduplicated using `myneta_candidate_id`.

A second-pass enricher (`scrape_candidate_detail` in `app/scrapers/punjab.py`) can pull richer per-affidavit data: serious cases count, asset breakdown by category, individual case details. This is partially implemented and is the natural next step.

## Database Schema

The core relational design:

- `politicians` — one row per unique person (keyed by myneta candidate ID, persists across cycles)
- `elections` — one row per election cycle (year + house + state)
- `election_appearances` — many-to-many between politicians and elections, holds the affidavit snapshot for that filing
- `assets`, `liabilities`, `criminal_cases` — child rows attached to an appearance (since declarations change each election)
- `parties`, `constituencies`, `states` — lookup tables

See `app/models.py` for the full schema.

## API Endpoints

Server-rendered HTML:

- `GET /` — homepage with featured politicians and stats
- `GET /browse?q=...&party=...&year=...` — filterable list
- `GET /politician/{slug}` — detailed profile with wealth-growth chart and election history

JSON:

- `GET /api/politicians` — list with `limit`, `offset`, `q` query params
- `GET /api/politicians/{slug}` — single profile with all appearances
- `GET /api/stats` — aggregate counts and available elections

## Deploying to Tier 1 (Free)

1. **Repository**: push to GitHub
2. **Database**: create a free Postgres at [neon.tech](https://neon.tech) or [supabase.com](https://supabase.com); copy the connection string
3. **Backend**: create a free service at [railway.app](https://railway.app) or [render.com](https://render.com); connect the repo; set `DATABASE_URL` to the Postgres URL from step 2
4. **Domain**: buy a `.in` or `.org` domain (~Rs 800/year); point it at the hosting service; Cloudflare in front for free DDoS protection and CDN
5. **Scraper**: run the scraper from your laptop (or a free GitHub Actions cron) and the data lands in Postgres

Total recurring cost: just the domain. Everything else stays free until you outgrow the free tiers.

## Scaling Up

When you're ready to expand:

1. **More states**: add a new scraper module under `app/scrapers/<state>.py` mirroring `punjab.py`. The DB schema already supports all states.
2. **Lok Sabha / Rajya Sabha**: same pattern — myneta has `LokSabha2024`, `LokSabha2019`, `ls2014`, `ls2009`, `loksabha2004` slugs ready to scrape.
3. **Search**: when Postgres LIKE queries start lagging, add a Meilisearch or Typesense instance for full-text search.
4. **Comparison page**: the mockup includes a comparison view; the API already returns the data — just needs the template.

## Contributing

Open source from day one. Areas that need help:

- Detail-page enrichment (parsing the full asset breakdown and individual cases from myneta candidate pages)
- Adding new states (the scraper pattern is repeatable)
- Comparison page template
- Parliament attendance / questions data integration (sansad.in)
- Wikidata enrichment for politician photos and biographical data
- Tests (none yet — `pytest` welcome)

## Disclaimer

All data comes from public Election Commission of India affidavits via ADR. Figures are self-declared by candidates. PoliTrack adds nothing — it only restructures and visualises what is already publicly disclosed.

## License

[MIT](./LICENSE). The license covers the code in this repository. The underlying affidavit data is collected and structured by [ADR](https://adrindia.org/) and published on myneta.info — please review their terms before redistributing the data independently of this codebase.
