"""Recover 'partial' bad extractions by back-filling missing name /
constituency / party fields from the manifest.

An extraction is 'partial' when Gemini extracted OCR text and produced
structured output, but the identity.name_in_english or
political.constituency_name is missing / empty. We fill those specific
missing fields from the manifest — which has the authoritative
name/constituency/party from the ECI listing scrape.

SAFETY: this NEVER overwrites a non-empty Gemini value. If Gemini
extracted a DIFFERENT name/const from what the manifest says, we leave
it alone — that's a possible PDF-collision signal, not a fill target.

Adds these metadata fields to the extraction so the apply script + UI
can tell that data was back-filled rather than LLM-extracted:
  extraction.identity._name_source = "gemini" | "manifest_backfill"
  extraction.political._const_source = "gemini" | "manifest_backfill"

Dry-run by default.

Usage:
    python scripts/backfill_partial_from_manifest.py                    # all states
    python scripts/backfill_partial_from_manifest.py --state "Kerala" --year 2026
    python scripts/backfill_partial_from_manifest.py --commit
"""
from __future__ import annotations
import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def load_manifest(cycle_dir: Path) -> dict[str, dict]:
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


def find_allowlist(slug_year: str) -> Path | None:
    d = ROOT / "data/allowlists"
    p = d / f"{slug_year}.txt"
    if p.exists():
        return p
    matches = sorted(d.glob(f"{slug_year}_top*.txt"))
    return matches[0] if matches else None


def backfill_cycle(cycle_dir: Path, commit: bool) -> dict[str, int]:
    slug_year = cycle_dir.name.replace("-", "_")
    lx_dir = ROOT / "data/eci/for_ai/llm_extracted" / slug_year
    if not lx_dir.exists():
        return {"scanned": 0, "backfilled": 0, "skipped_conflict": 0}

    allow_path = find_allowlist(slug_year)
    if not allow_path:
        return {"scanned": 0, "backfilled": 0, "skipped_conflict": 0}
    allow_stems = {ln.strip()[:-4] for ln in allow_path.read_text().splitlines()
                   if ln.strip().endswith(".pdf")}

    mf_idx = load_manifest(cycle_dir)

    scanned = backfilled = conflicts = 0
    for f in sorted(lx_dir.glob("*.json")):
        if f.name.startswith("_") or f.stem not in allow_stems:
            continue
        try:
            r = json.loads(f.read_text())
        except Exception:
            continue
        ext = r.get("extraction") or {}
        if "_raw" in ext:
            continue  # raw = different fix
        ident = ext.setdefault("identity", {})
        pol   = ext.setdefault("political", {})
        g_name  = (ident.get("name_in_english") or "").strip()
        g_const = (pol.get("constituency_name") or "").strip()
        g_party = (pol.get("party_name") or "").strip()

        # Only care about partial cases — skip good extractions
        if g_name and g_const:
            continue
        scanned += 1

        mfrow = mf_idx.get(f.stem + ".pdf", {})
        m_name  = mfrow.get("name", "").strip()
        m_const = mfrow.get("constituency", "").strip()
        m_party = mfrow.get("party", "").strip()

        did_backfill = False

        # Name backfill — only if Gemini's value is empty
        if not g_name and m_name:
            ident["name_in_english"] = m_name
            ident["_name_source"] = "manifest_backfill"
            did_backfill = True

        # Constituency backfill — only if Gemini's value is empty
        if not g_const and m_const:
            pol["constituency_name"] = m_const
            pol["_const_source"] = "manifest_backfill"
            did_backfill = True

        # Party backfill (bonus) — same rule
        if not g_party and m_party:
            pol["party_name"] = m_party
            pol["_party_source"] = "manifest_backfill"

        if did_backfill:
            backfilled += 1
            if commit:
                # Write back with the backfilled fields
                r["extraction"] = ext
                f.write_text(json.dumps(r, indent=2, ensure_ascii=False))
        elif not m_name and not m_const:
            # Nothing to backfill from manifest — likely a collision case
            conflicts += 1

    return {"scanned": scanned, "backfilled": backfilled,
            "skipped_conflict": conflicts}


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                  formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--state", default="",
                    help="Restrict to one state (e.g. 'Kerala'). Default: all.")
    ap.add_argument("--year", type=int, default=0,
                    help="Restrict to one year (with --state).")
    ap.add_argument("--commit", action="store_true",
                    help="Write backfilled fields to disk. Default is dry-run.")
    args = ap.parse_args()

    raw_root = ROOT / "data/eci/raw_pdfs"
    cycle_dirs = sorted(d for d in raw_root.iterdir()
                        if d.is_dir() and (d / "manifest.jsonl").exists())

    if args.state:
        wanted_prefix = args.state.lower().replace(" ", "")
        year_suffix = str(args.year) if args.year else ""
        cycle_dirs = [d for d in cycle_dirs
                      if d.name.startswith(wanted_prefix)
                      and (not year_suffix or d.name.endswith(year_suffix))]

    print(f"{'Cycle':<32s}  {'Scanned':>7s}  {'Backfilled':>10s}  {'No manifest':>11s}")
    print("-" * 68)
    tot_s = tot_b = tot_c = 0
    for cd in cycle_dirs:
        r = backfill_cycle(cd, args.commit)
        if not r["scanned"]:
            continue
        print(f"{cd.name:<32s}  {r['scanned']:>7d}  {r['backfilled']:>10d}  "
              f"{r['skipped_conflict']:>11d}")
        tot_s += r["scanned"]
        tot_b += r["backfilled"]
        tot_c += r["skipped_conflict"]
    print("-" * 68)
    print(f"{'TOTAL':<32s}  {tot_s:>7d}  {tot_b:>10d}  {tot_c:>11d}")

    if not args.commit:
        print(f"\n--- DRY RUN. Re-run with --commit to apply back-fills. ---")


if __name__ == "__main__":
    main()
