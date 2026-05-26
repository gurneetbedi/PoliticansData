# PoliTrack.in — Project Plan

An open source transparency platform showing details of Indian MPs and MLAs: their declared net worth, criminal cases, education, attendance, and parliamentary performance.

## 1. Vision

A single, searchable, citable place where any Indian citizen can look up their elected representative and see — with sources — what they have declared in their election affidavits and how they have performed in office. The data already exists, but it is scattered across Election Commission PDFs, ADR reports, and Parliament websites. PoliTrack consolidates it into one queryable interface.

## 2. Tech Stack

Since you have Python and database experience, here is the recommended stack:

**Backend:** Python with FastAPI (modern, async, auto-generates API docs). Django is the alternative if you want a built-in admin panel for editing data manually.

**Database:** PostgreSQL. It handles the relational data well (politicians, parties, constituencies, cases, assets all link together) and has strong full-text search for the search/filter feature. SQLite is fine for early local development.

**ORM:** SQLAlchemy (with FastAPI) or Django ORM.

**Data scraping:** Python with `requests`, `BeautifulSoup`, and `pdfplumber` for extracting data from Election Commission affidavits and the ADR/MyNeta.info site.

**Frontend:** Start with server-rendered Jinja2 templates (simple, fast, SEO-friendly). Upgrade to React/Next.js later if you need richer interactivity. The mockup HTML you have already shows the target design.

**Hosting:** Railway, Render, or Fly.io for the API + Postgres. Cloudflare in front for caching. All have generous free tiers.

**Search:** Postgres full-text search is enough at the start. Move to Meilisearch or Typesense if it gets slow.

## 3. Database Schema (Core Tables)

```
politicians
  id, name, slug, photo_url, dob, gender, education_level,
  profession, current_party_id, house (LS/RS/MLA), constituency_id,
  state_id, term_start, term_end, attendance_pct, debates_count,
  questions_count, created_at, updated_at

parties
  id, name, short_name, symbol_url, founded_year, ideology

constituencies
  id, name, state_id, type (LS/Assembly), reserved_for (SC/ST/None)

states
  id, name, code, region

assets
  id, politician_id, election_year, asset_type (immovable/movable/vehicle/jewelry/investment),
  description, declared_value_inr, source_document_url

liabilities
  id, politician_id, election_year, creditor, amount_inr, source_document_url

criminal_cases
  id, politician_id, case_number, ipc_sections, fir_date, court,
  charge_description, status (pending/acquitted/convicted/dismissed),
  is_serious (bool), source_document_url

election_history
  id, politician_id, year, constituency_id, party_id, votes_received,
  vote_share_pct, won (bool), opponent_id

income_sources
  id, politician_id, year, source_type, annual_amount_inr

data_sources
  id, source_name, source_url, last_fetched_at, fetch_status
```

## 4. Data Sources

The project depends on public data. The realistic pipeline:

**Primary source — myneta.info (ADR).** This is the most valuable source and should be the backbone of the data pipeline. The Association for Democratic Reforms (ADR) parses every candidate's Form 26 affidavit from the Election Commission and publishes it as structured HTML at https://myneta.info/. Every politician has a profile page with declared assets (movable + immovable broken down by category), liabilities, criminal cases (with IPC sections and case status), education, profession, and PAN-declared income. The URL pattern is predictable (e.g., `myneta.info/LokSabha2024/candidate.php?candidate_id=...`), which makes systematic scraping straightforward. This single source covers Lok Sabha, Rajya Sabha, and all state assemblies across multiple election cycles, so it can populate roughly 80% of the database on its own.

**Secondary sources.** The Election Commission of India (eci.gov.in) publishes raw affidavit PDFs — useful as a verification source when myneta data looks off. Parliament of India (sansad.in) publishes attendance, debates, and questions data not available on myneta. Wikidata has structured info (date of birth, photos, Wikipedia links) accessible via SPARQL and is licensed openly. State legislative assembly portals are inconsistent but worth checking for MLA-specific data.

**Scraping etiquette.** myneta is run by a non-profit and the data is public, but be a good citizen: rate limit to 1 request per 2 seconds, cache raw HTML locally so reruns don't re-hit their servers, set a clear User-Agent identifying the project and a contact email, respect robots.txt, and consider reaching out to ADR — they may be open to a direct data dump for an open source project rather than scraping. Always store the source URL alongside every fact — that is what makes the project trustworthy.

**Legality.** This data is public record and showing it on an aggregated site is exactly what ADR itself does. The project is legally on solid ground as long as it scrapes respectfully and attributes sources.

## 5. API Endpoints (FastAPI)

```
GET  /api/politicians                 list with filters (state, party, house, cases, networth)
GET  /api/politicians/{slug}          full profile
GET  /api/politicians/search?q=...    full-text search
GET  /api/compare?ids=1,2,3           comparison data
GET  /api/states                      list of states
GET  /api/parties                     list of parties
GET  /api/stats                       homepage aggregate stats
```

## 6. Implementation Roadmap

**Phase 1 — Foundation (week 1-2):** Set up the FastAPI project, Postgres database, and create the schema with Alembic migrations. Seed it with 20 hand-picked politicians so the rest of the work has real data to render.

**Phase 2 — Frontend (week 3-4):** Port the mockup HTML into Jinja2 templates connected to the API. Get the four pages (home, browse, detail, compare) working end-to-end with the seed data.

**Phase 3 — Data pipeline (week 5-7):** Write the scraper for myneta.info. Run it for one state first (something manageable like Goa or Punjab) to validate the pipeline. Add data validation and de-duplication. Then expand to all states.

**Phase 4 — Search and polish (week 8):** Add Postgres full-text search, filter combinations, sorting. Add the Chart.js visualizations from the mockup. Mobile responsive testing.

**Phase 5 — Open source release:** Publish to GitHub with a clear README, a CONTRIBUTING guide, a Code of Conduct, and good first issues. Set up GitHub Actions for tests. Add a public API with rate limiting so others can build on the data.

## 7. Project Structure

```
politrack/
  backend/
    app/
      api/              FastAPI routes
      models/           SQLAlchemy models
      schemas/          Pydantic request/response schemas
      services/         Business logic
      scrapers/         Data ingestion (myneta, parliament)
      templates/        Jinja2 HTML templates
      static/           CSS, JS, images
    alembic/            DB migrations
    tests/
    main.py
    requirements.txt
  data/
    seeds/              JSON seed files for initial data
    raw/                Scraped raw HTML/PDF (gitignored)
  docs/
    schema.md
    api.md
    contributing.md
  docker-compose.yml    Postgres + app for local dev
  README.md
  LICENSE               (recommend MIT or AGPL)
```

## 8. Things to Decide Early

A few choices will shape the project. Whether to include both Lok Sabha + Rajya Sabha + all 28 states' MLAs from day one, or start with just Lok Sabha (543 records, manageable). Whether to allow community contributions through pull requests for data corrections, or keep data ingestion fully automated from official sources. Whether to display photos (need a consistent rights-cleared source — Wikimedia Commons is the cleanest option). And the licence — MIT is friendlier for adoption, AGPL ensures derivatives stay open.

## 9. Open Source Considerations

To make it genuinely useful as an open source project: ship the data as a downloadable CSV/JSON dump alongside the live site, document the schema clearly, keep the scraping code well-commented so others can extend it for new sources, and version both code and data so researchers can cite a specific snapshot.

## 10. Next Steps

The mockup (`mockup.html` in this folder) shows the target UI. The next concrete step is to set up the FastAPI skeleton and the Postgres schema, then seed it with 5-10 real politicians' data manually so the templates have something to render. From there the scraper is the biggest engineering chunk, and the pages are mostly already designed.
