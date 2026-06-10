# Archived one-off scripts

These scripts each did a specific job once during the ECI proof-of-concept.
They're kept here for traceability — not part of the production pipeline.

| Script | What it did | When |
|---|---|---|
| `_quick_verify_kejriwal.py` | Targeted parse of pages 2-3, 15-17, 21 of Kejriwal's canonical PDF to confirm the Tesseract parser matched myneta's `₹4.24 Cr` total within 0.07%. First proof that ECI is viable as a data source. | Before parser hardening |
| `_quick_verify_akhilesh.py` | Same as above for Akhilesh — exposed Tesseract's signature-overlay failure on the immovable assets and the spouse-cell whitespace bug. Drove the multi-PSM, space-tolerant regex, 300 DPI, and dedup-signature fixes. | Hardening round |
| `_build_vision_demo_xlsx.py` | Generated `data/eci/vision_demo/delhi_vision_vs_myneta.xlsx` — the side-by-side comparison spreadsheet for Akhilesh + Ashish Sood showing vision LLM extraction beats myneta on 25+ fields and surfaces a 3-conviction data gap. | Vision PoC |

If you ever need to re-validate a single candidate against the live DB,
copy-paste one of these as a template — they're small and self-contained.

The production pipeline is in the sibling `scripts/` directory:
- `fetch_eci_affidavits.py` — Playwright crawler
- `preprocess_eci_pdfs.py` — EasyOCR + CV preprocessor
- `qc_preprocessed.py` — quality check on preprocessed JSON
- `build_ai_extraction_package.py` — organizes PDFs + schema for AI extraction
