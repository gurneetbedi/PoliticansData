"""
One-shot migration: wipe the myneta-sourced data in the canonical tables
and repopulate them from eci_candidates_provisional. After this script
runs, the entire site reads from ECI data exclusively.

Existing pages (Dashboard / Rankings / Politician detail / Heatmap) keep
working because the table SHAPE is unchanged. Fields where ECI doesn't
have data (financial totals, criminal case counts, votes received) end
up NULL — templates handle the NULL gracefully.

ONE STATE, ONE ELECTION
-----------------------
Only Delhi 2025 Assembly is populated. Other state rows in states/
elections/constituencies tables get deleted. The myneta scraper code in
app/scrapers/ stays put — it's just disabled per the ADR cease-and-desist
and the data it would have produced is gone.

BACKUP FIRST
------------
The script automatically copies politrack.db → politrack.db.bak before
making any changes. If anything looks wrong after, restore with:
    cp politrack.db.bak politrack.db

USAGE
-----
    python scripts/migrate_to_eci_only.py
    python scripts/migrate_to_eci_only.py --dry-run    # show counts, don't write
"""
from __future__ import annotations

import argparse
import re
import shutil
import sqlite3
import sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path

DB_DEFAULT = Path(__file__).resolve().parent.parent / "politrack.db"


def _slugify(s: str) -> str:
    """URL-safe slug from candidate name."""
    s = (s or "").lower()
    s = re.sub(r"[^a-z0-9]+", "-", s).strip("-")
    return s or "unknown"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=str(DB_DEFAULT))
    ap.add_argument("--dry-run", action="store_true",
                    help="Report what would happen without writing")
    ap.add_argument("--skip-backup", action="store_true",
                    help="Skip the politrack.db.bak step (faster re-runs)")
    args = ap.parse_args()

    db = Path(args.db)
    if not db.exists():
        sys.exit(f"DB not found: {db}")

    # ------- Backup ----------------------------------------------------
    if not args.skip_backup and not args.dry_run:
        backup = db.with_suffix(db.suffix + ".bak")
        shutil.copy2(db, backup)
        print(f"Backed up DB to {backup}", file=sys.stderr)

    con = sqlite3.connect(str(db))
    con.row_factory = sqlite3.Row
    cur = con.cursor()

    # ------- Read source: ALL Delhi cycles in provisional table --------
    # Multi-cycle: pull every (state, year) that exists. For each cycle,
    # we'll insert an elections row and link election_appearances to it.
    # Cross-cycle politician matching happens further below.
    cur.execute("""
        SELECT DISTINCT election_year
        FROM eci_candidates_provisional
        WHERE state = 'Delhi' AND affidavit_status = 'Accepted'
        ORDER BY election_year DESC
    """)
    cycle_years = [r[0] for r in cur.fetchall()]
    print(f"Found Delhi cycles in provisional: {cycle_years}",
          file=sys.stderr)

    # Pull rows per cycle, deduped within-cycle by (name, constituency)
    # picking the HIGHEST affidavit_id (most recent filing).
    rows_by_cycle: dict[int, list[dict]] = {}
    for year in cycle_years:
        cur.execute("""
            SELECT *
            FROM eci_candidates_provisional
            WHERE state = 'Delhi' AND election_year = ?
                  AND affidavit_status = 'Accepted'
        """, (year,))
        all_rows = [dict(r) for r in cur.fetchall()]
        # Dedup: pick max affidavit_id per (name, constituency)
        from collections import defaultdict
        by_key: dict[tuple, dict] = {}
        for r in all_rows:
            name = (r.get("candidate_name") or "").strip()
            const = (r.get("constituency") or "").strip()
            if not name or not const:
                continue
            key = (name, const)
            existing = by_key.get(key)
            if existing is None:
                by_key[key] = r
            else:
                # Compare affidavit_ids numerically; keep higher
                try:
                    if int(r["affidavit_id"]) > int(existing["affidavit_id"]):
                        by_key[key] = r
                except (TypeError, ValueError):
                    pass
        rows_by_cycle[year] = list(by_key.values())
        print(f"  Cycle {year}: {len(all_rows)} rows → "
              f"{len(by_key)} unique candidates after dedup",
              file=sys.stderr)

    eci_rows_all = [r for rows in rows_by_cycle.values() for r in rows]

    if args.dry_run:
        # Just count what each table would have
        parties = {r["party"] for r in eci_rows_all if r["party"]}
        # Count NORMALIZED constituencies (so '(SC)' suffixes and
        # spelling variants collapse — same logic the real run uses)
        consts = {_normalize_constituency(r["constituency"])
                   for r in eci_rows_all if r["constituency"]}
        print(f"\nWould populate:", file=sys.stderr)
        print(f"  states            1   (Delhi)", file=sys.stderr)
        print(f"  elections        {len(cycle_years)}   (one per Delhi cycle)",
              file=sys.stderr)
        print(f"  constituencies   {len(consts):>2d}   "
              f"(Delhi assembly seats, post-normalization)",
              file=sys.stderr)
        print(f"  parties          {len(parties):>2d}   (deduplicated party names)",
              file=sys.stderr)
        # Politician count = unique candidates across cycles (we'll
        # cross-match by name+constituency below)
        all_keys = set()
        for rows in rows_by_cycle.values():
            for r in rows:
                all_keys.add((_normalize_name(r["candidate_name"]),
                               r["constituency"]))
        print(f"  politicians (approx): {len(all_keys):>3d}   "
              f"(cross-cycle deduped by normalized name + constituency)",
              file=sys.stderr)
        print(f"  election_appearances: {len(eci_rows_all):>3d}",
              file=sys.stderr)
        return

    # ------- Wipe canonical tables -------------------------------------
    print("Wiping myneta-sourced rows from canonical tables ...",
          file=sys.stderr)
    for t in (
        "criminal_cases", "liabilities", "assets",
        "election_appearances",
        "politicians",
        "constituencies", "elections", "parties", "states",
    ):
        cur.execute(f"DELETE FROM {t}")
        cur.execute(f"DELETE FROM sqlite_sequence WHERE name = ?", (t,))

    # ------- Populate states + ONE elections row per cycle -------------
    cur.execute(
        "INSERT INTO states (id, name, code) VALUES (1, 'Delhi', 'DL')"
    )
    state_id = 1

    # One elections row per cycle, indexed by year
    election_id_by_year: dict[int, int] = {}
    for i, year in enumerate(sorted(cycle_years), 1):
        cur.execute(
            "INSERT INTO elections (id, year, house, state_id, myneta_slug) "
            "VALUES (?, ?, 'Assembly', ?, ?)",
            (i, year, state_id, f"Delhi{year}"),
        )
        election_id_by_year[year] = i
    print(f"  Elections inserted: {len(cycle_years)} "
          f"({', '.join(str(y) for y in sorted(cycle_years))})",
          file=sys.stderr)

    # ------- Populate constituencies (dedup + normalize across cycles) -
    # Normalize spellings so '(SC)' suffixes, dotted initials, and
    # whitespace variants all map to one row. Canonical name = the
    # MOST RECENT cycle's spelling (so it matches the constituency_coords
    # geojson + map / heatmap templates, which use 2025 spelling).
    canonical_by_norm: dict[str, str] = {}
    for year in sorted(cycle_years, reverse=True):
        # Iterate latest cycle first; first writer wins
        for r in rows_by_cycle[year]:
            const_raw = r.get("constituency") or ""
            norm = _normalize_constituency(const_raw)
            if norm and norm not in canonical_by_norm:
                # Clean the canonical: strip (SC)/(ST) suffix so the
                # display name reads as 'AMBEDKAR NAGAR' not
                # 'AMBEDKAR NAGAR(SC)'.
                display = const_raw
                for suf in ("(SC)", "(ST)"):
                    if display.endswith(suf):
                        display = display[: -len(suf)].strip()
                canonical_by_norm[norm] = display

    const_id_by_norm: dict[str, int] = {}
    for i, (norm, name) in enumerate(sorted(canonical_by_norm.items(),
                                              key=lambda kv: kv[1]), 1):
        cur.execute(
            "INSERT INTO constituencies (id, name, state_id, house) "
            "VALUES (?, ?, ?, ?)",
            (i, name, state_id, "Assembly"),
        )
        const_id_by_norm[norm] = i
    print(f"  Constituencies inserted: {len(canonical_by_norm)} "
          f"(canonical, post-normalization)", file=sys.stderr)

    # ------- Populate parties (dedup ACROSS cycles) --------------------
    # short_name is UNIQUE in the schema, so disambiguate collisions by
    # appending -2, -3, etc. Process MAJOR PARTIES FIRST so they always
    # get their canonical short_name — otherwise an obscure
    # "Aam Aadmi Parivartan Party" claims "AAP" before the real AAP.
    parties = sorted({r["party"] for r in eci_rows_all if r["party"]})
    _MAJOR_ORDER = [
        "Aam Aadmi Party", "Bharatiya Janata Party",
        "Indian National Congress", "Bahujan Samaj Party",
        "Samajwadi Party", "Nationalist Congress Party",
        "Rashtriya Janata Dal", "Independent",
    ]
    # Order major parties first (in priority order), then everyone else alpha
    ordered_parties = []
    for mp in _MAJOR_ORDER:
        if mp in parties:
            ordered_parties.append(mp)
    for p in parties:
        if p not in ordered_parties:
            ordered_parties.append(p)

    party_id_by_name: dict[str, int] = {}
    used_shorts: set[str] = set()
    for i, full_name in enumerate(ordered_parties, 1):
        base_short = _party_short_name(full_name)
        short = base_short
        n = 2
        while short in used_shorts:
            short = f"{base_short}-{n}"
            n += 1
        used_shorts.add(short)
        cur.execute(
            "INSERT INTO parties (id, short_name, full_name) VALUES (?, ?, ?)",
            (i, short, full_name),
        )
        party_id_by_name[full_name] = i
    print(f"  Parties inserted: {len(ordered_parties)}", file=sys.stderr)

    # ------- Populate politicians + election_appearances (multi-cycle) -
    # Cross-cycle matching: same person in the SAME constituency across
    # cycles → reuse politician_id. Match key is
    # `(normalized_name, constituency)`. If a 2020 candidate name matches
    # a 2025 candidate name in the same constituency after normalization,
    # we treat them as the same politician and append a new
    # election_appearance instead of creating a new politician row.
    #
    # Iteration order: process the LATEST cycle first so the politician
    # row carries the most recent name spelling / profession / age. Older
    # cycles attach as appearances on the already-created politician.
    used_slugs: set[str] = set()
    pol_inserted = 0
    appear_inserted = 0
    skipped_no_constituency = 0
    cross_cycle_matches = 0
    # politician_id keyed by (normalized_name, constituency)
    pol_id_by_key: dict[tuple, int] = {}

    for year in sorted(cycle_years, reverse=True):
        for r in rows_by_cycle[year]:
            const_raw = r.get("constituency") or ""
            const_norm = _normalize_constituency(const_raw)
            const_id = const_id_by_norm.get(const_norm)
            if not const_id:
                skipped_no_constituency += 1
                continue

            norm_name = _normalize_name(r["candidate_name"])
            # Match by (normalized_name, normalized_constituency) so a
            # 2020 candidate in 'AMBEDKAR NAGAR(SC)' matches their 2025
            # entry in 'AMBEDKAR NAGAR'.
            match_key = (norm_name, const_norm)

            if match_key in pol_id_by_key:
                # Same candidate seen in a later cycle already — reuse id
                pol_id = pol_id_by_key[match_key]
                cross_cycle_matches += 1
            else:
                # New politician — insert
                base = (
                    f"{_slugify(r['candidate_name'])}"
                    f"-delhi{year}-{r['affidavit_id'] or ''}"
                ).rstrip("-")
                slug = base
                n = 2
                while slug in used_slugs:
                    slug = f"{base}-{n}"
                    n += 1
                used_slugs.add(slug)

                cur.execute(
                    "INSERT INTO politicians "
                    "(name, slug, myneta_candidate_id, age, profession, "
                    " created_at, updated_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (
                        r["candidate_name"],
                        slug,
                        None,        # no myneta candidate id — pure ECI
                        r.get("age"),
                        r.get("profession_self"),
                        datetime.utcnow(),
                        datetime.utcnow(),
                    ),
                )
                pol_id = cur.lastrowid
                pol_id_by_key[match_key] = pol_id
                pol_inserted += 1

            party_id = (party_id_by_name.get(r["party"])
                         if r.get("party") else None)
            # const_id was set at top of loop from the normalized key
            election_id = election_id_by_year[year]
            source_url = "https://affidavit.eci.gov.in/"

            # PHASE 0 POLICY: regex-extracted numeric fields all NULL.
            # See migrate_to_eci_only.py git history for the rationale.
            cur.execute(
                "INSERT INTO election_appearances "
                "(politician_id, election_id, constituency_id, party_id, "
                " age, education, profession, won, "
                " total_assets_inr, total_liabilities_inr, "
                " movable_assets_inr, immovable_assets_inr, "
                " criminal_cases_count, serious_cases_count, "
                " source_url, scraped_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    pol_id, election_id, const_id, party_id,
                    r.get("age"),
                    r.get("education"),
                    r.get("profession_self"),
                    False,        # won — awaiting Phase 2 winner cross-ref
                    None, None, None, None,   # financials — NULL per Phase 0
                    None, None,                # criminal — NULL per Phase 0
                    source_url,
                    datetime.utcnow(),
                ),
            )
            appear_inserted += 1

    con.commit()
    con.close()

    # ------- Summary ---------------------------------------------------
    print(f"\n========== MIGRATION DONE ==========", file=sys.stderr)
    print(f"  Cycles loaded:                  {len(cycle_years)} "
          f"({', '.join(str(y) for y in sorted(cycle_years))})",
          file=sys.stderr)
    print(f"  Politicians inserted:           {pol_inserted}", file=sys.stderr)
    print(f"  Cross-cycle matches reused:     {cross_cycle_matches} "
          f"(same person, multiple cycles)", file=sys.stderr)
    print(f"  Election appearances inserted:  {appear_inserted}", file=sys.stderr)
    print(f"  Skipped (no constituency):      {skipped_no_constituency}",
          file=sys.stderr)
    print(f"  Backup at: {db.with_suffix('.db.bak')}", file=sys.stderr)


def _sum_or_null(*xs):
    xs = [int(x) for x in xs if x is not None and str(x).strip()]
    return sum(xs) if xs else None


def _normalize_constituency(name: str) -> str:
    """Normalize a Delhi constituency name for cross-cycle matching.

    Cycles spell constituencies inconsistently:
      2020: 'AMBEDKAR NAGAR(SC)'  vs  2025: 'AMBEDKAR NAGAR'
      2020: 'R K PURAM'           vs  2025: 'R. K. PURAM'
      2020: 'SEELAMPUR'           vs  2025: 'SEELAM PUR'
      2020: 'NARELA'              vs  2025: 'NERELA'  (spelling variant)

    Steps: uppercase, strip (SC)/(ST), strip all whitespace and dots so
    'R K PURAM' / 'R. K. PURAM' / 'R.K.PURAM' all collapse to 'RKPURAM'.
    A small alias map handles true spelling variants (NARELA↔NERELA).
    """
    if not name:
        return ""
    s = name.upper().strip()
    # Strip reservation suffixes
    for suf in ("(SC)", "(ST)", " SC", " ST"):
        if s.endswith(suf):
            s = s[: -len(suf)].strip()
    # Collapse all non-alpha-numeric (spaces, dots, dashes, etc.)
    s = re.sub(r"[^A-Z0-9]+", "", s)
    # Manual alias map for known spelling variants
    _ALIASES = {
        "NARELA":   "NERELA",   # 2020 → 2025 spelling
    }
    return _ALIASES.get(s, s)


def _normalize_name(name: str) -> str:
    """Normalize a candidate name for cross-cycle matching.

    The same person may appear with slightly different formatting across
    election cycles (extra spaces, S/O suffixes, honorifics, alternate
    spellings of common names). This function strips all of that to give
    a stable comparison key. Match key is `(normalized_name, constituency)`
    — same candidate in the same constituency across cycles → same
    politician_id; different person or different constituency → fresh
    politician_id.

    Conservative on purpose: we do NOT do phonetic matching or Levenshtein
    fuzz, only deterministic strip-and-collapse. False merges here would
    silently combine two different politicians under one slug; better to
    over-split than over-merge.
    """
    if not name:
        return ""
    s = name.upper().strip()
    # Strip common suffixes/prefixes that one cycle may include and another may not
    for prefix in ("DR. ", "DR ", "ADV. ", "ADV ", "ADVOCATE ",
                    "SHRI ", "SHRIMATI ", "SMT. ", "SMT ",
                    "MR. ", "MR ", "MS. ", "MS ", "MRS. ", "MRS "):
        if s.startswith(prefix):
            s = s[len(prefix):]
    # Drop "S/O", "D/O", "W/O" + everything after
    for marker in (" S/O ", " D/O ", " W/O ", " S.O ", " D.O ", " W.O "):
        if marker in s:
            s = s.split(marker)[0]
    # Drop parenthetical notes like "(BABA)" or "(JI)"
    s = re.sub(r"\([^)]*\)", "", s)
    # Collapse all non-alphanumeric runs to a single space
    s = re.sub(r"[^A-Z0-9]+", " ", s)
    return s.strip()


def _party_short_name(full: str) -> str:
    """Best-effort party-short-name guesser. Falls back to initials."""
    f = full.upper()
    if "AAM AADMI" in f: return "AAP"
    if "BHARATIYA JANATA" in f: return "BJP"
    if "INDIAN NATIONAL CONGRESS" in f: return "INC"
    if "BAHUJAN SAMAJ" in f: return "BSP"
    if "SAMAJWADI" in f: return "SP"
    if "NATIONALIST CONGRESS" in f: return "NCP"
    if "RASHTRIYA JANATA" in f: return "RJD"
    if "INDEPENDENT" in f: return "IND"
    # Fall back: initials of capitalised words, capped at 5 chars
    initials = "".join(w[0] for w in full.split() if w and w[0].isupper())
    return (initials or full[:5]).upper()[:8]


if __name__ == "__main__":
    main()
