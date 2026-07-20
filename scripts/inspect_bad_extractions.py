"""For each Bucket-3 case where Gemini produced empty or wrong data,
print the first ~800 chars of the Cloud Vision OCR text so we can see
whether the PDF actually contains the expected candidate.

Three verdicts you can eyeball:

  A. OCR text mentions the expected candidate name → PDF is right, Gemini
     just failed on this specific extraction. Fix: re-run Gemini on this
     file (rm the JSON, rerun llm_extract).

  B. OCR text mentions a completely different candidate → the PDF was
     mis-saved during a prior refetch collision. Fix: re-download this
     specific candidate via their profile URL.

  C. OCR text is empty / gibberish → the source PDF is genuinely bad
     (scanned at low resolution, rotated, ink-bled). Fix: re-fetch to try
     for a better copy; if still bad, accept as unrecoverable.

Usage:
    python scripts/inspect_bad_extractions.py --state "Rajasthan" --year 2023
"""
from __future__ import annotations
import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--state", required=True)
    ap.add_argument("--year", type=int, required=True)
    ap.add_argument("--stems", default="",
                    help="Optional comma-separated basenames without .pdf; "
                         "if omitted, uses ALL bad-extraction cases")
    args = ap.parse_args()

    slug = args.state.lower().replace(" ", "") + f"_{args.year}"
    lx = ROOT / "data/eci/for_ai/llm_extracted" / slug
    pp = ROOT / "data/eci/for_ai" / f"preprocessed_{slug}"

    if not lx.exists() or not pp.exists():
        sys.exit(f"Missing folders under data/eci/for_ai/")

    if args.stems:
        stems = [s.strip() for s in args.stems.split(",") if s.strip()]
    else:
        # Auto-detect bad extractions
        stems = []
        for f in sorted(lx.glob("*.json")):
            try:
                r = json.loads(f.read_text())
            except Exception:
                continue
            ext = r.get("extraction") or {}
            if "_raw" in ext:
                stems.append(f.stem)
                continue
            name = (ext.get("identity") or {}).get("name_in_english") or ""
            const = (ext.get("political") or {}).get("constituency_name") or ""
            if not name or not const:
                stems.append(f.stem)

    print(f"Inspecting {len(stems)} bad-extraction case(s)\n", file=sys.stderr)

    # Full detail goes to a report file so the terminal isn't overwhelmed.
    out_path = ROOT / "data/reports" / f"bad_extractions_{slug}.txt"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_f = out_path.open("w", encoding="utf-8")

    # Tally per verdict for the terminal summary.
    verdicts = {"right_person_bad_ocr": [], "wrong_person": [],
                "empty_ocr": [], "raw_recoverable": []}

    def _fuzzy_contains(hay: str, needle: str) -> bool:
        """Token-based match: at least 60% of the name's word tokens
        (each 4+ chars) appear in the OCR text after lowercasing + non-alnum
        strip. Tolerates OCR corruption of individual letters better than
        whole-name substring matching."""
        import re
        toks = [re.sub(r"[^a-z0-9]+", "", t.lower())
                for t in needle.split()]
        toks = [t for t in toks if len(t) >= 4]
        if not toks:
            return False
        norm_h = re.sub(r"[^a-z0-9]+", " ", hay.lower())
        hits = sum(1 for t in toks if t in norm_h)
        return hits / len(toks) >= 0.6

    for i, stem in enumerate(stems, 1):
        expected = stem.rsplit("__", 1)[0].replace("_", " ")
        pp_path = pp / (stem + ".json")

        # Full OCR text (all pages) — used for classification. We show
        # only the first 2000 chars in the report but classify on the
        # whole text so a name that appears on page 5 still counts.
        text_full = ""
        text_excerpt = ""
        if pp_path.exists():
            try:
                pdata = json.loads(pp_path.read_text())
                pages = pdata.get("pages") or []
                if pages:
                    text_full = "\n".join(p.get("text", "") for p in pages)
                    text_excerpt = text_full[:2000]
            except Exception as e:
                text_excerpt = text_full = f"<Cloud Vision JSON unreadable: {e}>"
        else:
            text_excerpt = text_full = "<no Cloud Vision output>"

        try:
            gdata = json.loads((lx / (stem + ".json")).read_text())
            ext = gdata.get("extraction") or {}
            g_name = (ext.get("identity") or {}).get("name_in_english") or "<empty>"
            has_raw = "_raw" in ext
        except Exception:
            g_name, has_raw = "<unreadable>", False

        # Auto-classify. Use full OCR text (not the truncated excerpt) so
        # we don't misclassify a candidate whose name appears on page 5+.
        # Threshold "empty" if the full text is under 200 non-whitespace
        # chars — a real affidavit has thousands.
        stripped_len = len("".join(text_full.split()))
        if has_raw:
            verdict = "raw_recoverable"
        elif stripped_len < 200:
            verdict = "empty_ocr"
        elif _fuzzy_contains(text_full, expected):
            verdict = "right_person_bad_ocr"
        else:
            verdict = "wrong_person"
        verdicts[verdict].append(stem)

        # Full detail → report file
        marker = "  [_raw]" if has_raw else ""
        out_f.write(f"[{i:>2d}] {stem}.pdf{marker}\n")
        out_f.write(f"     Verdict:  {verdict}\n")
        out_f.write(f"     Expected: {expected}\n")
        out_f.write(f"     Gemini:   {g_name}\n")
        out_f.write(f"     OCR total chars (all pages, non-ws): {stripped_len}\n")
        out_f.write(f"     OCR excerpt (first 2000 chars):\n")
        out_f.write("     " + "─" * 70 + "\n")
        for line in text_excerpt.splitlines()[:20]:
            out_f.write(f"     | {line[:110]}\n")
        out_f.write("\n")

    out_f.close()

    # Terminal summary
    print("\n" + "=" * 70, file=sys.stderr)
    print(f"VERDICTS ({len(stems)} candidates):", file=sys.stderr)
    print("=" * 70, file=sys.stderr)
    labels = {
        "right_person_bad_ocr": "PDF is right person, Gemini fumbled → re-run Gemini",
        "wrong_person":         "PDF contains DIFFERENT candidate → refetch this specific one",
        "empty_ocr":            "PDF unreadable (bad scan) → refetch, maybe unrecoverable",
        "raw_recoverable":      "Gemini _raw string parseable → salvage without re-run",
    }
    for v, stems_in_v in verdicts.items():
        print(f"\n  {len(stems_in_v):>3d}  {v}", file=sys.stderr)
        print(f"       → {labels[v]}", file=sys.stderr)
        for s in stems_in_v[:5]:
            print(f"       • {s}.pdf", file=sys.stderr)
        if len(stems_in_v) > 5:
            print(f"       • … + {len(stems_in_v) - 5} more (see report file)",
                  file=sys.stderr)
    print(f"\nFull per-candidate detail written to:", file=sys.stderr)
    print(f"  {out_path.relative_to(ROOT)}", file=sys.stderr)


if __name__ == "__main__":
    main()
