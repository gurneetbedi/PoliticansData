# ECI affidavit extraction — AI-agnostic package

This folder contains everything you need to extract structured data from Indian electoral affidavits using **any** AI you choose — Claude, ChatGPT, Gemini, Mistral, DeepSeek, etc.

## Folder contents

| File | Purpose |
|---|---|
| `pdfs/` | One canonical PDF per candidate, renamed `<seq>_<NAME>__<affidavit_id>.pdf`. 104 unique candidates from the current pull. |
| `index.csv` | Flat candidate roster — sequence number, name, filename, affidavit ID, file size, number of other affidavits. Open in Excel. |
| `index.json` | Same data as `index.csv` but JSON, with the full list of "other affidavits" per multi-affidavit candidate. |
| `extraction_prompt.md` | The prompt to copy-paste into your AI of choice. Tells the AI what Form 26 looks like, what fields to extract, and how to format numbers. |
| `extraction_schema.json` | JSON Schema describing every field. Use this to validate the AI's output programmatically. |
| `example_output.json` | A real candidate's fully filled extraction (Akhilesh Pati Tripathi, AAP, Delhi 2025), reviewed by a human against the source PDF. Use it as the gold standard for what the AI's output should look like. |

## How to use this with any AI

### Option A — Claude (claude.ai web or desktop app)

1. Open a new chat.
2. Drag `extraction_prompt.md` content into the prompt box (or paste it).
3. Attach one PDF from `pdfs/` as a file.
4. Send. You'll get back a JSON object.
5. Save the JSON output. Repeat for each candidate.

### Option B — ChatGPT (chat.openai.com)

1. New chat. Paste the contents of `extraction_prompt.md`.
2. Click the attachment icon, upload one PDF from `pdfs/`.
3. Add a second message: "Extract this PDF and return JSON only."
4. Save the JSON. Repeat.

### Option C — Google Gemini

1. New chat at gemini.google.com.
2. Paste the prompt + attach the PDF.
3. Same flow.

### Option D — Any other vision-LLM with PDF support

The prompt is provider-agnostic. As long as the model accepts PDFs (or you can convert each page to PNG and attach as images), the same instructions apply.

### Option E — Automated via API

If you want to script this with the OpenAI / Anthropic / Google API:

```python
# Pseudocode
import anthropic
client = anthropic.Anthropic()
prompt = open("extraction_prompt.md").read()
for pdf_path in sorted(Path("pdfs").glob("*.pdf")):
    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=8000,
        messages=[{
            "role": "user",
            "content": [
                {"type": "document", "source": {"type": "base64",
                                                 "media_type": "application/pdf",
                                                 "data": base64.b64encode(pdf_path.read_bytes()).decode()}},
                {"type": "text", "text": prompt},
            ],
        }],
    )
    json_out = response.content[0].text
    Path(f"output/{pdf_path.stem}.json").write_text(json_out)
```

Same shape works with OpenAI's responses API and Gemini's generateContent.

## Quality control

**For every candidate:**

1. **Open the source PDF alongside the extracted JSON** and spot-check 3-4 fields:
   - Candidate name (top of page 2)
   - Total movable assets (item 7A gross total, usually around page 14-15)
   - Pending criminal cases count (Part B abstract, last page before notary)
   - eStamp certificate number (page 1)

2. **Validate the JSON shape:**
   ```bash
   pip install jsonschema
   python -c "import json, jsonschema; \
     jsonschema.validate(json.load(open('output/001_AKHILESH_PATI_TRIPATHI.json')), \
                          json.load(open('extraction_schema.json')))"
   ```

3. **Cross-check against the existing DB (myneta-sourced):**
   - Total assets should match within ~5% (some variance is expected — see `extraction_notes`)
   - Criminal cases count is often off by ±2 in myneta — trust the affidavit
   - Convictions: myneta often shows 0; trust the affidavit which has the real number

## Multi-affidavit candidates

13 candidates in this pull filed multiple affidavits (sometimes 2-4 separate PDFs). The `pdfs/` folder includes ONLY the **canonical PDF per candidate** — defined as the largest file (most pages = most complete declaration).

The other affidavits for each candidate are listed in `index.csv` under `other_affidavits`. The full set is still in `../raw_pdfs/delhi-2025/raw_pdfs/`.

If you want to extract from a non-canonical version (e.g. an earlier filing to see how assets changed), grab it from the raw folder.

## What's NOT in this folder

- Rejected, Withdrawn, or Contesting affidavits — only **Accepted** candidates were downloaded.
- Candidates whose listing card name didn't extract cleanly (a small number from earliest smoke-test runs, stored as `__<id>.pdf` in raw folder — excluded here).
- The remainder of Delhi 2025 candidates (~470 candidates still to fetch). Re-run the fetcher to complete the pull.

## Field-by-field expectations

See `extraction_schema.json` for the canonical schema. The big sections:

1. **`candidate`** — Name, parents, address, contact, education, profession, PAN, 5-year income history
2. **`spouse`** — Spouse name, PAN, profession, income source, 5-year income
3. **`dependents`** — Children / parents listed as dependents
4. **`election`** — Party, constituency, voter serial
5. **`criminal`** — Pending cases (with detail from annexures) + convictions (with sections, court, punishment, date)
6. **`assets.movable`** — Per person (self / spouse): cash, bank accounts, mutual funds, vehicles, jewellery, insurance — fully itemised with account numbers, fund names, weights
7. **`assets.immovable`** — Per person: agricultural land, non-agri land, commercial buildings, residential buildings — with location, area, purchase date/cost, current market value
8. **`liabilities`** — Bank loans (per loan), government dues, disputed liabilities
9. **`abstract`** — Part B summary as the candidate wrote it (sometimes differs from Part A — capture both)
10. **`provenance`** — eStamp cert, dates, notary register entry

## Regenerating the package

If you fetch more PDFs later, re-run:

```bash
python scripts/build_ai_extraction_package.py
```

It will rescan `../raw_pdfs/delhi-2025/raw_pdfs/`, regroup by candidate, rebuild `pdfs/` and `index.csv`.
