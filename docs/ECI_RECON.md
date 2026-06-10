# ECI Phase 0 Reconnaissance Report

**Date:** Phase 0 of `ECI_MIGRATION_PLAN.md`
**Portal:** https://affidavit.eci.gov.in/
**Goal:** Determine feasibility of replacing myneta.info as the data source.

**Verdict:** Feasible, but Phase 1 architecture needs to be Playwright-based
(not simple HTTP). The portal has light bot protection on the PDF download
endpoint; metadata scraping (listing + profile pages) works fine.

---

## What the portal looks like

The site is a **Laravel-based** application with cascading form filters and
encrypted ID payloads in URLs. Coverage runs from **May 2019 to present**
(May 2026 by-elections were active when we explored).

### Cascading filter system

The candidate-affidavit page (`/candidate-affidavit`) has five dropdowns
that filter dependently:

1. **Election** (e.g. "GEN-BYE-Election-JAN-FEB-2025") — value format: `<row_id>-<chamber>-<phase>-<seq>-<int>`
2. **Election Type** ("AC - GENERAL" / "AC - BYE" / "PC - GENERAL") — populated after election selected
3. **State** — populated after election type selected (XHR-driven)
4. **Phase** — populated after state selected
5. **Constituency** — populated after phase selected

Each dropdown change fires an XHR to load the next level's options.
No URL-based deep linking — selections live only in form state.

### Endpoints

| URL | Method | Purpose | Response |
|---|---|---|---|
| `/candidate-affidavit` | GET | List all candidates (defaults to latest election) | HTML with paginated candidate cards |
| `/candidate-affidavit?...` | GET | Filtered listing after form submit | HTML, same shape |
| `/show-profile/<eyJpdiI...>` | GET | Single candidate profile | HTML with metadata |
| `/increaseDownloadCount` | POST | Bumps the public download counter for an affidavit | 200 JSON |
| `/affidavit-pdf-download/<eyJpdiI...>` | GET | Actual PDF download | 503 on programmatic clicks |

### Encrypted URL payloads

Profile URLs and the PDF download URL both use Laravel-encrypted payloads:

```
eyJpdiI6ImFlU2I1T29rQ2dMRFlSVGxNeGdDMkE9PSIs
InZhbHVlIjoicEdubnFSNjcvZHBaUEQwNDc0T0NIdz09Iiw
ibWFjIjoiZDhhYmVmY2Q2ZTNkYmEyYjdiZGM0ZTQ1ZmJk
OTE5N2Y2ZTMzZWZmMDAxMzE0MTBhMzk2NzMxYTRmMjY3NWU3YiIs
InRhZyI6IiJ9
```

That's base64 of `{"iv":"<base64-iv>", "value":"<base64-aes-encrypted>", "mac":"<hex>", "tag":""}`.
**We cannot enumerate these without server-side key material.** They have to
be discovered by crawling listing pages.

The good news: the PDF download URL is generated **freshly per click** on
the profile page — meaning the server is willing to mint new tokens. We
don't need to cache them long-term; we mint, immediately fetch, and discard.

---

## What data lives where

This is the single most important Phase 0 finding for migration planning.

### Listing page (per candidate, ~10 candidates per page)

```
Name:        SEKH SAJID
Party:       Rashtriya Secular Majlis Party
Status:      Accepted | Rejected | Withdrawn | Contesting
State:       West Bengal
Constituency: DOMJUR
[View more →]
```

Easy to scrape. Useful for building the "all candidates in election X" list.

### Profile page (`/show-profile/<encrypted>`)

```
Party Name:                Rashtriya Secular Majlis Party (English + Hindi)
Name:                      SEKH SAJID (English + Hindi)
Assembly constituency:     DOMJUR
State:                     West Bengal
Application Uploaded:      10th April, 2026
Current status:            Accepted
Affidavit Uploaded On:     10th April, 2026 13:46:14
Download Count:            1628
Father's / Husband's Name: SK SELI UDDIN
Address:                   N0011 BENUBAN, BANKRA, DOMJUR DISTRICT - HOWRAH, PIN 711403
Gender:                    male
Age:                       40
```

Decent metadata but **MISSING the fields we care most about**:
- ❌ Total declared assets
- ❌ Total declared liabilities
- ❌ Criminal cases count + details (IPC sections, court, status)
- ❌ Education
- ❌ Profession
- ❌ Per-asset breakdown (movable/immovable)

### Affidavit PDF (the only place where the rich data lives)

Each candidate has one downloadable PDF affidavit. **This is the only
source for assets/cases/education/profession**. There is no JSON or
structured-data alternative on the portal.

The PDF download button exposes a clean **integer affidavit ID**:

```html
<button class="download-btn" data-affidavit-id="19492">Download</button>
```

But hitting the download endpoint programmatically returns 503 — see next
section.

---

## The 503 anti-bot behavior

When `.click()` is dispatched programmatically on the Download button:

```
POST /increaseDownloadCount        → 200 OK  ✓ (counter incremented)
GET  /affidavit-pdf-download/<enc> → 503 Service Unavailable  ✗
```

Two attempts, same result. The encrypted URL is fresh each time, so the
server is willing to mint it; it's the actual PDF endpoint that rejects.

**Likely causes** (in order of probability):

1. **User-Agent detection** — the request goes through Chrome but the
   `.click()`-triggered fetch may be flagged differently
2. **Mouse-event signature** — real clicks include `isTrusted: true`;
   `.click()` calls don't
3. **CSRF token or session-cookie freshness** — there may be a token
   the JS attaches that we're not replicating
4. **Rate limit** — first 503 could have triggered a brief block

**Workaround for Phase 1:** use Playwright with `page.click()` and
realistic interaction timing (mouse hover before click, small jitter).
Playwright's clicks pass the `isTrusted: true` check that synthetic
DOM clicks fail. Should work without further effort.

---

## Implications for the migration plan

### Phase 1 architecture changes (from `ECI_MIGRATION_PLAN.md`)

Original plan called for an HTTP fetcher with `requests`. **That won't
work.** Required updates:

**`affidavit_fetcher.py`** — Playwright-based, not requests:

- Launches headless Chromium
- Navigates the cascading filter form
- Walks listing pages, scraping candidates + storing their encrypted
  profile URLs
- Visits each profile, clicks Download with realistic timing
- Catches the PDF blob from the download event
- Saves to `data/eci/raw_pdfs/<state>/<year>/<candidate_id>.pdf`

Estimated effort revised: **+1 week** to learn Playwright wiring vs simple
`requests`. Total Phase 1 effort: 3 weeks (was 2).

**`affidavit_parser.py`** — unchanged plan. Still PDF-only, pdfplumber-based.

**`results_fetcher.py`** — needs further reconnaissance against
`results.eci.gov.in` (separate portal) to confirm we can identify which
candidates were winners. Will be a Phase 0.5 task.

### Confidence assessment per the original plan

> ≥ 95% of winners have a parsed `total_assets_inr`

This now hinges entirely on PDF parser quality. **Affidavit PDFs follow a
standardised template** (Form 26 under the Conduct of Election Rules 1961)
— that's encouraging. Need to confirm with sample parses in Phase 0.5.

### Politeness considerations

ECI is a government site, but it still serves real users. Same care we
applied to myneta:

- Cache every PDF to `data/eci/raw_pdfs/` so repeat runs don't re-fetch
- Throttle to 1 request / 2 seconds minimum
- Set User-Agent string identifying the project + contact email
- Be prepared to back off if we hit broader 503s suggesting load impact

---

## Recommended next steps

1. **Phase 0.5 (~3 days):** Manually browse Delhi 2025 listings, download
   ~10 affidavit PDFs to disk via the real browser. Open them, confirm:
   - PDF text is extractable (vs scanned image requiring OCR)
   - Template is consistent across candidates
   - Field labels for assets / cases / education are stable enough for regex

2. **Phase 0.5 also:** explore `results.eci.gov.in` to understand how we'll
   identify winners (the affidavit portal doesn't distinguish winners from
   losers — both file affidavits).

3. **Then proceed to Phase 1** with Playwright-based fetcher.

If anything in Phase 0.5 looks bad (scanned PDFs, inconsistent templates,
no clean winner identification), we revisit before committing engineering
effort.

---

## Open questions

1. Does ECI have a public **terms of service or robots.txt** we should
   read? Worth checking before high-volume crawling.
2. The portal also shows historical totals (16,225 affidavits in the
   current default view). Is there a sitemap or per-election count we
   can use for scope estimation?
3. Will affidavits for **already-completed past elections** (e.g. Delhi
   2025 from Feb) still be served, or do they get archived after a
   retention window?

These are not blockers, but worth asking ADR-style polite emails to ECI's
public information officer once we have something to show.
