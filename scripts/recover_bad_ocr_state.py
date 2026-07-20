"""Recover bad Gemini extractions caused by OCR-without-language-hint.

For a given state cycle:
  1. Identify allowlist candidates whose current Gemini extraction is
     "bad" (raw / empty name / empty const / both empty).
  2. Delete their Cloud Vision + Gemini JSONs (source PDF preserved).
  3. Write a mini-allowlist so the next cloud_vision_preprocess and
     llm_extract_via_gemini runs only touch these files.

Then run:
    python scripts/cloud_vision_preprocess.py \\
      --pdf-dir data/eci/raw_pdfs/<cycle>/raw_pdfs \\
      --out-dir data/eci/for_ai/preprocessed_<slug_year> \\
      --pdf-allowlist /tmp/recover_<slug_year>.txt

    python scripts/llm_extract_via_gemini.py \\
      --in-dir data/eci/for_ai/preprocessed_<slug_year> \\
      --out-dir data/eci/for_ai/llm_extracted/<slug_year> \\
      --state "<State>" --year <year>

Dry-run by default. --commit deletes the JSONs.

Usage:
    python scripts/recover_bad_ocr_state.py --state "Uttar Pradesh" --year 2022
    python scripts/recover_bad_ocr_state.py --state "Uttar Pradesh" --year 2022 --commit
"""
from __future__ import annotations
import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                  formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--state", required=True)
    ap.add_argument("--year", type=int, required=True)
    ap.add_argument("--commit", action="store_true",
                    help="Actually delete bad JSONs. Default is dry-run.")
    ap.add_argument("--mini-allowlist", default="",
                    help="Path to write the mini-allowlist. Defaults to "
                         "/tmp/recover_<slug>_<year>.txt")
    args = ap.parse_args()

    slug_year = f"{args.state.lower().replace(' ', '')}_{args.year}"
    lx = ROOT / "data/eci/for_ai/llm_extracted" / slug_year
    pp = ROOT / "data/eci/for_ai" / f"preprocessed_{slug_year}"

    # Locate allowlist (canonical or top-N)
    allow_dir = ROOT / "data/allowlists"
    allow_path = allow_dir / f"{slug_year}.txt"
    if not allow_path.exists():
        matches = sorted(allow_dir.glob(f"{slug_year}_top*.txt"))
        if not matches:
            sys.exit(f"No allowlist for {slug_year}")
        allow_path = matches[0]
    allow_stems = {ln.strip()[:-4]
                   for ln in allow_path.read_text().splitlines()
                   if ln.strip().endswith(".pdf")}
    print(f"Allowlist: {allow_path.name} ({len(allow_stems)} candidates)",
          file=sys.stderr)

    if not lx.exists():
        sys.exit(f"No extractions dir: {lx.relative_to(ROOT)}")

    bad_stems: list[str] = []
    for f in sorted(lx.glob("*.json")):
        if f.name.startswith("_") or f.stem not in allow_stems:
            continue
        try:
            r = json.loads(f.read_text())
        except Exception:
            bad_stems.append(f.stem)
            continue
        ext = r.get("extraction") or {}
        if "_raw" in ext:
            bad_stems.append(f.stem)
            continue
        name  = (ext.get("identity")  or {}).get("name_in_english") or ""
        const = (ext.get("political") or {}).get("constituency_name") or ""
        if not name.strip() or not const.strip():
            bad_stems.append(f.stem)

    print(f"Bad extractions to recover: {len(bad_stems)}", file=sys.stderr)
    if not bad_stems:
        return

    # Show samples
    for s in bad_stems[:5]:
        print(f"  • {s}", file=sys.stderr)
    if len(bad_stems) > 5:
        print(f"  … + {len(bad_stems) - 5} more", file=sys.stderr)

    mini_path = Path(args.mini_allowlist) if args.mini_allowlist \
        else Path(f"/tmp/recover_{slug_year}.txt")

    if not args.commit:
        print(f"\n--- DRY RUN. Would delete {2 * len(bad_stems)} files "
              f"(PP + LX for {len(bad_stems)} stems) and write "
              f"{mini_path}. Re-run with --commit.", file=sys.stderr)
        return

    # Delete PP + LX for each bad stem
    deleted_pp = deleted_lx = 0
    for stem in bad_stems:
        pp_f = pp / (stem + ".json")
        lx_f = lx / (stem + ".json")
        if pp_f.exists():
            pp_f.unlink()
            deleted_pp += 1
        if lx_f.exists():
            lx_f.unlink()
            deleted_lx += 1

    # Write mini-allowlist
    mini_path.write_text("\n".join(sorted(s + ".pdf" for s in bad_stems)) + "\n")

    print(f"\n✓ Deleted {deleted_pp} Cloud Vision + {deleted_lx} Gemini JSONs",
          file=sys.stderr)
    print(f"✓ Mini-allowlist written: {mini_path}", file=sys.stderr)
    print(f"\nNext (copy-paste):", file=sys.stderr)
    print(f"  source secrets/.env", file=sys.stderr)
    print(f"  python scripts/cloud_vision_preprocess.py \\", file=sys.stderr)
    print(f"    --pdf-dir data/eci/raw_pdfs/{args.state.lower().replace(' ','')}-{args.year}/raw_pdfs \\",
          file=sys.stderr)
    print(f"    --out-dir data/eci/for_ai/preprocessed_{slug_year} \\", file=sys.stderr)
    print(f"    --pdf-allowlist {mini_path}", file=sys.stderr)
    print(f"  python scripts/llm_extract_via_gemini.py \\", file=sys.stderr)
    print(f"    --in-dir data/eci/for_ai/preprocessed_{slug_year} \\", file=sys.stderr)
    print(f"    --out-dir data/eci/for_ai/llm_extracted/{slug_year} \\", file=sys.stderr)
    print(f"    --state \"{args.state}\" --year {args.year}", file=sys.stderr)


if __name__ == "__main__":
    main()
