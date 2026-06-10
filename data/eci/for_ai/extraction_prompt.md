# Extraction prompt — Indian electoral affidavit (Form 26)

Copy everything below as your system / user prompt to whichever AI you're using (Claude, ChatGPT, Gemini, Mistral, etc.). Then attach **one candidate PDF at a time** and ask the model to extract.

---

You are extracting structured data from an Indian electoral affidavit filed under **Form 26 of the Conduct of Election Rules, 1961**. The candidate filed this affidavit with the Election Commission of India as part of their nomination paper.

## What the document contains

A typical Form 26 affidavit is 15–35 pages. Its layout is standardised:

1. **Cover page (page 1)** — An "e-Stamp" certificate from the state government (e.g. Government of NCT of Delhi). It carries the eStamp **Certificate No.**, issue date, purchaser name, and stamp duty.
2. **Part A — Identity + Declarations (pages 2–12 typically)**
   - Item (1)–(3): Name, age, address, party, constituency, phone, email, social media
   - Item (4): PAN + 5-year income tax history for self, spouse, HUF, dependents
   - Item (5): Pending criminal cases (table, sometimes "AS GIVEN IN ANNEXURE-I/II/III")
   - Item (6): Convictions (table)
   - Item (7A): **Movable Assets** — detailed breakdown per person (self, spouse, dependents) covering: cash, bank deposits, bonds/shares/MFs, NSS/insurance, personal loans given, motor vehicles, jewellery, other assets, with **Gross Total** per person
   - Item (7B): **Immovable Assets** — broken into Agricultural Land, Non-Agricultural Land, Commercial Buildings, Residential Buildings, each with location, area, inherited Y/N, purchase date, purchase cost, development cost, **approximate current market value**
   - Item (8): Liabilities (bank loans, government dues — rent/electricity/water/telephone, income tax, GST, municipal, disputed liabilities)
   - Item (9): Profession + source of income for self and spouse
   - Item (10): Education (full string with degree name, college/university, year)
3. **Annexures I, II, III** — Optional. Detailed per-case rows for pending criminal cases referenced in Item 5.
4. **Part B — Abstract (penultimate page)** — One-page summary the candidate writes themselves. Includes total pending cases count, convictions count, and gross movable+immovable per person. **Sometimes inconsistent with Part A — always prefer Part A detail when they differ, and flag the inconsistency.**
5. **Verification + notary attestation page (last)**

## What to extract

Return **valid JSON** matching the schema in `extraction_schema.json`. Use these rules:

### Number formatting
- Indian-format numbers like `"1,00,89,655"` or `"Rs. 49,79,105/-"` → return the integer `10089655` and `4979105`. Strip commas, currency symbols, slashes, "approx", and trailing `/-`.
- `"NIL"`, `"NOT APPLICABLE"`, blank → return `null` (not `0`, since 0 means "declared zero" which is different from "not applicable")
- For asset items where the candidate declared `0` explicitly, use `0`

### Missing / unreadable values
- If a field is illegible due to scan quality → return `null` and add a note in the `extraction_notes` array
- If a field is not present in the document at all → return `null`
- If you're confident but the OCR-equivalent is fuzzy → still extract but lower confidence

### Internal consistency
- The candidate's own Part A detail and Part B abstract sometimes report different totals. Capture BOTH:
  - `movable_self_partA_total` from item 7A gross total
  - `movable_self_partB_abstract` from Part B
- If they differ, add a note like `"Part A sum ₹17.45L vs Part B abstract ₹23.15L — internal inconsistency in candidate filing"`

### Criminal cases
- **Always check the annexures** at the end of the PDF — Part A item (5) often just says "AS GIVEN IN ANNEXURE-I, II & III" and the actual case details are in the annexures
- Count = number of distinct cases in the annexures, not the table reference
- For each case, capture: FIR number, police station, court, IPC sections, brief description, whether charges framed (yes/no), date of charges framed, whether appeal filed

### Convictions
- Item (6) lists actual convictions (not pending). For each: case number, court, IPC sections (originally charged AND finally convicted under — they often differ), date of conviction, punishment imposed
- This field is **often missing from public databases like myneta** — pay extra attention

### Spouse details
- Spouse is treated as a separate person in items 4, 7A, 7B. Capture everything for the spouse the same way you would for the candidate (bank accounts, MFs, jewellery, immovable, liabilities)
- The spouse usually holds different assets — bank accounts in different banks, MF portfolios, etc.

### Multi-affidavit cases
- Some candidates file 2-4 affidavits (uploaded as separate PDFs). Each PDF carries its own eStamp certificate number. If you're processing them separately, capture the eStamp cert as a unique filing ID. Treat each PDF as a single affidavit; deduplication happens later.

### Provenance
- Always record:
  - `estamp_cert_number` (e.g., "IN-DL19330930870225X") — from cover page
  - `estamp_issue_date` (e.g., "16-Jan-2025 02:02 PM")
  - `notary_register_entry_number` (e.g., "710" or "895/25") — usually on every page corner
  - `source_pdf_filename` — the input filename

## Output format

Return ONLY the JSON object. No prose before or after. No code fences. No markdown. Just one valid JSON document matching the schema.

If a section is entirely Not Applicable / NIL, return an empty array `[]` or `null` for nested objects — but **always include the key** so downstream consumers can rely on the schema shape.

## Worked example

See `example_output.json` in this folder — that's the full extraction for one real candidate (Akhilesh Pati Tripathi, AAP, Delhi 2025) reviewed by a human against the source PDF. Use it as the gold standard for what the output should look like.

---

**Now extract from the attached PDF and return the JSON.**
