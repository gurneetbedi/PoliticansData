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

# Local unified error-log writer — every unmatched candidate becomes an
# entry in data/eci/errors/pipeline_errors.jsonl so we have one place to
# look for pipeline health.
sys.path.insert(0, str(Path(__file__).resolve().parent))
from pipeline_errors import log_error  # noqa: E402


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
                 lookup: dict, threshold: int = 75,
                 exclude: set[int] | None = None) -> tuple[int | None, int, str]:
    """Find appearance_id in the given constituency whose name fuzzy-matches.

    Returns (appearance_id, score, method) where method is one of:
      - "exact"       — normalized names match exactly (score = 100)
      - "fuzzy"       — best fuzz score above threshold
      - "no_match"    — nothing above threshold (id will be None)

    `exclude` is a set of appearance IDs already claimed by another
    candidate in this run — the matcher will not return any of them,
    so no two ECI candidates share the same DB politician.
    """
    try:
        from rapidfuzz import fuzz
    except ImportError:
        sys.exit("pip install rapidfuzz")

    exclude = exclude or set()
    cand_norm = _normalize_name(candidate_name)
    if not cand_norm:
        return (None, 0, "no_match")

    # Exact match first
    hit = lookup.get((const_norm, cand_norm))
    if hit and hit not in exclude:
        return (hit, 100, "exact")

    # Fuzzy fallback within the same constituency
    best_id, best_score = None, 0
    for (c, n), aid in lookup.items():
        if c != const_norm or aid in exclude:
            continue
        s = max(fuzz.partial_ratio(cand_norm, n),
                fuzz.token_set_ratio(cand_norm, n),
                fuzz.token_sort_ratio(cand_norm, n))
        if s > best_score:
            best_score, best_id = s, aid
    if best_score >= threshold:
        return (best_id, int(best_score), "fuzzy")
    return (None, int(best_score), "no_match")


def _ensure_match_columns(cur) -> None:
    """Add match_score + match_method columns to election_appearances if
    they don't exist yet. Idempotent — safe to call every run."""
    cols = {r[1] for r in cur.execute("PRAGMA table_info(election_appearances)").fetchall()}
    if "match_score" not in cols:
        cur.execute("ALTER TABLE election_appearances ADD COLUMN match_score INTEGER")
    if "match_method" not in cols:
        cur.execute("ALTER TABLE election_appearances ADD COLUMN match_method TEXT")


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

    _ensure_match_columns(cur)
    lookup = build_appearance_lookup(cur, state_name, year)
    print(f"  DB appearances lookup: {len(lookup)} rows", file=sys.stderr)
    if not lookup:
        sys.exit(f"No appearances in DB for {state_name} {year}. "
                 "Run the affidavit pipeline first (fetch → OCR → apply → migrate).")

    winners = 0
    updated = 0
    unmatched = []
    nota_skipped = 0
    # Track already-consumed appearance IDs so a single DB politician can't
    # be matched to two ECI candidates in the same constituency (which
    # caused winner UPDATEs to be silently overwritten by later runner-up
    # matches — see the "flagged 197, audit shows 161" delta bug).
    used_aids: set[int] = set()

    for const in payload["constituencies"]:
        const_norm = _normalize_constituency(const["name"])
        # Process candidates in RANK order so winners get first pick when
        # names in a constituency fuzzy-collide.
        cands_sorted = sorted(
            const["candidates"],
            key=lambda c: c.get("rank", 999)
        )
        for cand in cands_sorted:
            # NOTA is "None of the Above" — a ballot option, not a person.
            # No politician row to update.
            if (cand.get("name") or "").strip().upper() == "NOTA":
                nota_skipped += 1
                continue
            aid, score, method = fuzzy_match(
                cand["name"], const_norm, lookup,
                args.match_threshold,
                exclude=used_aids,
            )
            if not aid:
                unmatched.append((const["name"], cand["name"],
                                     cand.get("total_votes"), cand.get("rank")))
                # Log to unified pipeline error log
                log_error(
                    stage="match_results",
                    state=state_name, year=year,
                    candidate=cand["name"],
                    constituency=const["name"],
                    error_type="fuzzy_no_match",
                    message=f"Best fuzz score was {score} (threshold {args.match_threshold})",
                    extra={"rank": cand.get("rank"),
                            "votes": cand.get("total_votes"),
                            "party": cand.get("party", "")},
                )
                continue
            used_aids.add(aid)
            if not args.dry_run:
                cur.execute("""
                    UPDATE election_appearances
                    SET won = ?, votes_received = ?, vote_share_pct = ?,
                        match_score = ?, match_method = ?
                    WHERE id = ?
                """, (bool(cand.get("won")), cand.get("total_votes"),
                       cand.get("vote_pct"), score, method, aid))
            updated += 1
            if cand.get("won"):
                winners += 1

    if not args.dry_run:
        con.commit()
    con.close()

    print(f"\n========== APPLY SUMMARY ==========", file=sys.stderr)
    print(f"  Updated appearances: {updated}", file=sys.stderr)
    print(f"  Winners flagged:     {winners}", file=sys.stderr)
    print(f"  NOTA skipped:        {nota_skipped}", file=sys.stderr)
    print(f"  Unmatched candidates: {len(unmatched)}", file=sys.stderr)

    # Post-run confidence breakdown — surfaces matches that ran below 85
    # (worth a manual look) or exactly at 100 (exact, safe).
    if not args.dry_run:
        con2 = sqlite3.connect(args.db)
        cur2 = con2.cursor()
        conf = cur2.execute("""
            SELECT
              SUM(CASE WHEN match_score = 100 THEN 1 ELSE 0 END) AS exact_,
              SUM(CASE WHEN match_score >= 85 AND match_score < 100 THEN 1 ELSE 0 END) AS strong,
              SUM(CASE WHEN match_score >= 75 AND match_score < 85 THEN 1 ELSE 0 END) AS uncertain
            FROM election_appearances ea
            JOIN elections e ON ea.election_id = e.id
            JOIN states s    ON e.state_id     = s.id
            WHERE s.name = ? AND e.year = ? AND ea.match_score IS NOT NULL
        """, (state_name, year)).fetchone()
        con2.close()
        e, s, u = (conf[0] or 0, conf[1] or 0, conf[2] or 0)
        print(f"  Match confidence — exact:{e}  strong(≥85):{s}  uncertain(75-84):{u}",
              file=sys.stderr)
    if unmatched:
        print(f"  First 10 unmatched:", file=sys.stderr)
        for c, n, v, r in unmatched[:10]:
            print(f"    [{r}] {c:22s} {n!r} votes={v}", file=sys.stderr)


if __name__ == "__main__":
    main()
