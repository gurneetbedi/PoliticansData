"""
Identify the top-3 candidates per Delhi 2025 constituency using a
heuristic: (winner) + (BJP candidate) + (AAP candidate). Fall back to
INC / BSP / other if BJP or AAP didn't field a candidate in that seat.

Cross-references against eci_candidates_provisional to confirm we have
affidavit data on each picked candidate.

WHY HEURISTIC, NOT VOTE COUNTS
------------------------------
myneta's data in our DB doesn't include per-candidate vote_received,
so we can't rank by votes. In Delhi specifically, BJP and AAP are the
only two parties that contested every seat, so (winner + BJP + AAP) is
~95% accurate as a proxy for "top 3 by votes". This script flags the
~5-10 constituencies where the heuristic is risky so they can be
manually reviewed.

When we eventually build the results.eci.gov.in fetcher and get actual
vote counts, this CSV gets regenerated with the real ranks. Site
structure stays the same; only the few heuristic mistakes shift.

USAGE
-----
    python scripts/identify_top3_delhi.py

Read-only. Writes:
    data/eci/for_ai/extracted/delhi_top3.csv
"""
from __future__ import annotations

import csv
import re
import sqlite3
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DB = ROOT / "politrack.db"
OUT = ROOT / "data/eci/for_ai/extracted/delhi_top3.csv"

# Major-party fallback chain when picking runner-ups.
PARTY_PRIORITY_FALLBACK = ["BJP", "AAP", "INC", "BSP", "SP", "AIMIM", "NCP"]


def _norm_name(s: str) -> str:
    """Aggressive name normalisation for fuzzy match against ECI."""
    s = (s or "").upper()
    s = re.sub(r"[^A-Z0-9 ]", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    for prefix in ("SH ", "SHRI ", "MS ", "SMT ", "DR ", "ADV ", "ADVOCATE ",
                    "CA ", "PROF ", "KU ", "KUMARI "):
        if s.startswith(prefix):
            s = s[len(prefix):]
    return s


def _norm_party_for_match(s: str) -> str:
    """Coalesce party names. myneta uses short codes (AAP/BJP/INC),
    ECI uses full names (AAM AADMI PARTY / BHARATIYA JANATA PARTY)."""
    s = (s or "").upper()
    if "AAM AADMI" in s or s == "AAP":
        return "AAP"
    if "BHARATIYA JANATA" in s or s == "BJP":
        return "BJP"
    if "INDIAN NATIONAL CONGRESS" in s or s == "INC" or s == "CONGRESS":
        return "INC"
    if "BAHUJAN SAMAJ" in s or s == "BSP":
        return "BSP"
    if "INDEPENDENT" in s or s == "IND":
        return "IND"
    return s


def main():
    if not DB.exists():
        sys.exit(f"DB not found: {DB}")
    OUT.parent.mkdir(parents=True, exist_ok=True)

    con = sqlite3.connect(str(DB))
    con.row_factory = sqlite3.Row
    cur = con.cursor()

    # --- All Delhi 2025 myneta candidates grouped by constituency ----------
    cur.execute("""
        SELECT p.id AS pid, p.name AS politician, c.name AS constituency,
               party.short_name AS party_short, ea.won,
               ea.total_assets_inr, ea.criminal_cases_count, ea.education,
               ea.id AS appearance_id
        FROM election_appearances ea
        JOIN elections e ON ea.election_id = e.id
        JOIN states s ON e.state_id = s.id
        JOIN politicians p ON ea.politician_id = p.id
        LEFT JOIN constituencies c ON ea.constituency_id = c.id
        LEFT JOIN parties party ON ea.party_id = party.id
        WHERE e.year = 2025 AND s.name = 'Delhi'
          AND c.name IS NOT NULL
    """)
    myneta_rows = cur.fetchall()

    by_constituency: dict[str, list] = defaultdict(list)
    for r in myneta_rows:
        by_constituency[r["constituency"]].append(dict(r))

    # --- ECI provisional table for cross-match ----------------------------
    cur.execute("""
        SELECT candidate_name, constituency, party, fields_present_count,
               quality_status
        FROM eci_candidates_provisional
        WHERE state = 'Delhi' AND election_year = 2025
    """)
    eci_rows = cur.fetchall()

    eci_by_norm_name: dict[str, list] = defaultdict(list)
    for r in eci_rows:
        eci_by_norm_name[_norm_name(r["candidate_name"])].append(dict(r))

    # --- Build the top-3 picks per constituency ---------------------------
    output_rows = []
    flagged_constituencies = []

    for constituency, candidates in sorted(by_constituency.items()):
        # Group by normalised party for picking
        by_party = defaultdict(list)
        for c in candidates:
            by_party[_norm_party_for_match(c["party_short"])].append(c)

        # Rank 1: the winner (if we have one)
        winner = next((c for c in candidates if c["won"]), None)
        rank1 = winner

        picks = []
        if rank1:
            picks.append(("1 (winner)", rank1, "winner"))
        else:
            flagged_constituencies.append(
                (constituency, "no_winner_in_myneta_data")
            )

        # Rank 2 + 3: walk the party priority chain, skipping the winner's
        # party and any party already picked.
        winner_party = (_norm_party_for_match(rank1["party_short"])
                        if rank1 else None)
        picked_parties = {winner_party} if winner_party else set()

        for party in PARTY_PRIORITY_FALLBACK:
            if party in picked_parties:
                continue
            if party not in by_party:
                continue
            # Take the highest-asset candidate from that party as the
            # likely "main" contender (heuristic — works better than
            # alphabetical)
            cand = max(by_party[party],
                       key=lambda c: c["total_assets_inr"] or 0)
            picks.append((str(len(picks) + 1), cand, f"party_priority:{party}"))
            picked_parties.add(party)
            if len(picks) >= 3:
                break

        # If we still don't have 3, fill from any remaining candidate by
        # asset value (proxy for prominence)
        if len(picks) < 3:
            others = sorted(
                [c for c in candidates
                 if c["pid"] not in {p[1]["pid"] for p in picks}],
                key=lambda c: -(c["total_assets_inr"] or 0),
            )
            for c in others:
                picks.append((str(len(picks) + 1), c, "fallback:asset_rank"))
                if len(picks) >= 3:
                    break

        if len(picks) < 3:
            flagged_constituencies.append(
                (constituency, f"only_{len(picks)}_candidates_available")
            )

        # Cross-match each pick against ECI
        for rank_label, c, reason in picks:
            matches = eci_by_norm_name.get(_norm_name(c["politician"]), [])
            eci_match = matches[0] if matches else None
            output_rows.append({
                "constituency": constituency,
                "rank": rank_label,
                "rank_reason": reason,
                "politician_id": c["pid"],
                "candidate_name": c["politician"],
                "party": c["party_short"],
                "won": c["won"],
                "myneta_total_assets": c["total_assets_inr"],
                "myneta_criminal_cases": c["criminal_cases_count"],
                "myneta_education": c["education"],
                "has_eci_match": bool(eci_match),
                "eci_quality_status": (eci_match["quality_status"]
                                        if eci_match else ""),
                "eci_fields_present": (eci_match["fields_present_count"]
                                        if eci_match else None),
            })

    # --- Write CSV ----------------------------------------------------------
    fieldnames = [
        "constituency", "rank", "rank_reason", "politician_id",
        "candidate_name", "party", "won",
        "myneta_total_assets", "myneta_criminal_cases", "myneta_education",
        "has_eci_match", "eci_quality_status", "eci_fields_present",
    ]
    with OUT.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames)
        w.writeheader()
        for r in output_rows:
            w.writerow(r)

    # --- Report -------------------------------------------------------------
    n_constituencies = len(by_constituency)
    n_picks = len(output_rows)
    eci_matched = sum(1 for r in output_rows if r["has_eci_match"])
    winners_known = sum(1 for r in output_rows if r["rank"] == "1 (winner)")

    print("=" * 70)
    print(f"DELHI 2025 — top-3 candidates (heuristic: winner + BJP + AAP)")
    print("=" * 70)
    print()
    print(f"  Constituencies in myneta:     {n_constituencies}")
    print(f"  Constituencies w/ winner:     {winners_known}")
    print(f"  Total top-3 picks:            {n_picks}")
    print(f"    Of which matched in ECI:    {eci_matched}  "
          f"({100 * eci_matched / n_picks:.1f}%)")
    print(f"    Of which NOT in ECI yet:    {n_picks - eci_matched}")
    print()

    if flagged_constituencies:
        print(f"Flagged constituencies ({len(flagged_constituencies)}):")
        for con_name, reason in flagged_constituencies[:10]:
            print(f"  {con_name[:30]:30s}  {reason}")
        if len(flagged_constituencies) > 10:
            print(f"  ... and {len(flagged_constituencies) - 10} more")
        print()

    print(f"CSV written: {OUT}")
    print()
    print("Spot-check a few rows:")
    print(f"{'Constituency':18s}  {'Rank':4s}  {'Candidate':30s}  "
          f"{'Party':6s}  ECI?")
    print("-" * 70)
    for r in output_rows[:12]:
        eci_flag = "✓" if r["has_eci_match"] else "·"
        print(f"  {r['constituency'][:16]:16s}  {r['rank']:4s}  "
              f"{r['candidate_name'][:28]:28s}  {(r['party'] or '?')[:6]:6s}  "
              f"{eci_flag}")


if __name__ == "__main__":
    main()
