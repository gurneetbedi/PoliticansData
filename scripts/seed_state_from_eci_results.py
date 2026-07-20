"""Seed the canonical DB tables (states / elections / constituencies /
politicians / election_appearances) for a state from its ECI results JSON.

Use this when a state has an ECI affidavit dataset (Cloud Vision +
Gemini extractions) but was never added to the canonical tables. The
apply_llm_extraction step needs an existing election_appearance row to
attach wealth/education to — without a seed, all writes go into the
void.

Reads:
    data/eci/results/<slug>_<year>_eci_results.json   (required)

Writes to these DB tables ONLY (never touches existing rows in other states):
    states                    — 1 row per new state
    elections                 — 1 row per (state, year)
    constituencies            — 1 row per constituency
    parties                   — 1 row per unique party short_name
    politicians               — 1 row per unique candidate
    election_appearances      — 1 row per candidate × constituency

Dry-run by default. --commit applies.

Usage:
    source secrets/.env    # for DATABASE_URL
    python scripts/seed_state_from_eci_results.py \\
        --results data/eci/results/uttarpradesh_2022_eci_results.json

    python scripts/seed_state_from_eci_results.py \\
        --results data/eci/results/uttarpradesh_2022_eci_results.json \\
        --commit

Safety:
    - Skips insert if the state already has ANY election_appearance rows
      (protects against accidental double-seeding).
    - --force to override that safety.
"""
from __future__ import annotations
import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

import sqlalchemy
from sqlalchemy import text
from sqlalchemy.orm import sessionmaker
from slugify import slugify


ROOT = Path(__file__).resolve().parent.parent
DB_URL = os.environ.get("DATABASE_URL", f"sqlite:///{ROOT / 'lokvani.db'}")


def get_or_create_id(sess, table, where_kwargs: dict, insert_kwargs: dict) -> int:
    """Look up a row by `where_kwargs`; if missing, insert with the union
    of where_kwargs + insert_kwargs. Returns the row's id."""
    where_clause = " AND ".join(f"{k} = :{k}" for k in where_kwargs)
    row = sess.execute(
        text(f"SELECT id FROM {table} WHERE {where_clause}"),
        where_kwargs,
    ).fetchone()
    if row:
        return row[0]
    all_kwargs = {**where_kwargs, **insert_kwargs}
    cols = ", ".join(all_kwargs)
    vals = ", ".join(f":{k}" for k in all_kwargs)
    row = sess.execute(
        text(f"INSERT INTO {table} ({cols}) VALUES ({vals}) RETURNING id"),
        all_kwargs,
    ).fetchone()
    return row[0]


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                  formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--results", required=True,
                    help="Path to <slug>_<year>_eci_results.json")
    ap.add_argument("--commit", action="store_true")
    ap.add_argument("--force", action="store_true",
                    help="Override the 'already-seeded' safety check")
    args = ap.parse_args()

    data = json.loads(Path(args.results).read_text())
    state_name = data["state"]
    year = int(data["year"])
    house = data.get("house", "Assembly")
    constituencies = data["constituencies"]

    print(f"State:            {state_name}")
    print(f"Year:             {year}")
    print(f"House:            {house}")
    print(f"Constituencies:   {len(constituencies)}")
    total_cands = sum(len(c.get("candidates", [])) for c in constituencies)
    winners = sum(1 for c in constituencies for cand in c.get("candidates", [])
                  if cand.get("won"))
    print(f"Total candidates: {total_cands}")
    print(f"Winners:          {winners}")

    engine = sqlalchemy.create_engine(DB_URL)
    sess = sessionmaker(bind=engine)()

    # Safety: is this state already seeded?
    existing_count = sess.execute(text("""
        SELECT COUNT(*)
        FROM election_appearances ea
        JOIN elections e ON ea.election_id = e.id
        JOIN states s    ON e.state_id = s.id
        WHERE s.name = :s AND e.year = :y
    """), {"s": state_name, "y": year}).scalar() or 0

    if existing_count > 0 and not args.force:
        sys.exit(f"\n⚠ {state_name} {year} already has {existing_count} "
                  f"election_appearance rows in DB. Refusing to seed. "
                  f"Use --force to override (creates duplicates).")

    if not args.commit:
        print(f"\n--- DRY RUN. Would insert {len(constituencies)} constituencies + "
              f"{total_cands} candidates. Re-run with --commit.")
        return

    now = datetime.now(timezone.utc)

    # 1. State
    state_id = get_or_create_id(sess, "states",
        {"name": state_name},
        {"code": None, "created_at": now, "updated_at": now})

    # 2. Election
    election_id = get_or_create_id(sess, "elections",
        {"year": year, "state_id": state_id, "house": house},
        {"myneta_slug": f"{state_name.lower().replace(' ', '')}{year}",
         "created_at": now, "updated_at": now})

    # 3. Constituencies + candidates + appearances
    party_cache: dict[str, int] = {}
    inserted_c = inserted_p = inserted_pol = inserted_a = 0
    for const in constituencies:
        const_name = const["name"]
        const_id = get_or_create_id(sess, "constituencies",
            {"name": const_name, "state_id": state_id, "house": house},
            {"created_at": now, "updated_at": now})
        inserted_c += 1

        for cand in const.get("candidates", []):
            party_short = cand.get("party", "") or "UNKNOWN"
            if party_short not in party_cache:
                pid = get_or_create_id(sess, "parties",
                    {"short_name": party_short},
                    {"full_name": None, "created_at": now, "updated_at": now})
                party_cache[party_short] = pid
                inserted_p += 1
            party_id = party_cache[party_short]

            cand_name = cand["name"]
            pol_id = get_or_create_id(sess, "politicians",
                {"name": cand_name},
                {"slug": slugify(f"{cand_name}-{state_name}-{year}"),
                 "created_at": now, "updated_at": now})
            inserted_pol += 1

            # Election appearance (fresh row per candidate × election)
            sess.execute(text("""
                INSERT INTO election_appearances
                    (politician_id, election_id, constituency_id, party_id,
                     age, won, votes_received, vote_share_pct,
                     created_at, updated_at)
                VALUES
                    (:pol, :ele, :con, :par,
                     :age, :won, :votes, :pct,
                     :now, :now)
            """), {
                "pol": pol_id, "ele": election_id, "con": const_id, "par": party_id,
                "age": cand.get("age"),
                "won": bool(cand.get("won")),
                "votes": cand.get("total_votes"),
                "pct":   cand.get("vote_pct"),
                "now": now,
            })
            inserted_a += 1

    sess.commit()

    print(f"\n✓ Committed:")
    print(f"   State + Election:       1")
    print(f"   Constituencies touched: {inserted_c}")
    print(f"   Parties touched:        {len(party_cache)}")
    print(f"   Politicians touched:    {inserted_pol}")
    print(f"   Appearances inserted:   {inserted_a}")


if __name__ == "__main__":
    main()
