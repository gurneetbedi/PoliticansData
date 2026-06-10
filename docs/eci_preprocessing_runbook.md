# ECI preprocessing pipeline — runbook

Image-sanitation + EasyOCR + spatial column-aware extraction for the 104
Delhi 2025 candidate PDFs.

## What this does

For each PDF in `data/eci/for_ai/pdfs/`, produces a JSON of cleaned text
per page in `data/eci/for_ai/preprocessed/`. The per-page text is:

- Pulled from the PDF's digital text layer when present (fast — no OCR)
- Otherwise OCR'd at 300 DPI after stamp removal, with column-separator
  bars (` | `) injected between table cells to prevent text bleed-through

Then a QC tool tags each candidate `CLEAN / LIGHT / FLAG / EMPTY` based on
whether key markers (name, PAN, party, currency, Part-B header) were found.

## One-time setup

```bash
cd "Politicians Project"
source .venv-eci/bin/activate          # the venv you set up for Playwright

pip install easyocr opencv-python pdf2image pdfplumber pillow
# poppler should already be installed from earlier; if not:
brew install poppler tesseract
```

`easyocr` is ~1 GB of PyTorch + model weights. On first invocation it
downloads ~64 MB of detection + recognition weights to
`~/.EasyOCR/model/`.

## Smoke test (1 candidate first, ~30 seconds)

Start with our known-tough case — Akhilesh, the one with the signature
overlay that broke Tesseract:

```bash
python scripts/preprocess_eci_pdfs.py \
    --only 005_AKHILESH_PATI_TRIPATHI__1679.pdf
```

Expected output:

```
Initialising EasyOCR (English, gpu=False) — first run downloads ~64 MB ...
[1/1] 005_AKHILESH_PATI_TRIPATHI__1679.pdf  ... done in 45s (pdfplumber=22, easyocr=12)
```

Then peek at the output:

```bash
python -c "
import json
d = json.load(open('data/eci/for_ai/preprocessed/005_AKHILESH_PATI_TRIPATHI__1679.json'))
# Show page 25 (Part B abstract) — the one Tesseract botched
for p in d['pages']:
    if 'PART' in p['text'].upper() and 'ABSTRACT' in p['text'].upper():
        print(p['text'][:2000])
        break
"
```

You should see the Part B values readable, including the spouse movable
column (which Tesseract split into "4979 105" with a space).

## Full run (104 candidates)

Estimated time: ~50-70 minutes on a typical Mac (most pages hit the
pdfplumber fast path). Resumable — re-running skips files that already
have output.

```bash
python scripts/preprocess_eci_pdfs.py
```

Then QC:

```bash
python scripts/qc_preprocessed.py
```

This prints a status breakdown and writes a per-candidate CSV at
`data/eci/for_ai/preprocessed/_qc_report.csv`.

## Reading the QC report

| Status | What it means | Action |
|---|---|---|
| `CLEAN` (5/5 markers) | Name, PAN, party, currency, Part-B all found in text | Production-ready. Move on. |
| `LIGHT` (4/5 markers) | One marker missing | Probably fine — open the JSON, see what's missing |
| `FLAG` (≤3/5 markers) | Multiple key markers missing | Inspect the PDF + JSON. May need LLM escalation. |
| `EMPTY` (<500 chars) | Almost no usable text extracted | Definitely escalate — could be a fully-handwritten or photocopied affidavit |

**Decision rule:** if `CLEAN + LIGHT` covers ≥ 85% of candidates, the cheap
EasyOCR pipeline is doing the job and you can skip the LLM tax. If it's
below 85%, send the FLAG + EMPTY rows through the LLM as a top-up pass.

## What to do with the output

The preprocessed JSONs are still just per-page text — not the final
structured JSON our schema needs. Two paths from here:

1. **Send the cleaned text (not images) to an LLM** for the structured-
   extraction step. Use the same `extraction_prompt.md` but reference the
   text JSONs instead of the PDFs. Cost: ~$0.05 per candidate (~$5 for
   Delhi; ~$300 for pan-India).
2. **Write regex parsers** that read the column-separated text directly.
   Free, but more brittle. Probably gets 75-85% MATCH on its own.

Most cost-effective: combine. Use regex for the structured fields when the
QC report says CLEAN, escalate FLAG/EMPTY candidates to the LLM.
