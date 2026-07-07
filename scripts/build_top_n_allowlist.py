"""
Build a top-N-per-constituency allowlist of affidavit PDFs.

Purpose: cut OCR + Gemini costs by only processing the top-2 (or top-N)
candidates per constituency — winner + runner-up captures 90%+ of the
editorially interesting story. The other candidates' PDFs stay on disk
and can be reprocessed later if needed.

Workflow:
    1. Fetch Wikipedia's per-constituency results JSON (already produced
       by scripts/load_<state>_election_results.py)
    2. For each constituency, take the top-N by rank (default: 2 — winner
       + runner-up)
    3. Fuzzy-match Wikipedia names against candidates in the fetcher's
       manifest.jsonl (which has the exact PDF filenames)
    4. Emit an allowlist file: one PDF filename per line

Then pass the allowlist to cloud_vision_preprocess.py and
llm_extract_via_gemini.py via a new --allowlist flag.

Usage:
    # After you've run load_<state>_election_results.py to produce
    # the parsed JSON with winners + runners-up:
    python scripts/build_top_n_allowlist.py \\
        --results data/eci/results/haryana_2024_results.json \\
        --manifest data/eci/raw_pdfs/haryana-2024/manifest.jsonl \\
        --output data/eci/allowlists/haryana_2024_top2.txt \\
        --top-n 2
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path


def _normalize_name(name: str) -> str:
    """Match the normalization the state loaders use."""
    if not name:
        return ""
    s = name.upper().strip()
    for prefix in ("DR. ", "DR ", "ADV. ", "ADV ", "ADVOCATE ",
                    "SHRI ", "SHRIMATI ", "SMT. ", "SMT ",
                    "MR. ", "MR ", "MS. ", "MS ", "MRS. ", "MRS ",
                    "PROF. ", "PROF ", "CH. ", "CH ", "CHAUDHARY ",
                    "PANDIT ", "PT. ", "PT ", "S. ", "SARDAR ",
                    "BIBI ", "REV. ", "REV "):
        if s.startswith(prefix):
            s = s[len(prefix):]
    if "@" in s:
        s = s.split("@")[0].strip()
    for marker in (" S/O ", " D/O ", " W/O "):
        if marker in s:
            s = s.split(marker)[0]
    s = re.sub(r"\([^)]*\)", "", s)
    while True:
        m = re.match(r"^(.*?)\s+[A-Z]\.\s*$", s)
        if not m:
            break
        s = m.group(1).strip()
    while True:
        m = re.match(r"^[A-Z]\.\s+(.+)$", s)
        if not m:
            break
        s = m.group(1).strip()
    s = re.sub(r"[^A-Z0-9]+", " ", s)
    return s.strip()


def _normalize_constituency(name: str) -> str:
    if not name:
        return ""
    s = name.upper().strip()
    for suf in ("(SC)", "(ST)", "(BL)", " SC", " ST", " BL"):
        if s.endswith(suf):
            s = s[: -len(suf)].strip()
    s = re.sub(r"[^A-Z0-9]+", "", s)
    return s


def main():
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument("--results", required=True,
                    help="Path to <state>_<year>_results.json produced "
                         "by the state loader.")
    ap.add_argument("--manifest", required=True,
                    help="Path to manifest.jsonl produced by "
                         "fetch_eci_affidavits.py")
    ap.add_argument("--output", required=True,
                    help="Where to write the allowlist (one PDF filename "
                         "per line).")
    ap.add_argument("--top-n", type=int, default=2,
                    help="Number of top candidates per constituency to keep. "
                         "Default 2 (winner + runner-up).")
    ap.add_argument("--match-threshold", type=int, default=70,
                    help="Fuzzy match score threshold (0-100). Default 70.")
    ap.add_argument("--report", default="",
                    help="Optional path — write a per-constituency report of "
                         "how many candidates Wikipedia had vs how many we "
                         "kept. Useful for spotting under-filled seats.")
    args = ap.parse_args()

    try:
        from rapidfuzz import fuzz
    except ImportError:
        sys.exit("pip install rapidfuzz")

    # 1. Load Wikipedia results
    results_path = Path(args.results)
    if not results_path.exists():
        sys.exit(f"Results file not found: {results_path}")
    results = json.loads(results_path.read_text())
    if not results:
        sys.exit("Results JSON is empty. Did the state loader parse anything?")

    # Build wiki index: {const_norm: [{name, rank, votes}, ...]}
    wiki_by_const: dict[str, list[dict]] = {}
    total_wiki_cands = 0
    for row in results:
        const_norm = row.get("constituency_norm") or _normalize_constituency(
            row.get("constituency_raw", "")
        )
        for cand in row.get("candidates", []):
            wiki_by_const.setdefault(const_norm, []).append({
                "name":  cand.get("name", ""),
                "rank":  cand.get("rank", 999),
                "votes": cand.get("votes"),
            })
            total_wiki_cands += 1

    # Take top-N per constituency (by rank ASC — lowest rank = winner)
    top_n_by_const: dict[str, list[dict]] = {}
    for const_norm, cands in wiki_by_const.items():
        top_n_by_const[const_norm] = sorted(
            cands, key=lambda c: c.get("rank", 999)
        )[: args.top_n]

    print(f"Wikipedia results: {len(results)} constituencies · "
          f"{total_wiki_cands} candidates seen · "
          f"{sum(len(v) for v in top_n_by_const.values())} kept after top-{args.top_n} filter",
          file=sys.stderr)

    # 2. Load manifest — map (const, name) → pdf filename
    manifest_path = Path(args.manifest)
    if not manifest_path.exists():
        sys.exit(f"Manifest file not found: {manifest_path}")

    manifest_by_const: dict[str, list[dict]] = {}
    total_manifest_cands = 0
    for line in manifest_path.read_text().splitlines():
        try:
            r = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not r.get("download_succeeded"):
            continue
        pdf_path = r.get("pdf_path") or ""
        if not pdf_path:
            continue
        const = r.get("constituency", "")
        const_norm = _normalize_constituency(const)
        manifest_by_const.setdefault(const_norm, []).append({
            "name":        r.get("name", ""),
            "constituency": const,
            "party":       r.get("party", ""),
            "pdf_filename": Path(pdf_path).name,
        })
        total_manifest_cands += 1

    print(f"Manifest: {total_manifest_cands} downloaded PDFs across "
          f"{len(manifest_by_const)} constituencies", file=sys.stderr)

    # 3. Fuzzy-match top-N Wikipedia names against manifest candidates
    #    per constituency. Emit allowlist filenames.
    allowlist: set[str] = set()
    matched = 0
    unmatched: list[tuple] = []
    # Per-constituency accounting for --report
    per_const: dict[str, dict] = {}

    # Pre-compute all manifest constituency keys for fuzzy fallback
    manifest_const_keys = list(manifest_by_const.keys())

    def _fuzzy_constituency_lookup(wiki_key: str) -> str | None:
        """When a Wikipedia constituency has no exact match in the manifest
        (Wikipedia and ECI often disagree on 1-2 letters: 'Chawamanu' vs
        'Chawmanu', 'Pabiachhara' vs 'Pabiachara', 'Town Bordowali' vs
        'Town Bardowali'), try fuzzy match. Threshold 85 catches typical
        1-2 char spelling variations while avoiding false pairs."""
        best = None
        best_score = 0
        for mc_key in manifest_const_keys:
            score = max(
                fuzz.ratio(wiki_key, mc_key),
                fuzz.partial_ratio(wiki_key, mc_key),
            )
            if score > best_score:
                best_score = score
                best = mc_key
        if best and best_score >= 85:
            return best
        return None

    for const_norm, wiki_cands in top_n_by_const.items():
        manifest_cands = manifest_by_const.get(const_norm, [])

        # Fuzzy fallback: constituency name may have 1-2 letter variation
        # between Wikipedia and ECI portal.
        aliased_from = None
        if not manifest_cands:
            fuzzy_key = _fuzzy_constituency_lookup(const_norm)
            if fuzzy_key:
                manifest_cands = manifest_by_const[fuzzy_key]
                aliased_from = fuzzy_key
                print(f"  ⓘ constituency alias: Wiki {const_norm!r} → "
                      f"manifest {fuzzy_key!r} ({len(manifest_cands)} cands)",
                      file=sys.stderr)

        per_const[const_norm] = {
            "wiki_kept":       len(wiki_cands),
            "manifest_size":   len(manifest_cands),
            "matched":         0,
            "unmatched_names": [],
            "aliased_from":    aliased_from,
        }
        if not manifest_cands:
            for wc in wiki_cands:
                unmatched.append((const_norm, wc["name"],
                                   "no manifest candidates for constituency"))
                per_const[const_norm]["unmatched_names"].append(wc["name"])
            continue

        for wc in wiki_cands:
            wiki_norm = _normalize_name(wc["name"])
            if not wiki_norm:
                continue

            # Try exact match first
            best = None
            best_score = 0
            for mc in manifest_cands:
                m_norm = _normalize_name(mc["name"])
                if not m_norm:
                    continue
                score = max(
                    fuzz.partial_ratio(wiki_norm, m_norm),
                    fuzz.token_set_ratio(wiki_norm, m_norm),
                    fuzz.token_sort_ratio(wiki_norm, m_norm),
                )
                if score > best_score:
                    best_score = score
                    best = mc

            if best and best_score >= args.match_threshold:
                allowlist.add(best["pdf_filename"])
                matched += 1
                per_const[const_norm]["matched"] += 1
            else:
                unmatched.append((const_norm, wc["name"],
                                   f"best_score={best_score} for {best.get('name') if best else '(none)'!r}"))
                per_const[const_norm]["unmatched_names"].append(wc["name"])

    # 4. Write allowlist
    out_path = Path(args.output)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text("\n".join(sorted(allowlist)) + "\n")

    print()
    print(f"========== ALLOWLIST BUILD ==========")
    print(f"  Top-{args.top_n}-per-constituency filter")
    print(f"  Wikipedia candidates kept:        "
          f"{sum(len(v) for v in top_n_by_const.values())}")
    print(f"  Manifest PDFs matched + kept:     {matched}")
    print(f"  Unmatched Wikipedia names:        {len(unmatched)}")
    print(f"  Distinct PDFs in allowlist:       {len(allowlist)}")
    print(f"  Written to: {out_path}")

    if unmatched:
        print()
        print(f"Sample unmatched (first 10):")
        for const, name, reason in unmatched[:10]:
            print(f"  {const:15s} {name!r:40s}  {reason}")
        if len(unmatched) > 10:
            print(f"  ... and {len(unmatched) - 10} more")

    # 5. Optional: write per-constituency report
    if args.report:
        report_path = Path(args.report)
        report_path.parent.mkdir(parents=True, exist_ok=True)
        with report_path.open("w") as f:
            f.write("Per-constituency allowlist report\n")
            f.write(f"Top-N target: {args.top_n}\n")
            f.write(f"Total constituencies: {len(per_const)}\n\n")
            f.write(f"{'Constituency':30s} {'wiki':>5s} {'kept':>5s} "
                    f"{'gap':>5s} {'manifest':>10s}   "
                    f"Unmatched names\n")
            f.write("-" * 80 + "\n")

            # Sort: under-filled constituencies (gap > 0) first, then alpha
            def sort_key(item):
                const, d = item
                gap = args.top_n - d["matched"]
                return (-gap, const)

            for const, d in sorted(per_const.items(), key=sort_key):
                gap = args.top_n - d["matched"]
                unm = ", ".join(d["unmatched_names"]) if d["unmatched_names"] else ""
                f.write(f"{const:30s} {d['wiki_kept']:>5d} {d['matched']:>5d} "
                        f"{gap:>5d} {d['manifest_size']:>10d}   {unm}\n")

            # Summary counters
            under = sum(1 for d in per_const.values()
                        if args.top_n - d["matched"] > 0)
            full = sum(1 for d in per_const.values()
                       if d["matched"] >= args.top_n)
            zero = sum(1 for d in per_const.values() if d["matched"] == 0)

            f.write("\n")
            f.write(f"Summary:\n")
            f.write(f"  Full (matched >= top-{args.top_n}):  {full}\n")
            f.write(f"  Under-filled (matched < top-{args.top_n}):  {under}\n")
            f.write(f"    of which 0 matched (no data):        {zero}\n")

        print()
        print(f"Per-constituency report: {report_path}")
        print(f"  Full: {full}  ·  Under-filled: {under}  ·  Zero-match: {zero}")


if __name__ == "__main__":
    main()
