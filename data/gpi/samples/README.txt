GPI Bootstrap CSVs
==================

`punjab_bootstrap_example.csv` shows the column shape and 6 example rows for
Punjab GSDP growth (E01) across FY18-FY23. The raw_values in the example are
approximate figures — VERIFY EACH ONE against the cited RBI Handbook table
before treating them as ground truth.

To use:
  1. Copy the example to a working file, e.g.:
       cp data/gpi/samples/punjab_bootstrap_example.csv data/gpi/punjab_seed.csv
  2. Verify / correct the example rows against actual RBI publications.
  3. Add rows for other indicators (E02, F01, F02, LO01, ...) as you collect them.
  4. Ingest:
       python scripts/gpi_ingest_csv.py data/gpi/punjab_seed.csv
  5. Compute:
       python scripts/gpi_compute_scores.py --state PB

For sources with real-time dashboards (JJM, SBM, PMGSY, OMMAS, NJDG), the
values are copy-paste from the dashboard on the retrieval date — record the
extraction date via the `extraction_method` = "manual" and `staleness` field.
