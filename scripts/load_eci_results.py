"""
Load ECI-scraped election results into the DB.

Replaces the per-state Wikipedia loaders (load_<state>_election_results.py)
with a state-agnostic loader that reads structured JSON produced by
`scripts/fetch_eci_results.py`.

Advantages over the Wikipedia loaders:
    - Every candidate has vote counts (not just top-2).
    - No table-index / column-offset guesswork.
    - Winner determination is unambiguous (max votes wins).
    - Same code path handles every state.

Usage:
    python scripts/load_eci_results.py \\
        --results data/eci/results/westbengal_2026_eci_results.json

Matches candidates to `election_appearances` rows via:
    (state.name = STATE, election.year = YEAR)
    ∩ fuzzy-matched constituency name
    ∩ fuzzy-matched candidate name (rapidfuzz, threshold=75)

Updates set: won, votes_received, vote_share_pct.
"""
from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parent.parent
DB_PATH      = PROJECT_ROOT / "lokvani.db"


def _normalize_name(name: str) -> str:
    if not name:
        return ""
    s = name.upper().strip()
    for prefix in ("DR. ", "DR ", "ADV. ", "ADV ", "ADVOCATE ",
                    "SHRI ", "SHRIMATI ", "SMT. ", "SMT ",
                    "MR. ", "MR ", "MS. ", "MS ", "MRS. ", "MRS ",
                    "PROF. ", "PROF ", "PANDIT ", "PT. ", "PT "):
        if s.startswith(prefix):
            s = s[len(prefix):]
    if "@" in s:
        s = s.split("@")[0].strip()
    for marker in (" S/O ", " D/O ", " W/O "):
        if marker in s:
            s = s.split(marker)[0]
    s = re.sub(r"\([^)]*\)", "", s)
    s = re.sub(r"[^A-Z0-9]+", " ", s)
    return s.strip()


def _normalize_constituency(name: str) -> str:
    if not name:
        return ""
    s = name.upper().strip()
    for suf in ("(SC)", "(ST)", " SC", " ST"):
        if s.endswith(suf):
            s = s[: -len(suf)].strip()
    s = re.sub(r"[^A-Z0-9]+", "", s)
    return s


def build_appearance_lookup(cur, state_name: str, year: int) -> dict:
    """Return {(const_norm, cand_norm): appearance_id} for all rows we could touch."""
    cur.execute("""
        SELECT ea.id, p.name, c.name
        FROM election_appearances ea
        JOIN politicians p     ON ea.politician_id  = p.id
        JOIN constituencies c  ON ea.constituency_id = c.id
        JOIN elections e       ON ea.election_id    = e.id
        JOIN states s          ON e.state_id        = s.id
        WHERE s.name = ? AND e.year = ?
    """, (state_name, year))
    return {(_normalize_constituency(c), _normalize_name(n)): i
             for i, n, c in cur.fetchall()}


def fuzzy_match(candidate_name: str, const_norm: str,
                 lookup: dict, threshold: int = 75) -> int | None:
    """Find appearance_id in the given constituency whose name fuzzy-matches."""
    try:
        from rapidfuzz import fuzz
    except ImportError:
        sys.exit("pip install rapidfuzz")

    cand_norm = _normalize_name(candidate_name)
    if not cand_norm:
        return None

    # Exact match first
    hit = lookup.get((const_norm, cand_norm))
    if hit:
        return hit

    # Fuzzy fallback within the same constituency
    best_id, best_score = None, 0
    for (c, n), aid in lookup.items():
        if c != const_norm:
            continue
        s = max(fuzz.partial_ratio(cand_norm, n),
                fuzz.token_set_ratio(cand_norm, n),
                fuzz.token_sort_ratio(cand_norm, n))
        if s > best_score:
            best_score, best_id = s, aid
    return best_id if best_score >= threshold else None


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                  formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--results", required=True,
                    help="Path to ECI results JSON produced by fetch_eci_results.py")
    ap.add_argument("--db", default=str(DB_PATH))
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--match-threshold", type=int, default=75)
    args = ap.parse_args()

    if not Path(args.db).exists():
        sys.exit(f"DB not found: {args.db}")

    payload = json.loads(Path(args.results).read_text())
    state_name = payload["state"]
    year       = payload["year"]

    print(f"→ Applying ECI results for {state_name} {year} "
          f"({len(payload['constituencies'])} constituencies)", file=sys.stderr)

    con = sqlite3.connect(args.db)
    cur = con.cursor()

    lookup = build_appearance_lookup(cur, state_name, year)
    print(f"  DB appearances lookup: {len(lookup)} rows", file=sys.stderr)
    if not lookup:
        sys.exit(f"No appearances in DB for {state_name} {year}. "
                 "Run the affidavit pipeline first (fetch → OCR → apply → migrate).")

    winners = 0
    updated = 0
    unmatched = []

    for const in payload["constituencies"]:
        const_norm = _normalize_constituency(const["name"])
        for cand in const["candidates"]:
            aid = fuzzy_match(cand["name"], const_norm, lookup,
                                args.match_threshold)
            if not aid:
                unmatched.append((const["name"], cand["name"],
                                     cand.get("total_votes"), cand.get("rank")))
                continue
            if not args.dry_run:
                cur.execute("""
                    UPDATE election_appearances
                    SET won = ?, votes_received = ?, vote_share_pct = ?
                    WHERE id = ?
                """, (bool(cand.get("won")), cand.get("total_votes"),
                       cand.get("vote_pct"), aid))
            updated += 1
            if cand.get("won"):
                winners += 1

    if not args.dry_run:
        con.commit()
    con.close()

    print(f"\n========== APPLY SUMMARY ==========", file=sys.stderr)
    print(f"  Updated appearances: {updated}", file=sys.stderr)
    print(f"  Winners flagged:     {winners}", file=sys.stderr)
    print(f"  Unmatched candidates: {len(unmatched)}", file=sys.stderr)
    if unmatched:
        print(f"  First 10 unmatched:", file=sys.stderr)
        for c, n, v, r in unmatched[:10]:
            print(f"    [{r}] {c:22s} {n!r} votes={v}", file=sys.stderr)


if __name__ == "__main__":
    main()
