"""
Cross-check ECI results JSONs against downloaded affidavit manifests to
find winners / runners-up whose affidavit PDFs were NOT captured (usually
because ECI's server-side pagination capped the query at 2000 rows).

Runs across every state under `data/eci/results/*.json` that has a
matching manifest at `data/eci/raw_pdfs/<state>-<year>/manifest.jsonl`.

Usage:
    python scripts/audit_manifest_coverage.py                # all states
    python scripts/audit_manifest_coverage.py --state madhyapradesh  # one state
    python scripts/audit_manifest_coverage.py --detail       # list missing names

Output: a table showing per-state
    - total winners
    - winners with affidavit downloaded
    - missing winners
    - same for runners-up
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent


def _norm(name: str) -> str:
    """Aggressive normalization to make fuzzy comparison work across the
    ECI Statistical Report Excel format and the affidavit portal manifest
    (which use different capitalization + honorifics)."""
    if not name:
        return ""
    s = name.upper().strip()
    # Strip honorifics
    for prefix in ("DR.", "DR", "SHRI", "SMT.", "SMT", "SHRIMATI",
                    "MR.", "MR", "MRS.", "MRS", "MS.", "MS",
                    "ADV.", "ADV", "ADVOCATE", "PROF.", "PROF",
                    "PT.", "PT", "PANDIT"):
        if s.startswith(prefix + " "):
            s = s[len(prefix)+1:]
    # Strip trailing initials like "P."
    s = re.sub(r"\s+[A-Z]\.\s*$", "", s)
    # Strip parens like "(vakeel Saab)"
    s = re.sub(r"\([^)]*\)", "", s)
    # Remove all non-alpha to normalize spacing/punctuation
    s = re.sub(r"[^A-Z]+", "", s)
    return s


def _load_results(path: Path) -> tuple[list[dict], list[dict]]:
    """Return (winners, runners_up) lists of {name, constituency} from the
    ECI results JSON. Handles both the fetcher schema (nested) and the
    Wikipedia schema (flat)."""
    raw = json.loads(path.read_text())
    winners: list[dict] = []
    runners: list[dict] = []

    if isinstance(raw, dict) and "constituencies" in raw:
        # ECI results / Excel schema
        for c in raw["constituencies"]:
            cname = c.get("name", "")
            for cand in c.get("candidates", []):
                if cand.get("name", "").strip().upper() == "NOTA":
                    continue
                rec = {"name": cand["name"], "constituency": cname,
                        "party": cand.get("party", "")}
                if cand.get("won"):
                    winners.append(rec)
                elif cand.get("rank") == 2:
                    runners.append(rec)
    elif isinstance(raw, list):
        # Wikipedia schema
        for row in raw:
            cname = row.get("constituency_raw") or row.get("constituency_norm", "")
            for cand in row.get("candidates", []):
                if cand.get("name", "").strip().upper() == "NOTA":
                    continue
                rec = {"name": cand["name"], "constituency": cname,
                        "party": cand.get("party_raw", "")}
                if cand.get("is_winner") or cand.get("rank") == 1:
                    winners.append(rec)
                elif cand.get("rank") == 2:
                    runners.append(rec)
    return winners, runners


def _load_manifest(path: Path) -> set[str]:
    """Return the set of normalized candidate names from the manifest."""
    names: set[str] = set()
    for line in path.read_text().splitlines():
        try:
            r = json.loads(line)
        except json.JSONDecodeError:
            continue
        if r.get("name"):
            names.add(_norm(r["name"]))
    return names


def _find_matches(
    people: list[dict],
    manifest_names: set[str],
) -> tuple[int, list[dict]]:
    """Return (matched_count, missing_records). A match is when the
    normalized candidate name is a substring of any manifest name, or
    vice versa — this tolerates the ECI affidavit portal sometimes
    including father/husband names in the field."""
    matched = 0
    missing: list[dict] = []
    for p in people:
        norm = _norm(p["name"])
        if not norm:
            missing.append(p)
            continue
        # Exact
        if norm in manifest_names:
            matched += 1
            continue
        # Substring either direction (handles "RAM SINGH" vs "RAM SINGH YADAV")
        if any(norm in m or m in norm for m in manifest_names if len(m) >= 4):
            matched += 1
        else:
            missing.append(p)
    return matched, missing


def _discover_states(target: str = "") -> list[tuple[str, Path, Path]]:
    """Yield (label, results_json_path, manifest_path) for every state
    where both files exist. `label` is like 'madhyapradesh-2023'.
    """
    out: list[tuple[str, Path, Path]] = []
    results_dir = ROOT / "data" / "eci" / "results"
    raw_pdfs = ROOT / "data" / "eci" / "raw_pdfs"
    for json_path in sorted(results_dir.glob("*.json")):
        # Extract label — {state}_{year}[_eci]_results.json → {state}-{year}
        stem = json_path.stem
        m = re.match(r"^([a-z]+)_(\d{4})(_eci)?_results$", stem)
        if not m:
            continue
        state_lc, year = m.group(1), m.group(2)
        label = f"{state_lc}-{year}"
        if target and target not in (state_lc, label):
            continue
        manifest_path = raw_pdfs / label / "manifest.jsonl"
        if not manifest_path.exists():
            continue
        out.append((label, json_path, manifest_path))
    return out


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                  formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--state", default="",
                    help="Only audit this state (e.g. 'madhyapradesh')")
    ap.add_argument("--detail", action="store_true",
                    help="Print full list of missing candidates per state")
    args = ap.parse_args()

    states = _discover_states(args.state)
    if not states:
        sys.exit("No state pairs found under data/eci/results + data/eci/raw_pdfs.")

    print(f"{'State':22s}  {'Wins':>4s}  {'Miss':>4s}  {'Cov%':>5s} | "
          f"{'RU':>4s}  {'Miss':>4s}  {'Cov%':>5s}  {'Manifest':>9s}")
    print("-" * 88)

    total_win_miss = 0
    total_ru_miss = 0
    detail_by_state: dict[str, list[dict]] = {}

    for label, results_path, manifest_path in states:
        winners, runners = _load_results(results_path)
        manifest_names = _load_manifest(manifest_path)
        wm, w_missing = _find_matches(winners, manifest_names)
        rm, r_missing = _find_matches(runners, manifest_names)

        w_cov = 100 * wm / len(winners) if winners else 0
        r_cov = 100 * rm / len(runners) if runners else 0
        w_miss_count = len(winners) - wm
        r_miss_count = len(runners) - rm
        total_win_miss += w_miss_count
        total_ru_miss += r_miss_count

        print(f"{label:22s}  {len(winners):>4d}  {w_miss_count:>4d}  "
              f"{w_cov:>4.1f}% | {len(runners):>4d}  {r_miss_count:>4d}  "
              f"{r_cov:>4.1f}%  {len(manifest_names):>9d}")

        if args.detail:
            detail_by_state[label] = w_missing + r_missing

    print("-" * 88)
    print(f"{'TOTAL':22s}  {'':>4s}  {total_win_miss:>4d}  {'':>5s} | "
          f"{'':>4s}  {total_ru_miss:>4d}")

    if args.detail:
        print("\n=== Detail: missing candidates by state ===")
        for st, misses in detail_by_state.items():
            if not misses:
                continue
            print(f"\n{st}:")
            for m in misses[:30]:
                print(f"  {m['constituency']:30s} | {m['party']:8s} | {m['name']}")
            if len(misses) > 30:
                print(f"  ... and {len(misses) - 30} more")


if __name__ == "__main__":
    main()
