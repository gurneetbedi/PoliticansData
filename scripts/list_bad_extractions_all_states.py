"""Cross-state inventory of every "bad" Gemini extraction, scoped to
the top-N allowlist per state. Writes one row per bad case with enough
context to diagnose patterns — no fixes applied.

Output: data/reports/bad_extractions_all_states.csv with columns:
  cycle              e.g. rajasthan-2023
  basename           source PDF filename stem
  failure_type       raw | empty_name | empty_const | both_empty | unparseable
  gemini_name        what Gemini extracted for name (may be empty)
  gemini_const       what Gemini extracted for constituency
  gemini_party       what Gemini extracted for party
  manifest_name      what the ECI listing crawl recorded (authoritative)
  manifest_const     ditto
  manifest_party     ditto
  manifest_aff_id    the affidavit_id from manifest (unique per candidate)
  ocr_char_count     total non-whitespace chars in Cloud Vision text
  ocr_sample         first 300 chars of OCR (to see if content is present)

Usage:
    python scripts/list_bad_extractions_all_states.py
    python scripts/list_bad_extractions_all_states.py --cycles kerala-2026 uttarpradesh-2022
"""
from __future__ import annotations
import argparse
import csv
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
RAW_ROOT = ROOT / "data" / "eci" / "raw_pdfs"
ALLOW_DIR = ROOT / "data" / "allowlists"
PP_ROOT = ROOT / "data" / "eci" / "for_ai"
LX_ROOT = ROOT / "data" / "eci" / "for_ai" / "llm_extracted"
OUT_CSV = ROOT / "data" / "reports" / "bad_extractions_all_states.csv"


def find_allowlist_path(slug_year: str) -> Path | None:
    p = ALLOW_DIR / f"{slug_year}.txt"
    if p.exists():
        return p
    matches = sorted(ALLOW_DIR.glob(f"{slug_year}_top*.txt"))
    return matches[0] if matches else None


def load_manifest_by_basename(cycle_dir: Path) -> dict[str, dict]:
    idx = {}
    mf = cycle_dir / "manifest.jsonl"
    if not mf.exists():
        return idx
    for line in mf.read_text().splitlines():
        try:
            r = json.loads(line)
        except Exception:
            continue
        p = r.get("pdf_path") or ""
        if p:
            idx[Path(p).name] = r
    return idx


def read_ocr_sample(pp_dir: Path, stem: str) -> tuple[int, str]:
    """Return (total_non_ws_chars, first_300_chars_of_text)."""
    p = pp_dir / (stem + ".json")
    if not p.exists():
        return (0, "")
    try:
        data = json.loads(p.read_text())
    except Exception:
        return (0, "<unreadable OCR json>")
    text = "\n".join(page.get("text", "") for page in data.get("pages", []))
    non_ws = len("".join(text.split()))
    return (non_ws, text[:300].replace("\n", " "))


def classify_extraction(ext: dict) -> tuple[str, str, str, str]:
    """Return (failure_type, name, const, party). Empty failure_type means
    the extraction is good."""
    if "_raw" in ext:
        return ("raw", "", "", "")
    name  = (ext.get("identity")  or {}).get("name_in_english") or ""
    const = (ext.get("political") or {}).get("constituency_name") or ""
    party = (ext.get("political") or {}).get("party_name") or ""
    n_empty = not name.strip()
    c_empty = not const.strip()
    if n_empty and c_empty:
        return ("both_empty", name, const, party)
    if n_empty:
        return ("empty_name", name, const, party)
    if c_empty:
        return ("empty_const", name, const, party)
    return ("", name, const, party)


def scan_cycle(cycle_dir: Path) -> list[dict]:
    cycle_name = cycle_dir.name
    slug_year = cycle_name.replace("-", "_")
    lx_dir = LX_ROOT / slug_year
    pp_dir = PP_ROOT / f"preprocessed_{slug_year}"
    allow_path = find_allowlist_path(slug_year)
    if not allow_path or not lx_dir.exists():
        return []
    allow_stems = {ln.strip()[:-4] for ln in allow_path.read_text().splitlines()
                   if ln.strip().endswith(".pdf")}
    mf = load_manifest_by_basename(cycle_dir)

    rows = []
    for f in sorted(lx_dir.glob("*.json")):
        if f.name.startswith("_"):
            continue
        stem = f.stem
        if stem not in allow_stems:
            continue
        try:
            r = json.loads(f.read_text())
        except Exception:
            failure = "unparseable"
            g_name = g_const = g_party = ""
        else:
            ext = r.get("extraction") or {}
            failure, g_name, g_const, g_party = classify_extraction(ext)
            if not failure:
                continue
        mfrow = mf.get(stem + ".pdf", {})
        ocr_len, ocr_sample = read_ocr_sample(pp_dir, stem)
        rows.append({
            "cycle":          cycle_name,
            "basename":       stem + ".pdf",
            "failure_type":   failure,
            "gemini_name":    g_name,
            "gemini_const":   g_const,
            "gemini_party":   g_party,
            "manifest_name":  mfrow.get("name", ""),
            "manifest_const": mfrow.get("constituency", ""),
            "manifest_party": mfrow.get("party", ""),
            "manifest_aff_id": mfrow.get("affidavit_id", ""),
            "ocr_char_count": ocr_len,
            "ocr_sample":     ocr_sample,
        })
    return rows


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                  formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--cycles", nargs="*", default=[])
    args = ap.parse_args()

    cycle_dirs = sorted(d for d in RAW_ROOT.iterdir()
                        if d.is_dir() and (d / "manifest.jsonl").exists())
    if args.cycles:
        wanted = set(args.cycles)
        cycle_dirs = [d for d in cycle_dirs if d.name in wanted]

    all_rows = []
    for cd in cycle_dirs:
        rows = scan_cycle(cd)
        if rows:
            print(f"  {cd.name:<32s}  {len(rows):>4d} bad", file=sys.stderr)
        all_rows.extend(rows)

    print(f"\nTotal bad extractions in allowlist scope: {len(all_rows)}",
          file=sys.stderr)

    # Failure-type breakdown
    from collections import Counter
    by_type = Counter(r["failure_type"] for r in all_rows)
    print("\nBy failure type:", file=sys.stderr)
    for ft, n in by_type.most_common():
        print(f"  {n:>4d}  {ft}", file=sys.stderr)

    # Also break out failure_type by cycle so we can see patterns per state
    print("\nPer cycle × failure_type:", file=sys.stderr)
    by_cycle_type: dict[tuple[str, str], int] = {}
    for r in all_rows:
        by_cycle_type[(r["cycle"], r["failure_type"])] = \
            by_cycle_type.get((r["cycle"], r["failure_type"]), 0) + 1
    cycles_sorted = sorted({c for c, _ in by_cycle_type})
    types_sorted = sorted({t for _, t in by_cycle_type})
    hdr = f"  {'cycle':<32s}  " + "  ".join(f"{t:>12s}" for t in types_sorted)
    print(hdr, file=sys.stderr)
    for c in cycles_sorted:
        row = f"  {c:<32s}  " + "  ".join(
            f"{by_cycle_type.get((c, t), 0):>12d}" for t in types_sorted)
        print(row, file=sys.stderr)

    OUT_CSV.parent.mkdir(parents=True, exist_ok=True)
    with OUT_CSV.open("w", newline="", encoding="utf-8") as f:
        if all_rows:
            w = csv.DictWriter(f, fieldnames=list(all_rows[0].keys()))
            w.writeheader()
            w.writerows(all_rows)
    print(f"\nCSV: {OUT_CSV.relative_to(ROOT)}", file=sys.stderr)


if __name__ == "__main__":
    main()
