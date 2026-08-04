"""
Populate election_appearances.won / votes_received / vote_share_pct
from the ECI Statistical Report JSONs (data/eci/results/*_eci_results.json).

INPUT
-----
    data/eci/results/{state_slug}_{year}_eci_results.json

Each JSON has the shape:
    {
      "state": "Kerala", "year": 2021,
      "constituencies": [
        {"number": 1, "name": "RATABARI",
         "candidates": [
           {"name": "BIJOY MALAKAR", "party": "BJP", "rank": 1,
            "won": true, "total_votes": 84711, "vote_pct": 61.92, ...},
           ...
         ]}
      ]
    }

MATCHING (STRICT)
-----------------
Constituency:  exact name match after `_norm_const()` normalization.
Candidate:     exact name match after `_norm_name()` normalization.

Any row we can't strict-match is logged to
`data/reports/winner_populate_unmatched.csv` for later fuzzy review.

USAGE
-----
    # Populate every cycle that has a *_eci_results.json on disk
    python scripts/populate_winners_from_eci_results.py

    # One cycle
    python scripts/populate_winners_from_eci_results.py --cycles kerala_2021

    # Dry run — show what would change, don't write
    python scripts/populate_winners_from_eci_results.py --dry-run
"""
from __future__ import annotations

import argparse
import csv
import json
import re
import sqlite3
import sys
from pathlib import Path
from collections import defaultdict

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DB_DEFAULT   = PROJECT_ROOT / "lokvani.db"
RESULTS_DIR  = PROJECT_ROOT / "data/eci/results"
UNMATCHED_CSV = PROJECT_ROOT / "data/reports/winner_populate_unmatched.csv"

# state slug (used in filenames) → canonical state name (states.name in DB)
STATE_SLUG_TO_NAME = {
    "andhrapradesh":    "Andhra Pradesh",
    "arunachal":        "Arunachal Pradesh",
    "arunachalpradesh": "Arunachal Pradesh",
    "assam":            "Assam",
    "bihar":            "Bihar",
    "chhattisgarh":     "Chhattisgarh",
    "delhi":            "Delhi",
    "goa":              "Goa",
    "gujarat":          "Gujarat",
    "haryana":          "Haryana",
    "himachal":         "Himachal Pradesh",
    "himachalpradesh":  "Himachal Pradesh",
    "jammuandkashmir":  "Jammu and Kashmir",
    "jk":               "Jammu and Kashmir",
    "jharkhand":        "Jharkhand",
    "karnataka":        "Karnataka",
    "kerala":           "Kerala",
    "madhyapradesh":    "Madhya Pradesh",
    "maharashtra":      "Maharashtra",
    "manipur":          "Manipur",
    "meghalaya":        "Meghalaya",
    "mizoram":          "Mizoram",
    "nagaland":         "Nagaland",
    "odisha":           "Odisha",
    "puducherry":       "Puducherry",
    "punjab":           "Punjab",
    "rajasthan":        "Rajasthan",
    "sikkim":           "Sikkim",
    "tamilnadu":        "Tamil Nadu",
    "telangana":        "Telangana",
    "tripura":          "Tripura",
    "uttarpradesh":     "Uttar Pradesh",
    "uttarakhand":      "Uttarakhand",
    "westbengal":       "West Bengal",
}


# ---------------------------------------------------------------------------
# Normalization
# ---------------------------------------------------------------------------

_HONORIFICS = (
    "DR. ", "DR ", "ADV. ", "ADV ", "ADVOCATE ", "SHRI ", "SHRIMATI ",
    "SMT. ", "SMT ", "MR. ", "MR ", "MS. ", "MS ", "MRS. ", "MRS ",
    "PROF. ", "PROF ", "CH. ", "CH ", "CHAUDHARY ", "PANDIT ", "PT. ",
    "PT ", "S. ", "SARDAR ", "BIBI ", "REV. ", "REV ",
)


# Party symbol descriptions that OCR sometimes appends to candidate names
# in the ECI Statistical Report PDFs. Everything from the first symbol
# token onwards is dropped. Order matters — longer phrases first so we
# match "HAMMER AND SICKLE" before "HAMMER".
_SYMBOL_TOKENS = [
    # Multi-word first
    "HAMMER SICKLE AND STAR", "HAMMER AND SICKLE",
    "AND SICKLE", "AND STAR",
    # Single word
    "HAMMER", "SICKLE", "STAR", "FLOWER", "LOTUS", "RICKSHAW", "BALL",
    "VOTES", "SYMBOL", "COTTON", "SCISSORS", "SAFFRON", "BROOM",
    "LANTERN", "ARROW", "ELEPHANT", "COW", "CALF", "BICYCLE", "CAR",
    "LADDER", "PANEL",
]


def _strip_symbol_suffix(s: str) -> str:
    """Drop trailing party-symbol tokens the ECI PDF OCR appended.
    Only strips when the token appears as its own word at the tail."""
    parts = s.split()
    changed = True
    while changed and parts:
        changed = False
        tail = " ".join(parts).strip()
        for tok in _SYMBOL_TOKENS:
            if tail.endswith(" " + tok) or tail == tok:
                parts = tail[: len(tail) - len(tok)].strip().split()
                changed = True
                break
    return " ".join(parts)


def _norm_name(name: str) -> str:
    """Uppercase, strip honorifics, symbol suffixes, punctuation.
    Same rules as build_top_n_allowlist.py's `_normalize_name`."""
    if not name:
        return ""
    s = name.upper().strip()
    for prefix in _HONORIFICS:
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
    s = re.sub(r"[^A-Z0-9]+", " ", s).strip()
    # Party-symbol suffix stripping happens LAST so we don't leave dangling
    # trailing junk after honorific/S-O extraction.
    s = _strip_symbol_suffix(s)
    return s


def _norm_const(name: str) -> str:
    """Strip category suffixes and punctuation. Loops to catch
    double-suffix cases like 'GANNAVARAM(SC) (SC)'."""
    if not name:
        return ""
    s = name.upper().strip()
    changed = True
    while changed:
        changed = False
        for suf in ("(SC)", "(ST)", "(BL)", " SC", " ST", " BL"):
            if s.endswith(suf):
                s = s[: -len(suf)].strip()
                changed = True
                break
    s = re.sub(r"[^A-Z0-9]+", "", s)
    return s


# ---------------------------------------------------------------------------
# Cycle discovery
# ---------------------------------------------------------------------------

def _list_available_cycles() -> list[tuple[str, int, Path]]:
    """Return [(state_name, year, json_path), ...] for every results file.

    Prefers `*_eci_results.json` (from the ECI Statistical Report PDF —
    authoritative for votes/vote_pct/rank) when available; falls back to
    `*_results.json` (Wikipedia-scraped, has is_winner + vote counts) so
    the 20 cycles without ECI JSONs still get won/votes populated."""
    by_cycle: dict[tuple[str, int], Path] = {}

    def _register(pattern: str, tag: str):
        for p in RESULTS_DIR.glob(pattern):
            stem = p.stem.replace(tag, "")
            parts = stem.rsplit("_", 1)
            if len(parts) != 2 or not parts[1].isdigit():
                continue
            slug, year = parts[0], int(parts[1])
            state = STATE_SLUG_TO_NAME.get(slug)
            if not state:
                continue
            key = (state, year)
            if key not in by_cycle:  # first wins (ECI preferred)
                by_cycle[key] = p

    _register("*_eci_results.json", "_eci_results")
    _register("*_results.json", "_results")

    out = [(s, y, p) for (s, y), p in by_cycle.items()]
    return sorted(out, key=lambda t: (t[0], t[1]))


def _parse_cycle_arg(s: str) -> tuple[str, int] | None:
    """Parse 'kerala_2021' → ('Kerala', 2021)."""
    parts = s.rsplit("_", 1)
    if len(parts) != 2 or not parts[1].isdigit():
        return None
    slug, year = parts[0], int(parts[1])
    state = STATE_SLUG_TO_NAME.get(slug)
    if not state:
        return None
    return (state, year)


# ---------------------------------------------------------------------------
# Per-cycle apply
# ---------------------------------------------------------------------------

def _normalize_results_schema(raw) -> list[dict]:
    """Return list[{"name": constituency, "candidates": [{name, party,
    won: bool, votes: int|None, vote_pct: float|None}]}] regardless of
    whether the input was an ECI Statistical Report JSON (dict with
    "constituencies" key, per-candidate `won`/`total_votes`/`vote_pct`)
    or a Wikipedia-scraped JSON (list of constituencies, per-candidate
    `is_winner`/`votes`/`vote_share_pct`)."""
    out = []
    if isinstance(raw, dict) and "constituencies" in raw:
        for cst in raw["constituencies"]:
            out.append({
                "name": cst.get("name", ""),
                "candidates": [
                    {
                        "name":     cd.get("name", ""),
                        "party":    cd.get("party", ""),
                        "won":      bool(cd.get("won") or cd.get("rank") == 1),
                        "votes":    cd.get("total_votes"),
                        "vote_pct": cd.get("vote_pct"),
                    }
                    for cd in cst.get("candidates", [])
                    if cd.get("name", "").strip().upper() != "NOTA"
                ],
            })
    elif isinstance(raw, list):
        for cst in raw:
            out.append({
                "name": cst.get("constituency_raw") or cst.get("constituency_norm", ""),
                "candidates": [
                    {
                        "name":     cd.get("name", ""),
                        "party":    cd.get("party", ""),
                        "won":      bool(cd.get("is_winner") or cd.get("rank") == 1),
                        "votes":    cd.get("votes"),
                        "vote_pct": cd.get("vote_share_pct"),
                    }
                    for cd in cst.get("candidates", [])
                    if cd.get("name", "").strip().upper() != "NOTA"
                ],
            })
    return out


def apply_cycle(cur, state: str, year: int, json_path: Path,
                dry_run: bool, unmatched_rows: list) -> tuple[int, int, int]:
    """Returns (updated, unmatched_cons, unmatched_cand)."""
    raw = json.loads(json_path.read_text())
    constituencies = _normalize_results_schema(raw)

    # Load election_id (state, year, house=Assembly)
    row = cur.execute("""
        SELECT e.id FROM elections e
        JOIN states s ON s.id = e.state_id
        WHERE s.name = ? AND e.year = ?
    """, (state, year)).fetchone()
    if not row:
        print(f"  ⚠ No elections row for {state} {year} — skipping",
              file=sys.stderr)
        return (0, 0, 0)
    election_id = row[0]

    # Load {constituency_norm: constituency_id} for this state
    const_map = {}
    for cid, cname in cur.execute("""
        SELECT co.id, co.name FROM constituencies co
        JOIN states s ON s.id = co.state_id
        WHERE s.name = ?
    """, (state,)):
        const_map[_norm_const(cname)] = cid

    # Load {(constituency_id, candidate_name_norm): [appearance_id, ...]} for this election
    app_by_key = defaultdict(list)
    for aid, cid, pname in cur.execute("""
        SELECT ea.id, ea.constituency_id, p.name
        FROM election_appearances ea
        JOIN politicians p ON p.id = ea.politician_id
        WHERE ea.election_id = ?
    """, (election_id,)):
        app_by_key[(cid, _norm_name(pname))].append(aid)

    updated = 0
    unmatched_cons = 0
    unmatched_cand = 0
    for cst in constituencies:
        cname = cst["name"]
        cnorm = _norm_const(cname)
        cid = const_map.get(cnorm)
        if cid is None:
            unmatched_cons += 1
            for cand in cst["candidates"]:
                unmatched_rows.append({
                    "state": state, "year": year,
                    "constituency_json": cname,
                    "constituency_norm": cnorm,
                    "candidate": cand["name"],
                    "party": cand["party"],
                    "reason": "constituency_not_in_db",
                })
            continue

        for cand in cst["candidates"]:
            cand_name = cand["name"]
            nname = _norm_name(cand_name)
            aids = app_by_key.get((cid, nname), [])

            if len(aids) == 0:
                unmatched_cand += 1
                unmatched_rows.append({
                    "state": state, "year": year,
                    "constituency_json": cname,
                    "constituency_norm": cnorm,
                    "candidate": cand_name,
                    "party": cand["party"],
                    "reason": "candidate_not_found_in_appearances",
                })
                continue
            if len(aids) > 1:
                # Multiple candidates with same normalized name in same
                # constituency — strict mode punts to unmatched.
                unmatched_cand += 1
                unmatched_rows.append({
                    "state": state, "year": year,
                    "constituency_json": cname,
                    "constituency_norm": cnorm,
                    "candidate": cand_name,
                    "party": cand["party"],
                    "reason": f"ambiguous_{len(aids)}_matches",
                })
                continue

            aid = aids[0]
            won = 1 if cand["won"] else 0
            if not dry_run:
                cur.execute("""
                    UPDATE election_appearances
                    SET won = ?, votes_received = ?, vote_share_pct = ?
                    WHERE id = ?
                """, (won, cand["votes"], cand["vote_pct"], aid))
            updated += 1

    return (updated, unmatched_cons, unmatched_cand)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                  formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db", default=str(DB_DEFAULT))
    ap.add_argument("--cycles", nargs="*",
                    help="Restrict to these cycles (e.g. kerala_2021 delhi_2025). "
                         "Default: every cycle with a JSON on disk.")
    ap.add_argument("--dry-run", action="store_true",
                    help="Compute matches, don't write. Still emits the "
                         "unmatched CSV.")
    args = ap.parse_args()

    available = _list_available_cycles()
    if args.cycles:
        wanted = set()
        for c in args.cycles:
            parsed = _parse_cycle_arg(c)
            if parsed:
                wanted.add(parsed)
            else:
                print(f"  ⚠ Unknown cycle {c!r} — skipping", file=sys.stderr)
        cycles = [(s, y, p) for s, y, p in available if (s, y) in wanted]
    else:
        cycles = available

    if not cycles:
        sys.exit("No cycles to process. Available: " +
                 ", ".join(f"{s} {y}" for s, y, _ in available))

    con = sqlite3.connect(args.db)
    cur = con.cursor()

    unmatched_rows = []
    total_updated = 0
    total_uc = 0
    total_ucand = 0

    for state, year, path in cycles:
        print(f"\n→ {state} {year}  ({path.name})", file=sys.stderr)
        upd, uc, ucand = apply_cycle(cur, state, year, path,
                                     args.dry_run, unmatched_rows)
        print(f"    updated: {upd}   unmatched constituencies: {uc}   "
              f"unmatched candidates: {ucand}", file=sys.stderr)
        total_updated += upd
        total_uc += uc
        total_ucand += ucand

    if args.dry_run:
        print("\n(dry run — no writes)", file=sys.stderr)
    else:
        con.commit()

    con.close()

    # Write unmatched CSV
    if unmatched_rows:
        UNMATCHED_CSV.parent.mkdir(parents=True, exist_ok=True)
        with UNMATCHED_CSV.open("w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=[
                "state", "year", "constituency_json", "constituency_norm",
                "candidate", "party", "reason",
            ])
            w.writeheader()
            w.writerows(unmatched_rows)
        print(f"\nUnmatched rows written to {UNMATCHED_CSV}", file=sys.stderr)

    print(f"\n========== SUMMARY ==========", file=sys.stderr)
    print(f"  cycles processed         : {len(cycles)}", file=sys.stderr)
    print(f"  election_appearances updated : {total_updated}", file=sys.stderr)
    print(f"  unmatched constituencies : {total_uc}", file=sys.stderr)
    print(f"  unmatched candidates     : {total_ucand}", file=sys.stderr)


if __name__ == "__main__":
    main()
