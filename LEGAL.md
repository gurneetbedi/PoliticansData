# Legal Considerations for PoliTrack.in

This document is a practical guide to the legal landscape for an Indian political transparency platform. It is **not legal advice**. Before launching publicly, consult a lawyer registered with the Bar Council of India who has experience in media, defamation, or digital rights law. Organisations like the Internet Freedom Foundation (IFF) maintain lawyer referrals for civic tech.

## 1. The data itself — is it legal to publish?

**Short answer: yes, with attribution.**

All affidavit data comes from candidate disclosures filed with the Election Commission of India under Section 33A of the Representation of the People Act, 1951, and Form 26 of the Conduct of Election Rules, 1961. These disclosures are required by Supreme Court orders (*Union of India v. Association for Democratic Reforms*, 2002; *People's Union for Civil Liberties v. Union of India*, 2003) which established the voter's right to know.

ADR has aggregated and published this data on myneta.info since 2003. Multiple media outlets (The Hindu, Indian Express, NDTV) republish ADR data routinely. Aggregating and re-publishing is well-established practice. The data is, by Supreme Court mandate, public.

**What you must do:**

Attribute clearly. Every page should credit ADR / myneta.info as the source. Link the original source URL on every politician page (we already do this via `source_url`).

State the disclaimer. Include text like: *"All financial and criminal data is self-declared by candidates in affidavits filed with the Election Commission of India. PoliTrack restructures and visualises this data but does not verify it. For the authoritative version, see the Election Commission of India website."*

Honor takedown requests in good faith. Although the data is public, if a politician asks you to correct a factual error — say their name is misspelled, or a case was dismissed and isn't reflected — fix it promptly and document the correction.

## 2. Defamation risk (most important section)

This is where the real risk lies. Indian defamation law has both **civil** (Section 19 of the Code of Civil Procedure, tortious defamation) and **criminal** (Sections 499-500 of the Indian Penal Code) tracks. A politician unhappy with how they are portrayed could file under either.

**Where you are protected:**

Truth is an absolute defence in civil defamation. Truth + public good is a defence in criminal defamation (IPC 499 Exception 1). Political conduct and the assets/cases of elected representatives clearly fall in the public-interest zone.

Fair comment on a matter of public interest is protected. Showing court-filed criminal case data without editorial commentary is the safest position.

**Where you are exposed:**

Pending vs. convicted matters. A criminal case is an *allegation* until convicted. Listing "criminal cases" without making this clear could be construed as imputation of guilt. **Required wording:** never say "criminal", always say "**pending criminal cases**" or "**cases as declared in affidavit**". Avoid words like "tainted", "criminal politician", "accused" in your own copy — let users draw conclusions.

Editorial language and rankings. The leaderboard "Most Criminal Cases" should be labelled "**Most Pending Cases (as declared)**" to be safe. The "Biggest Wealth Gainers" feature is factual and defensible, but avoid loaded captions like "got rich how?"

Showing photos with cases. If you ever add candidate photos, place them next to the disclaimer ("Cases listed are pending / not adjudicated"), not next to inflammatory text.

User-submitted content. If you ever allow comments, you need active moderation — Section 79 of the IT Act gives you safe harbour but only if you respond to takedown requests within 36 hours of being notified.

**Recommended wording changes for the current UI:**

Rename "Most Criminal Cases" → "Most Pending Cases (as declared)". Rename "Top Cases" tab on the homepage similarly. On the detail page, label every case with "**Status: Pending**" or actual status from the source. Add a footer disclaimer on every page reading: *"Cases listed are pending; candidates are presumed innocent until proven guilty."*

## 3. The Digital Personal Data Protection Act, 2023 (DPDP)

The DPDP Act came into force in 2023. It treats individuals (Data Principals) as the owners of their personal data.

**Why you are mostly fine:**

Section 17(1)(a) of DPDP exempts data made public by the data principal themselves *or by anyone under legal obligation*. Affidavits filed with the ECI fall under this exemption — candidates legally disclose this information.

**Where to be careful:**

Do not collect additional personal data beyond what is in affidavits — no scraping of WhatsApp numbers, home addresses, family member details, religious affiliations.

If you ever store user accounts (people who log in to your site), you become a Data Fiduciary under DPDP and need a privacy policy, consent mechanism, and grievance officer. Avoid this complexity by **not adding accounts** until you really need them.

Right to correction: if a politician asks you to correct affidavit data that they themselves have updated in a newer filing, you should update it.

## 4. Photos, logos and image licensing

**Politician photos:**

The cleanest source is **Wikimedia Commons** — most major politicians have photos there under Creative Commons or public-domain licences. Each photo will specify its licence (CC-BY-SA 4.0 is most common). Attribute the photographer/source as required by the licence. Never hot-link from Wikipedia — download the image and host locally with attribution.

ADR / myneta.info: photos there are often unattributed and not safe to reuse. Don't pull from there.

ECI candidate affidavits sometimes contain a passport photo. These were submitted by the candidate for public disclosure and are arguably public domain, but the safe interpretation is to **not redistribute** these without explicit consent.

If a politician requests removal of their photo, comply immediately. The data (assets, cases) is public; the photo is a softer issue.

**Party logos / election symbols:**

ECI publishes official party symbols at https://eci.gov.in/political-parties/. These are public-record symbols of registered political parties. Wikimedia Commons hosts SVG versions of most symbols, usually marked as public domain (in India, government works including ECI publications are in the public domain).

You can use party symbols/logos for **identification** (showing which party a politician belongs to). You **cannot** use them in a way that implies endorsement by the party — e.g., do not put a party logo on a "Best of [Party]" page.

For now, PoliTrack uses **text initials with brand colors** rather than actual logos — this is the safest choice and avoids any licensing complexity. Logos can be added later via Wikimedia Commons SVGs with proper attribution.

## 5. Election Commission Code of Conduct (only during elections)

The Model Code of Conduct kicks in when elections are announced. During this period, certain content restrictions apply to ensure fair elections.

What you can keep doing: show factual affidavit data. This is what ADR does during every election.

What you should avoid: don't add "endorsement", "voter recommendations", or "best candidate" features. Don't add party-favourable rankings during the MCC period. If you publish original analysis (not just data display), be scrupulously even-handed across parties.

It is worth pausing any new feature launches during the MCC of an election cycle you cover, just to avoid scrutiny.

## 6. IT Act compliance (the basics)

Section 79 (intermediary safe harbour) protects you if you act on takedown notices within 36 hours.

Maintain a public **Grievance Officer** email address. Include a "Report an issue" link in the footer.

The IT Rules 2021 require some platforms to publish monthly compliance reports. These thresholds (>50 lakh users) won't apply to PoliTrack early on, but be aware of them.

## 7. Hosting jurisdiction

Hosting in India means Indian law applies cleanly. Hosting abroad (e.g., on Vercel/Render servers in the US) means Indian courts can still order your domain blocked, and you'll be in a weaker position to fight it. Either is workable, but plan for the former: register the company/non-profit in India even if you host abroad.

## 8. Practical checklist before launch

Code/content changes to make before going live publicly:

- [ ] Rename "Criminal Cases" labels to "Pending Cases (as declared)" everywhere
- [ ] Add a footer disclaimer on every page about presumption of innocence and source attribution
- [ ] Add a dedicated "/about" page explaining: what we do, what we don't do, where data comes from
- [ ] Add a "/methodology" page explaining how data is gathered, what's verified vs. self-declared
- [ ] Add a "/corrections" or "Report an Issue" form, with a published response SLA
- [ ] Add a "/privacy" page (short — "we collect nothing about visitors, see DPDP exemptions")
- [ ] Add a "/terms" page covering use of the site and data attribution
- [ ] Pick a **non-profit legal structure** — Section 8 company is the standard for civic tech in India. Costs ~₹15-20k to set up via a CA. Gives you legal cushion and grant eligibility.
- [ ] Get the source code open from day one (MIT or AGPL) — this is itself a defence: "we're a transparency project, source available."
- [ ] Consult a media-law lawyer for a 1-hour review of the live site before any press coverage

## 9. Useful references

- *Union of India v. ADR*, AIR 2002 SC 2112 — established voter right-to-know
- *PUCL v. Union of India*, 2003 — disclosure of criminal cases is mandatory
- Representation of the People Act, 1951, Sections 33A and 33B
- Conduct of Election Rules, 1961, Form 26
- Digital Personal Data Protection Act, 2023 — full text at meity.gov.in
- Information Technology Act, 2000, Section 79 (intermediary liability)
- ADR's own legal page: https://adrindia.org/content/about-adr — they have navigated this terrain since 1999

## 10. If you get a legal notice

Don't ignore it. Respond within the stated timeframe (typically 30 days).

Take immediate action only on **factual errors** (correct or remove the disputed data). Do not capitulate on properly sourced public data — that sets a bad precedent for everyone else doing transparency work.

Contact the Internet Freedom Foundation (iff.org.in) — they offer pro bono support for civic tech facing legal action and have handled defamation suits against journalists/researchers.

Document everything: the notice, your response, supporting source URLs.

The risk is real but manageable. ADR has been sued multiple times over the past two decades and has won the substantive cases because the underlying data is public-record affidavits. Your strongest defence is: **show the source, show the date, show the URL on myneta or ECI**. We already do this — keep doing it.
