# ECI affidavit deduplication method

When a candidate uploads multiple affidavit PDFs to affidavit.eci.gov.in,
we need a deterministic way to:

1. Group every PDF for the **same candidate, same election** together.
2. Group PDFs **representing the same filing** (re-uploaded versions of one
   legal document) within each candidate's bucket.
3. Pick a **canonical filing** per candidate — the one whose data we surface
   on the PolitiTrack profile page.

This is the spec the `scripts/dedup_affidavits.py` tool implements.

## Identifiers — what each does

| Identifier | Stability | What it identifies |
|---|---|---|
| File name (e.g. `Affidavit-1780926334.pdf`) | Unstable — ECI mints new IDs on each re-upload | A single uploaded PDF |
| **eStamp certificate number** (e.g. `IN-DL19131279062636X`) | Government-stamped at filing time, **immutable** | A single legal filing — survives ECI re-uploads of the same paper |
| Candidate signature hash (computed) | Deterministic from Form 26 Part A | A single candidate in a single election |

The eStamp cert is the key insight. Two PDFs whose cover pages bear the
same cert number are **the same legal filing** — they were stamped at the
same minute by the Stock Holding Corporation of India, and any byte
difference is just ECI's portal renaming the upload.

## Candidate signature

```
sha256(
    normalise(name)
    | normalise(father_or_husband_name)
    | normalise(constituency)
    | normalise(party)
)[:16]
```

where `normalise()` upper-cases, strips non-alphanumerics, collapses
whitespace. We don't include election year because all PDFs in a batch
come from the same election.

## Canonical filing rule

Among the filings a candidate has on file, the canonical one is chosen by:

1. **Numerically largest eStamp cert number** (digits only). SHCIL issues
   certs sequentially within state, so the larger cert was issued later in
   real time. This is more reliable than the OCR-extracted issue date,
   which sometimes fails on rougher scans.
2. **Largest total PDF bytes across the filing** as a tie-breaker —
   longer documents usually mean a more complete supplementary set.

## Canonical PDF rule

Within a single filing (one eStamp cert), if multiple PDFs were uploaded,
pick the **largest one** — same reasoning, more bytes = more pages = more
complete declaration.

## Validation on the 4-Kejriwal sample folder

Folder: `Affadivit/` — 5 PDFs, expected:

- 3 PDFs by Arvind Kejriwal (AAP, AC-40 New Delhi)
- 1 PDF by a different BJP candidate (Jangpura)
- 1 standalone eStamp paper (no Form 26 body)

What the dedup script produced (`data/eci/dedup.json`):

```
Candidates: 2 | Unclassified: 1

=== ARVIND KEJRIWAL  |  AAM AADMI PARTY  |  AC-40 NEW DELHI
    canonical filing: IN-DL19131279062636X
    canonical PDF:    Affidavit-1780926334.pdf  (28-page version)
    ★ cert IN-DL19131279062636X  (date OCR failed)         pdfs=1
        - Affidavit-1780926334.pdf
      cert IN-DL17805491029828X  (13-Jan-2025 05:23 PM)    pdfs=2
        - Affidavit-1780924720.pdf
        - Affidavit-1780926352.pdf

=== (name OCR failed)  |  BHARATIYA JANATA PARTY  |  JANGPURA
    canonical filing: IN-DL19165209220431X
    canonical PDF:    Affidavit-1780926397.pdf
    ★ cert IN-DL19165209220431X  (date OCR failed)         pdfs=1
        - Affidavit-1780926397.pdf

--- unclassified ---
    U05_918_633_20250120024725.pdf  -- no_form_26_part_a_found
```

All three Kejriwal PDFs were correctly attributed to the same candidate.
Two of them share the same eStamp cert (same legal filing, stored twice
under different ECI upload IDs) — exactly the duplication pattern we set
out to detect. The third PDF has a separate, **later** cert and is picked
as canonical — matching our prior reconciliation report which found this
28-page filing matches myneta's headline asset figure to 0.05%.

The Jangpura BJP candidate's name field failed to OCR cleanly on this
particular scan; we still produced a stable signature using party +
constituency, which means any future re-uploads from the same candidate
will hash to the same bucket.

The U05 PDF is the standalone eStamp paper that has no Form 26 body —
correctly bucketed as "unclassified" so it won't be confused with a real
affidavit.

## How this plugs into the migration plan

The Phase 1 ingest pipeline (per `ECI_MIGRATION_PLAN.md`) becomes:

1. Playwright fetcher downloads PDFs into `data/eci/raw_pdfs/<state>/<year>/`
2. `dedup_affidavits.py` runs over the folder and produces a grouping JSON
3. Only the **canonical PDF per filing** is fed to `parse_eci_affidavit.py`
4. The parsed records land in `eci_candidates` keyed by candidate
   signature; the `eci_filings` table preserves the full history (one
   row per cert)

This means re-running ingestion is idempotent: if ECI re-uploads a PDF
under a new ID but the same eStamp cert, dedup recognises it as an
existing filing and we skip re-parsing. If a candidate amends their
declaration (new eStamp cert with a later number), it shows up as a new
filing and the canonical pointer flips to the newer one.

## Known limitations

- **OCR can lose the name field** on rougher scans. Records where name
  is empty but party + constituency are present still produce a valid
  signature, but the signature is weaker (two candidates from the same
  party in the same constituency would collide). Per-state we expect
  one candidate per (party, constituency) so collisions are unlikely
  but should be monitored when scaling.
- **Cert-number-as-chronology assumes within-state issuance order**.
  Comparing certs across states (e.g. IN-DL vs IN-UP) is meaningless.
  Our dedup runs per-state so this is fine.
- **The script needs ~10 seconds per PDF** (4 OCR pages × 2-3s each).
  For a state-level ingest of 600+ PDFs this is ~100 minutes. Acceptable
  — runs once per election, then cached.
