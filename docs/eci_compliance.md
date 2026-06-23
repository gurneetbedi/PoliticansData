# ECI affidavit pipeline — compliance & ethics

This document captures the full thinking behind how the ECI fetcher and
loader engage with the Election Commission of India's affidavit portal
(<https://affidavit.eci.gov.in>) and what we do with the data once it
lands in our system.

It is the public counterpart to a private mistake we made earlier with
ADR/myneta — we scraped at scale without notifying them, and received a
cease-and-desist. The point of this document is to learn from that.

## Are these affidavits public records?

Yes — under three converging bases:

### 1. Government Open Data License – India (GODL)

The most direct authority. Issued under the **National Data Sharing
and Accessibility Policy (NDSAP), 2012** by the Department of Science
& Technology, Government of India. The current license text lives at
<https://ap.data.gov.in/godl>.

The GODL grants:

> "a worldwide, royalty-free, non-exclusive license to use, adapt,
> publish (either in original, or in adapted and/or derivative forms),
> translate, display, add value, and create derivative works (including
> products and services), for all lawful commercial and non-commercial
> purposes."

Applies to "all data and information created, generated, collected and
archived using public funds provided by Government of India directly or
through authorised agencies" — which covers Form 26 affidavits filed
with the ECI. Conditions are: attribution to the source, no implied
warranty, and compliance with all applicable laws.

### 2. Statutory basis — Rule 4A, Conduct of Election Rules 1961

**Form 26** is the affidavit every candidate must file with their
nomination paper under Rule 4A, pursuant to the Representation of the
People Act, 1951. The contents are mandated to be disclosed publicly so
voters can make an informed choice. ECI's running of
<https://affidavit.eci.gov.in> is the operational expression of that
disclosure obligation — they are not "hosting it grudgingly";
publication is the point.

### 3. Constitutional basis — Supreme Court rulings

In **Union of India v. Association for Democratic Reforms (2002)** and
the subsequent **People's Union for Civil Liberties (PUCL) v. Union of
India (2003)**, the Supreme Court of India held that the voter's right
to information about a candidate is part of the fundamental right to
freedom of expression under Article 19(1)(a) of the Constitution. That
is the constitutional basis for the affidavit regime — and for
downstream civic uses like this project.

So: there is no scenario where the *information itself* is not for
public consumption. The question is only about *how* we go about
collecting and republishing it.

## Why this pipeline is not equivalent to the ADR/myneta case

ADR is a **private non-profit** that built a curated derivative work —
their database of all affidavits, with their own data cleaning,
aggregation, and presentation. We had been scraping that derivative
work without permission, which is what they objected to.

ECI is **the government body that publishes the underlying primary
records**. Affidavits are public-domain government documents. Our
posture toward them is different in principle, but not in *practice* —
we should still be polite, transparent, and responsive.

## Politeness controls baked into the fetcher

`scripts/fetch_eci_affidavits.py` enforces:

| Control | Default | Where in the code |
|---|---|---|
| Sleep between candidate fetches | 2 seconds | `DEFAULT_DELAY_S` |
| Polite identification header | `X-PolitiTrack-Contact` | `CONTACT_HEADER` |
| User-Agent string | Real Chrome 126 (not because we're hiding — Akamai blocks `HeadlessChrome` by default) | `USER_AGENT` |
| Status filter | `Accepted` only | `--status` arg, default |
| Cap on listing pages | 200 | `--max-pages`, default |
| Resumable manifest | After each candidate | `manifest.jsonl` |
| Akamai-block detection | Bails immediately + saves HTML | `is_akamai_blocked()` |
| Headless mode | Off by default | `--headless` flag |

If ECI ever asks for stricter limits (longer sleeps, smaller window
hours, lower concurrent connections), the script accepts CLI flags for
each; no code change required to comply.

## Data we extract vs. data we will publish

The ECI affidavit contains many fields that ECI publishes openly but
which we should treat carefully when re-aggregating into a searchable
database. The Digital Personal Data Protection Act, 2023 (DPDP) creates
an obligation to limit the *processing* of personal data even when its
*original disclosure* was lawful.

### Fields we extract from each affidavit (this is full)

Every field in [`data/eci/for_ai/extraction_schema.json`](../data/eci/for_ai/extraction_schema.json) — identity, election, criminal cases, full asset / liability breakdown, abstract, provenance. We store the full extraction internally to enable accurate reconciliation against existing records.

### Fields we will REDACT before publishing on PolitiTrack

Before any ECI-sourced record lands in the production database that
serves the live site, the loader (TODO) will drop or generalise:

| Field | Redaction | Reason |
|---|---|---|
| `candidate.pan` | DROP | Direct financial identifier; sensitive personal data under DPDP |
| `spouse.pan` | DROP | Same; family member's |
| `candidate.phone[]` | DROP | Personal contact info; not relevant to public-interest disclosure |
| `candidate.email` | DROP | Same |
| `candidate.address` | GENERALISE | Keep PIN code + ward/constituency-level descriptor; drop house number, gali, locality |
| `dependents[].name` | DROP if minor child | Children should not be in a searchable political database |
| `dependents[].pan` | DROP | Same |
| `assets.movable.*.bank_accounts[].account_no` | DROP | Account numbers are sensitive financial identifiers |
| `assets.movable.*.investments_bonds_mf[].registration_no` | DROP | Same |
| `assets.movable.*.vehicles[].registration` | DROP | Vehicle reg numbers are PII |
| `provenance.notary_register_entry_number` | KEEP | Aids verifiability without identifying anyone personally |

### Fields we will PUBLISH

- Candidate name, party, constituency, age, gender
- Education (full string)
- Profession + source of income, for candidate and spouse
- Income totals per year (not the underlying bank/FD line items)
- Criminal cases — count + per-case sections, court, charges-framed status (this is exactly the information voters use)
- **Convictions** — case number, court, sections, date, punishment (we believe this is the single most under-reported field today)
- Total assets and total liabilities (aggregates, not the underlying account-by-account breakdown)
- Property holdings — broad category (agricultural / non-agri / commercial / residential), area, current market value. Drop survey numbers and exact addresses; generalise to district level
- Spouse name (the candidate herself disclosed it publicly), spouse profession, spouse income source (the aggregate, not bank details)
- The eStamp certificate number and the link back to the source PDF on `affidavit.eci.gov.in` — for verifiability and attribution

This redaction list will be implemented in the loader and reviewed
before any candidate record goes live. The list is also versioned —
changes will be discussed in pull requests.

## Right to be forgotten

The site will include an opt-out workflow. Any candidate who wishes to
have their PolitiTrack record removed or corrected can email the project
contact. We will:

1. Remove the record from the public-facing site within 7 days
2. Annotate the underlying database row with a takedown notice
3. Maintain a small public log of takedown actions taken — counts only,
   not names — to keep the process transparent

The original ECI PDF is unaffected and remains available on the ECI portal.

## Outreach to ECI

Before we run the fetcher against a second state, we will email ECI's
Public Information Officer with a clear description of:

- What the project is and who runs it
- What we extract and what we publish (the redaction list above)
- How we throttle our requests
- How to reach us if anything is wrong or if rate limits need adjusting
- A direct link to this compliance document for full context

Draft of the email is at [`docs/eci_outreach_email_draft.md`](eci_outreach_email_draft.md).

If ECI asks us to stop or to adjust the approach, we comply, document
the exchange, and update this document accordingly. Same posture we
adopted for ADR after their notice — no argument, no delay.

## Takedown switch

`scripts/fetch_eci_affidavits.py` can be disabled with a single
environment variable check (mirroring `ALLOW_MYNETA_SCRAPE` in
`app/scrapers/myneta_client.py`). If we ever need to halt the pipeline
formally, this is one line of code and a deploy.

---

This document is living — open a PR if you spot something we should
tighten.
