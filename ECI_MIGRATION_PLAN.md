# ECI Migration Plan

Pivot the data source from **myneta.info** (ADR's curated dataset) to the
**Election Commission of India** affidavit corpus. ADR has formally declined
bulk data access and notified us not to continue scraping their site. ECI's
candidate affidavits are public records published under election law and
are not covered by that notice.

This document is the spec. Code lives in branches/PRs derived from these
phases. **Read this before starting any phase.**

---

## Approach (the part we've already decided)

1. **Start state:** Delhi 2025 — small (70 seats), recent, definitely on suvidha.eci.gov.in.
2. **Transition strategy:** parallel tables. Build ECI pipeline into `eci_*`-prefixed tables alongside existing myneta-sourced ones. Migrate by promotion (rename), not by mutation. Site keeps working throughout.
3. **Pre-2014 historical data:** defer the question. For now, the migration goal is "match what we have from myneta from ~2014 onward." Older cycles will be addressed after the recent ones are clean.

---

## Phases

### Phase 0 — Reconnaissance (1 week, no production code)

**Goal:** answer "is this actually feasible?" before committing to engineering.

Deliverables:
- 20-30 Delhi 2025 affidavits downloaded manually from suvidha
- A 2-page report: PDF template structure, URL patterns, OCR-needed-or-not, sample of what fields we can reliably extract
- A small Python notebook that successfully extracts {name, party, total_assets, criminal_cases_count, education} from a sample of 5 PDFs
- A go/no-go decision: parser quality good enough? scaling feasible?

Output location:
- `docs/eci_reconnaissance.md` (the report)
- `notebooks/eci_phase0.ipynb` (proof of parsing)
- `data/eci/raw_pdfs/delhi2025_sample/` (the downloaded PDFs)

### Phase 1 — Delhi 2025 end-to-end (2 weeks)

**Goal:** a complete Delhi 2025 dataset in `eci_*` tables, sourced entirely from ECI.

New modules (none of this touches existing scraper):

```
app/sources/eci/
  __init__.py
  affidavit_fetcher.py      # Download affidavit PDFs from suvidha. Cache to disk.
  affidavit_parser.py       # PDF → structured fields. The hard part.
  results_fetcher.py        # Per-constituency winners from results.eci.gov.in
  constituency_map.py       # ECI codes ↔ our DB names. Per-state mapping table.
  ingest_eci.py             # Writes parsed records into eci_election_appearances etc.
```

New DB tables (parallel to existing):

```sql
eci_politicians           (mirrors politicians schema + source_url + extraction_confidence)
eci_election_appearances  (mirrors election_appearances)
eci_assets                (mirrors assets)
eci_criminal_cases        (mirrors criminal_cases)
```

CLI target:

```bash
python -m app.sources.eci.ingest_eci delhi 2025
```

Acceptance criteria:
- All 70 Delhi 2025 winners present in `eci_election_appearances`
- ≥ 95% of winners have a parsed `total_assets_inr`
- ≥ 90% of winners have a parsed `criminal_cases_count`
- Every record has its `source_url` field set to the actual ECI affidavit URL
- An `extraction_confidence` field flags low-quality parses for manual review

### Phase 2 — Reconciliation (1-2 weeks)

**Goal:** confirm ECI parse matches reality before we promote anything.

Script: `scripts/reconcile_eci_vs_myneta.py`

For each Delhi 2025 winner, joins our myneta record with the corresponding ECI record. Outputs:

- `reconciliation/delhi_2025_summary.json` — per-field match rates
- `reconciliation/delhi_2025_discrepancies.csv` — every row where ECI ≠ myneta, sorted by severity

Categorizes discrepancies as:
- **Identical** — green, ignore
- **Cosmetic** — formatting only (₹1,57,00,000 vs ₹15,700,000)
- **Quantitative** — actual numeric differences. Pick winner per field per case.
- **Categorical** — different party/name/constituency. Manual review.

Field-by-field confidence assessment after this phase:

| Field | Expected match rate | Action if mismatched |
|---|---|---|
| `name` | ≥ 99% | ECI authoritative (raw from candidate's own filing) |
| `party` | ≥ 99% | ECI authoritative |
| `constituency` | 100% (we use ECI's mapping) | ECI by definition |
| `total_assets_inr` | ≥ 60% identical, ≥ 95% within 1% | ECI raw value preferred; flag outliers |
| `criminal_cases_count` | ≥ 85% | ECI authoritative |
| `education` | ≥ 80% | Either source acceptable |
| `profession` | ≥ 70% | Either source acceptable |

If we don't hit these thresholds, the migration pauses while we improve the parser or accept the trade-off.

### Phase 3 — Scale + migrate (2-4 weeks)

**Goal:** every state we have myneta data for is also in `eci_*` tables, validated, and the site reads from ECI by default.

Per-state checklist (repeated for each state in this order: Delhi → Punjab → Goa → Sikkim → Bihar → Northeast tier → others):

1. Build/adapt `affidavit_parser.py` for state-specific template quirks
2. Run `ingest_eci.py <state> <cycle>` for all available cycles
3. Run reconciliation; manually review CSV
4. Sign off if metrics pass

Then **one migration commit**:

```sql
-- Promote ECI tables to primary
ALTER TABLE politicians           RENAME TO legacy_myneta_politicians;
ALTER TABLE election_appearances  RENAME TO legacy_myneta_election_appearances;
ALTER TABLE assets                RENAME TO legacy_myneta_assets;
ALTER TABLE criminal_cases        RENAME TO legacy_myneta_criminal_cases;

ALTER TABLE eci_politicians           RENAME TO politicians;
ALTER TABLE eci_election_appearances  RENAME TO election_appearances;
ALTER TABLE eci_assets                RENAME TO assets;
ALTER TABLE eci_criminal_cases        RENAME TO criminal_cases;
```

After migration:
- `services.py`, `main.py`, all templates work unchanged (data shape is identical)
- Attribution on every page updates: "Source: Election Commission of India" with link to original ECI URL
- Footer methodology page updated
- README updated
- Email ADR a brief notice that the project no longer reads from myneta

The `legacy_myneta_*` tables are kept around as a safety net (so any data quality regression can be diagnosed against the original source) until we're confident in the new pipeline — probably 1-2 months.

### Phase 4 — Backfill historical data (deferred)

We agreed to defer this. When we come back to it:

- ECI digital affidavits become unreliable pre-2014
- Options to revisit then: state CEO site OCR, alternative archives, accept gap

For now, the data we already scraped from myneta for those older cycles stays in `legacy_myneta_*` tables. Whether to surface them on the live site is a separate decision we'll make after Phase 3 lands.

---

## Risks and how we'll catch them early

| Risk | Detection point | Mitigation |
|---|---|---|
| Affidavit PDFs are scanned images (need OCR) | Phase 0 | OCR + Tesseract fallback in parser; quality bar drops |
| ECI publishes affidavits inconsistently per state | Phase 0 sample, Phase 3 per-state | Per-state parser variants; reconciliation catches it before sign-off |
| Asset values parse wrong due to Indian number formatting | Phase 2 reconciliation | Field-level confidence tracking; flag mismatches > 1% for manual review |
| ECI portal changes structure or URLs | Anytime | Cache every PDF to disk on first fetch; fetcher resilient to URL drift |
| ECI also issues a takedown | Anytime | Lower probability (affidavits are public record by election law) but plan to engage politely if so |
| Time investment turns out larger than 5-9 weeks | Phase 0 / Phase 1 | Each phase has a "no-go" exit criterion; we don't have to finish if the work compounds badly |

---

## Things this plan does NOT do

To prevent scope creep:

- **No new states.** We migrate exactly the 17 we have. New state additions wait until the pipeline is stable.
- **No data improvements beyond what ECI provides.** We don't backfill missing fields from other sources during the migration.
- **No UI changes.** The site looks identical post-migration. Only attribution text + footer methodology updates.
- **No parser perfectionism.** If a field is reliably parsed for 95% of candidates, we accept the 5% gap and flag it rather than rebuilding the extractor.

---

## What's next (the very first concrete action)

Phase 0 starts with **a manual deep-dive into Delhi 2025 on suvidha.eci.gov.in**:

1. Find ~30 candidate affidavit URLs by browsing the suvidha portal
2. Download them, save to `data/eci/raw_pdfs/delhi2025_sample/`
3. Open 5 of them, look at the template, decide: text-extractable or scanned?
4. Write a 2-page report on what we found

I'll do this exploration when you tell me to go. Nothing else changes until that report exists and we agree the project is feasible.

---

## How we'll know we're done

The migration is complete when:

- [ ] Every Delhi 2025 winner page on the site reads its data from `eci_*` tables
- [ ] Footer attribution says "Election Commission of India" with a working link
- [ ] `services.py` has no references to `myneta.info` in the data path
- [ ] The disabled scraper guard in `myneta_client.py` stays in place
- [ ] README documents the new pipeline
- [ ] One follow-up email sent to ADR notifying them of the cutover

Then we expand to other states.
