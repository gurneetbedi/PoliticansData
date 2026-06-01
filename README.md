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
