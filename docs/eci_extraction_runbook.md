# ECI extraction runbook — running all 104 PDFs through an LLM

Once preprocessing is done (or even before, if you want the highest
quality), `scripts/extract_via_llm.py` takes a folder of PDFs (or
preprocessed text JSONs) and produces one structured JSON per candidate
in `data/eci/for_ai/output/` — matching `extraction_schema.json`.

## Setup (one-time)

```bash
cd "Politicians Project"
source .venv-eci/bin/activate

pip install anthropic jsonschema

# Get an API key at https://console.anthropic.com/, then:
export ANTHROPIC_API_KEY=sk-ant-...
```

If you don't have an Anthropic account, sign up — you get $5 free credit
which is enough to extract every Delhi 2025 candidate in `--mode pdf`,
and far more than enough in `--mode text`.

## Smoke test (2 candidates, ~1 minute)

```bash
python scripts/extract_via_llm.py --mode pdf --limit 2
```

Expected output:

```
Mode: pdf  |  Input dir: .../data/eci/for_ai/pdfs  |  Output dir: .../data/eci/for_ai/output
Model: claude-sonnet-4-6  |  2 candidates to process
[1/2] 001_AALEY_MOHAMMED_IQBAL__2011.pdf  ... done  in= 78234 out=2412  $0.272  ✓
[2/2] 002_ABDUL_REHMAN__1900.pdf  ... done  in= 65021 out=1987  $0.225  ✓
```

The `✓` means the output JSON validated cleanly against our schema. A
`⚠ N schema warnings` means it produced JSON but a few fields drifted
from the schema — the file is still saved, you can inspect and decide.

Open one and eyeball it:

```bash
cat data/eci/for_ai/output/001_AALEY_MOHAMMED_IQBAL__2011.json | jq
```

You should see the same shape as `example_output.json` — `candidate`,
`spouse`, `dependents`, `election`, `criminal`, `assets`, `liabilities`,
`abstract`, `provenance` keys.

## Full run

```bash
python scripts/extract_via_llm.py --mode pdf
```

For ~104 candidates at average ~0.25 cents per candidate, full Delhi
2025 lands at **~$25–35** total and takes **~30–45 minutes** running
unattended. Resumable — Ctrl-C is safe, re-running picks up where it
stopped.

If your budget is tighter and you've already preprocessed, use the
`text` mode:

```bash
# First, make sure preprocessing has completed:
python scripts/preprocess_eci_pdfs.py

# Then extract in text mode (~$5 total instead of ~$30):
python scripts/extract_via_llm.py --mode text
```

Text mode is ~7× cheaper because we send the OCR'd text instead of the
PDF images. Quality is slightly lower on the worst-OCR'd pages but
acceptable on the majority. For Delhi 2025, this means roughly:

| Mode | Cost | Quality | Time |
|---|---|---|---|
| `pdf`  | ~$30 | best | 30-45 min |
| `text` | ~$5  | ~90% as good, weaker on signature-overlaid pages | 15-25 min |

## Spot-checking

After a full run:

```bash
ls data/eci/for_ai/output/ | wc -l        # should be 104
jq '.candidate.name, .criminal.pending_cases_count, .liabilities.grand_total' \
    data/eci/for_ai/output/*.json | head -30
```

Open 3-4 random outputs alongside their source PDFs and check:

1. `candidate.name` matches the PDF
2. `criminal.pending_cases_count` matches the affidavit's Part B abstract
3. `liabilities.grand_total` matches the affidavit's item (8) sum
4. `provenance.estamp_cert_number` matches the cover page

## Diff against the existing myneta DB

Once outputs land, you can re-run our existing reconciliation tool to
see which candidates match myneta's numbers within tolerance and which
deviate (those are usually the ones where myneta missed convictions, or
where the candidate filed a corrected affidavit later):

```bash
python scripts/reconcile_eci_vs_db.py \
    --extracted-dir data/eci/for_ai/output \
    --db lokvani.db \
    --election-year 2025 --state Delhi \
    --out data/eci/reconcile_delhi2025.jsonl
```

*(Note: this command pre-supposes a small tweak to reconcile_eci_vs_db.py
to read pre-extracted JSONs directly instead of calling parse_eci_affidavit
on PDFs — we'll wire that up after the first batch of extractions.)*

## Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| `ANTHROPIC_API_KEY not set` | Forgot the env var | `export ANTHROPIC_API_KEY=sk-ant-...` |
| `Rate limit` / 429 from API | Too many parallel candidates | Default is sequential — should not hit. If it does, wait a minute and re-run; resumable. |
| Schema warnings on most candidates | LLM drifting from schema | Open one output, see which field is wrong. Usually a wrong type (e.g. integer vs string). Tweak `extraction_prompt.md` to be more explicit, re-run with `--refresh`. |
| Cost much higher than estimate | You're running `--mode pdf` on 30+ page affidavits at lots of tokens | Switch to `--mode text` after preprocessing. |
| Output JSON has prose before the `{` | Model didn't follow the "JSON only" instruction | The script strips ```json fences defensively, but if you see issues, retry with `--refresh` |
