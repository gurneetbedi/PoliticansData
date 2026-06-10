# ECI multi-affidavit verification: Akhilesh Pati Tripathi (Delhi 2025)

User asked: "*Are the values correct?*" on candidates with multiple
affidavits, using the existing pulled candidates.

Picked Akhilesh Pati Tripathi (AAP, Model Town, Delhi 2025) — sitting MLA,
3 affidavits pulled (`__1666.pdf`, `__1673.pdf`, `__1679.pdf`), all 34
pages, in our DB at appearance_id 9787.

## TL;DR

**Values are NOT correct yet.** Three problems found:

1. **70× under-count.** DB total assets = **₹67,24,979**. Parser on the 3
   ECI PDFs produced ₹3,20,853 / ₹232 / ₹93,232 — three orders of
   magnitude too low.
2. **Dedup splits one candidate into two.** Because identity OCR (name +
   constituency) failed on 2 of 3 PDFs, dedup put them in a different
   bucket from the one PDF that parsed cleanly.
3. **Criminal case count off by 3.** Affidavit says "07 (SEVEN)" pending
   cases. DB says 10. Either ADR is inflating, or affidavit was amended
   later, or our parser captured the wrong row.

## Side-by-side

| Field | DB (myneta) | PDF 1666 | PDF 1673 | PDF 1679 |
|---|---|---|---|---|
| Name | Akhilesh Pati Tripathi | _(OCR failed)_ | AKHILESH PATI TR | _(OCR failed)_ |
| Party | AAP | AAM AADMI PARTY | AAM AADMI PARTY | AAM AADMI PARTY |
| Constituency | MODEL TOWN | _(OCR failed)_ | AC-18, MODEL TOWN | _(OCR failed)_ |
| Age | (missing) | (missing) | (missing) | 40 |
| Pending criminal cases | **10** | 7 | 7 | _(none)_ |
| Total assets | **₹67,24,979** | ₹3,20,853 | ₹232 | ₹93,232 |
| Movable subtotal | _(no breakdown)_ | ₹3,20,853 | ₹232 | ₹93,232 |
| Immovable subtotal | _(no breakdown)_ | _none_ | _none_ | _none_ |
| File size | — | 3.7 MB | 3.7 MB | 8.4 MB |

## Why the values came out wrong

OCR'd Part B page 26 of PDF 1666 (the "best" of the three):

```
A. Moveable Assets    RS. S. NOT  IL  IL.   NOT
(Total value)         315874/  4979 105\APPLICABLE...
                              ^^^^^^^^^
                              spouse value SPLIT by OCR-inserted whitespace
```

- Self movable = **₹3,15,874** — parser captured it (read as ₹3,20,853 — close)
- Spouse movable = **₹49,79,105** — parser missed it because OCR rendered the value as `4979 105` with a space, and the regex only captures contiguous digit runs
- Immovable section was scrambled into garbage like `Spa CA Ps MS > @ =` — that part of the table has the candidate's signature overlaid on the cells, and Tesseract can't read it

If we'd captured both movable cells, subtotal would be ₹52,94,979. Add the
immovable rows (which we can't read here) and we'd land near the DB
figure ₹67,24,979.

## At 300 DPI the OCR is significantly better

Re-OCR'd page 26 of PDF 1666 at 300 DPI with PSM 4:

```
A. Moveable Assets    RS. RS,  NOT  NIL  NIL  NOT
(Total value)         315874/  4979105  |APPLICABLE...
```

The space is gone — `4979105` reads as one number. Bumping DPI from 200
to 300 alone would fix the movable bug. **Immovable still fails** because
of the signature overlay — that's a different problem (handwriting on
top of typed numbers).

## What needs to change before we ingest at scale

Mandatory (parser robustness):

1. **OCR at 300 DPI**, not 200. ~50% slower per page but materially better
   accuracy on table-heavy pages.
2. **Movable / immovable regex must accept space-split numbers** (`4979 105`
   → `4979105`). Strip whitespace inside the captured digit run.
3. **Multi-PSM fallback.** Try `--psm 6` first, fall back to `--psm 4`
   (single column) if the abstract regex doesn't match. Different PDFs
   linearise better with different modes.
4. **Stop splitting candidates on partial OCR failure.** Right now the
   signature hash includes name, constituency, party, father. If any of
   those is empty, the hash differs and dedup splits the candidate. Use a
   weaker fingerprint when fields are missing (e.g. file-name prefix
   match, or signature-with-empty-slots-treated-as-wildcards).
5. **ADR-vs-ECI delta validator.** During ingest, compare ECI parsed
   total to the existing DB total. Flag candidates with >10% delta for
   manual review. Don't auto-replace until reviewed.

Nice to have:

- Detect signature overlays on cells and skip those PDFs (the candidate
  has a cleaner version on file — pick a different one of their multiple
  affidavits)
- For multi-affidavit candidates, parse ALL their PDFs and pick the one
  whose parse most closely matches the DB headline (cross-validation)

## Recommendation

**Pause the full Delhi 2025 pull at the current ~126 PDFs.** What we have
is a useful sample for hardening the parser — running it on the rest
without these fixes would produce 600+ records with the same data
quality issues.

Once the parser changes above land:

1. Re-parse the 126 PDFs we already have
2. Tabulate ECI-vs-DB deltas per candidate
3. If ≥85% of candidates match within 5%, the parser is ready — resume
   the full pull
4. Otherwise, more iterations needed

The 14 multi-affidavit candidates in this batch are the perfect test set
for canonical-PDF selection. We should make sure dedup keeps them in one
group, then verify the canonical pick gives values closest to the DB.
