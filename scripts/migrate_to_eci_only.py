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
The script automatically copies lokvani.db → lokvani.db.bak before
making any changes. If anything looks wrong after, restore with:
    cp lokvani.db.bak lokvani.db

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

DB_DEFAULT = Path(__file__).resolve().parent.parent / "lokvani.db"


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
                    help="Skip the lokvani.db.bak step (faster re-runs)")
    args = ap.parse_args()

    db = Path(args.db)
    if not db.exists():
        sys.exit(f"DB not found: {db}")

    # ------- Backup ----------------------------------------------------
    if not args.skip_backup and not args.dry_run:
        backup = db.with_suffix(db.suffix + ".bak")
        shutil.copy2(db, backup)
        print(f"Backed up DB to {backup}", file=sys.stderr)

    # ------- Ensure canonical schema exists ----------------------------
    # If this DB was just created (e.g. after corruption recovery), the
    # canonical tables — politicians, election_appearances, parties,
    # constituencies, elections, states, assets, liabilities,
    # criminal_cases — won't exist yet because they're normally created
    # by FastAPI's startup hook via SQLAlchemy. Pull the schema from the
    # app's models so we don't duplicate it here.
    #
    # The DATABASE_URL env var is consulted by app/database.py; if not
    # set, it defaults to sqlite:///./lokvani.db. To target a different
    # path, set DATABASE_URL accordingly. Most users don't need to.
    if not args.dry_run:
        try:
            import os
            # Make `app.database` importable regardless of where the script
            # is called from. The script lives at scripts/, so the project
            # root is one level up. Without this prepend, Python's default
            # sys.path doesn't include the project root and `import app.*`
            # fails with "No module named 'app'".
            _project_root = str(Path(__file__).resolve().parent.parent)
            if _project_root not in sys.path:
                sys.path.insert(0, _project_root)

            # Point SQLAlchemy at the SAME db file we're migrating, in
            # case the user passed --db with a non-default path.
            os.environ.setdefault(
                "DATABASE_URL", f"sqlite:///{db.resolve()}"
            )
            # Importing here (not at module top) so dry-runs and
            # `--help` invocations don't pay the SQLAlchemy import cost.
            # IMPORTANT: importing app.models is what REGISTERS the table
            # classes with Base.metadata. Without that import, Base has
            # no tables and create_all() silently does nothing.
            from app.database import Base, engine
            import app.models  # noqa: F401  (registers tables on Base)
            Base.metadata.create_all(bind=engine)
            print("Canonical schema verified (tables created if missing).",
                  file=sys.stderr)
        except ImportError as e:
            print(f"  ⚠ Could not import app.database to bootstrap schema: {e}",
                  file=sys.stderr)
            print(f"  ⚠ If you hit 'no such table' errors below, run:",
                  file=sys.stderr)
            print(f"        python -c \"from app.database import Base, engine; "
                  f"Base.metadata.create_all(bind=engine)\"",
                  file=sys.stderr)

    con = sqlite3.connect(str(db))
    con.row_factory = sqlite3.Row
    cur = con.cursor()

    # ------- Read source: ALL (state, cycle) pairs in provisional ------
    # Multi-state, multi-cycle: pull every (state, year) that exists.
    # For each pair, we'll insert an elections row and link
    # election_appearances to it. Cross-cycle politician matching happens
    # per-state (a Manpreet Singh in Punjab is NOT the same person as a
    # Manpreet Singh in Delhi, even if both ran in a 'Patiala' constituency).
    cur.execute("""
        SELECT DISTINCT state, election_year
        FROM eci_candidates_provisional
        WHERE affidavit_status = 'Accepted'
        ORDER BY state, election_year DESC
    """)
    state_cycles = [(r[0], r[1]) for r in cur.fetchall()]
    states_in_data = sorted({s for s, _ in state_cycles})
    print(f"States in provisional: {states_in_data}", file=sys.stderr)
    for st in states_in_data:
        years = sorted({y for s, y in state_cycles if s == st}, reverse=True)
        print(f"  {st}: cycles {years}", file=sys.stderr)

    # Pull rows per (state, cycle), deduped within-cycle by
    # (name, constituency) picking the HIGHEST affidavit_id (most
    # recent filing).
    rows_by_state_cycle: dict[tuple[str, int], list[dict]] = {}
    for state_name, year in state_cycles:
        cur.execute("""
            SELECT *
            FROM eci_candidates_provisional
            WHERE state = ? AND election_year = ?
                  AND affidavit_status = 'Accepted'
        """, (state_name, year))
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
        rows_by_state_cycle[(state_name, year)] = list(by_key.values())
        print(f"  {state_name} {year}: {len(all_rows)} rows → "
              f"{len(by_key)} unique candidates after dedup",
              file=sys.stderr)

    eci_rows_all = [r for rows in rows_by_state_cycle.values() for r in rows]

    if args.dry_run:
        # Just count what each table would have (per state)
        parties = {r["party"] for r in eci_rows_all if r["party"]}
        # Constituencies are per-state, but for dry-run we just want a total
        const_total = 0
        for st in states_in_data:
            st_rows = [r for r in eci_rows_all if r.get("state") == st]
            consts = {_normalize_constituency(r["constituency"])
                       for r in st_rows if r["constituency"]}
            const_total += len(consts)
        print(f"\nWould populate:", file=sys.stderr)
        print(f"  states           {len(states_in_data):>2d}   "
              f"({', '.join(states_in_data)})", file=sys.stderr)
        print(f"  elections        {len(state_cycles):>2d}   "
              f"(one per (state, cycle))", file=sys.stderr)
        print(f"  constituencies   {const_total:>2d}   "
              f"(per-state, post-normalization)", file=sys.stderr)
        print(f"  parties          {len(parties):>2d}   (deduplicated party names)",
              file=sys.stderr)
        # Politicians: per-state (state, norm_name, constituency)
        all_keys = set()
        for (state_name, _yr), rows in rows_by_state_cycle.items():
            for r in rows:
                all_keys.add((state_name,
                               _normalize_name(r["candidate_name"]),
                               r["constituency"]))
        print(f"  politicians (approx): {len(all_keys):>3d}   "
              f"(per-state, cross-cycle deduped)",
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

    # ------- Populate states (one per unique state in provisional) -----
    # State code mapping: prefer the well-known ISO-like 2-letter codes.
    # Falls back to first two letters of the state name uppercased for
    # unmapped states; this is fine until we hit two states starting
    # with the same letter.
    _STATE_CODES = {
        "Delhi":             "DL",
        "Punjab":            "PB",
        "Bihar":             "BR",
        "Goa":               "GA",
        "Haryana":           "HR",
        "Karnataka":         "KA",
        "Maharashtra":       "MH",
        "Tamil Nadu":        "TN",
        "Uttar Pradesh":     "UP",
        "Puducherry":        "PY",
        "Sikkim":            "SK",
        "Mizoram":           "MZ",
        "Manipur":           "MN",
        "Tripura":           "TR",
        "Meghalaya":         "ML",
        "Nagaland":          "NL",
        "Arunachal Pradesh": "AR",
        "Uttarakhand":       "UK",
        "Himachal Pradesh":  "HP",
        "Jammu and Kashmir": "JK",
        "Chhattisgarh":      "CG",
        "Jharkhand":         "JH",
        "Rajasthan":         "RJ",
        "Madhya Pradesh":    "MP",
        "Gujarat":           "GJ",
        "Odisha":            "OD",
        "West Bengal":       "WB",
        "Andhra Pradesh":    "AP",
        "Telangana":         "TS",
        "Kerala":            "KL",
        "Assam":             "AS",
    }
    state_id_by_name: dict[str, int] = {}
    for i, state_name in enumerate(states_in_data, 1):
        code = _STATE_CODES.get(state_name, state_name[:2].upper())
        cur.execute(
            "INSERT INTO states (id, name, code) VALUES (?, ?, ?)",
            (i, state_name, code),
        )
        state_id_by_name[state_name] = i
    print(f"  States inserted: {len(states_in_data)} "
          f"({', '.join(states_in_data)})", file=sys.stderr)

    # ------- Populate elections (one per (state, cycle)) ---------------
    # Index by (state_name, year) so the politician/appearance loop can
    # look up the right election_id when joining a row to its election.
    election_id_by_state_year: dict[tuple[str, int], int] = {}
    sorted_state_cycles = sorted(state_cycles)  # deterministic order
    for i, (state_name, year) in enumerate(sorted_state_cycles, 1):
        cur.execute(
            "INSERT INTO elections (id, year, house, state_id, myneta_slug) "
            "VALUES (?, ?, 'Assembly', ?, ?)",
            (i, year, state_id_by_name[state_name],
             f"{state_name.lower().replace(' ', '')}{year}"),
        )
        election_id_by_state_year[(state_name, year)] = i
    print(f"  Elections inserted: {len(sorted_state_cycles)} "
          f"(one per state-cycle)", file=sys.stderr)

    # ------- Populate constituencies (per-state, dedup across cycles) --
    # Normalize spellings so '(SC)' suffixes, dotted initials, and
    # whitespace variants all map to one row. Canonical name = the
    # MOST RECENT cycle's spelling. Constituencies are per-state, so we
    # build the canonical-by-norm map per state separately — same
    # constituency name in two states (e.g. "PATIALA" can exist in
    # Punjab as a real constituency; not collide with anything else)
    # gets two separate constituency rows.
    const_id_by_state_norm: dict[tuple[str, str], int] = {}
    next_const_id = 1
    for state_name in states_in_data:
        state_years = sorted({y for s, y in state_cycles if s == state_name},
                              reverse=True)
        canonical_by_norm: dict[str, str] = {}
        for year in state_years:
            # Iterate latest cycle first; first writer wins
            for r in rows_by_state_cycle[(state_name, year)]:
                const_raw = r.get("constituency") or ""
                norm = _normalize_constituency(const_raw)
                if norm and norm not in canonical_by_norm:
                    display = const_raw
                    for suf in ("(SC)", "(ST)"):
                        if display.endswith(suf):
                            display = display[: -len(suf)].strip()
                    canonical_by_norm[norm] = display
        for norm, name in sorted(canonical_by_norm.items(),
                                   key=lambda kv: kv[1]):
            cur.execute(
                "INSERT INTO constituencies (id, name, state_id, house) "
                "VALUES (?, ?, ?, ?)",
                (next_const_id, name, state_id_by_name[state_name],
                 "Assembly"),
            )
            const_id_by_state_norm[(state_name, norm)] = next_const_id
            next_const_id += 1
        print(f"  Constituencies for {state_name}: "
              f"{len(canonical_by_norm)}", file=sys.stderr)

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
    dup_appearances_skipped = 0
    # politician_id keyed by (state_name, normalized_name, normalized_constituency)
    # — state must be part of the key so a 'Manpreet Singh' in Punjab
    # doesn't merge with a 'Manpreet Singh' in Delhi.
    pol_id_by_key: dict[tuple, int] = {}
    # Track (politician_id, election_id) pairs we've already inserted so
    # we don't violate the UNIQUE(politician_id, election_id) constraint.
    # This happens when two raw rows (e.g. 'ARVIND KEJRIWAL' vs
    # 'ARVIND KEJRI WAL' — OCR spacing artifact) survive the per-cycle
    # raw-name dedup but collide under our normalized-name match key.
    # We keep the first one and silently drop subsequent dupes; the
    # 'first one' has the highest affidavit_id by construction (the
    # earlier dedup step prefers max aff_id).
    seen_appearance: set[tuple[int, int]] = set()

    # Iterate per state, then per cycle within state (latest cycle first).
    # The cross-cycle matching only ever fires within a single state.
    for state_name in states_in_data:
        state_years = sorted({y for s, y in state_cycles if s == state_name},
                              reverse=True)
        state_lower = state_name.lower().replace(" ", "")
        for year in state_years:
            for r in rows_by_state_cycle[(state_name, year)]:
                const_raw = r.get("constituency") or ""
                const_norm = _normalize_constituency(const_raw)
                const_id = const_id_by_state_norm.get(
                    (state_name, const_norm))
                if not const_id:
                    skipped_no_constituency += 1
                    continue

                norm_name = _normalize_name(r["candidate_name"])
                # Match by (state, normalized_name, normalized_constituency)
                # so a 2020 candidate in 'AMBEDKAR NAGAR(SC)' matches their
                # 2025 entry in 'AMBEDKAR NAGAR' but Manpreet Singh in
                # Punjab does NOT merge with Manpreet Singh in Delhi.
                match_key = (state_name, norm_name, const_norm)

                if match_key in pol_id_by_key:
                    # Same candidate seen in a later cycle already — reuse id.
                    # NO new politician insert; we just attach an
                    # additional election_appearance below.
                    pol_id = pol_id_by_key[match_key]
                    cross_cycle_matches += 1
                else:
                    # New politician — generate unique slug and insert.
                    base = (
                        f"{_slugify(r['candidate_name'])}"
                        f"-{state_lower}{year}-{r['affidavit_id'] or ''}"
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
                election_id = election_id_by_state_year[(state_name, year)]
                source_url = "https://affidavit.eci.gov.in/"

                # Skip if we've already inserted an appearance for this
                # (politician, election) — see seen_appearance comment above.
                appearance_key = (pol_id, election_id)
                if appearance_key in seen_appearance:
                    dup_appearances_skipped += 1
                    continue
                seen_appearance.add(appearance_key)

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
    print(f"  States loaded:                  {len(states_in_data)} "
          f"({', '.join(states_in_data)})", file=sys.stderr)
    print(f"  Cycles loaded:                  {len(state_cycles)} "
          f"(across all states)", file=sys.stderr)
    for st in states_in_data:
        yrs = sorted({y for s, y in state_cycles if s == st})
        print(f"    {st}: {', '.join(str(y) for y in yrs)}",
              file=sys.stderr)
    print(f"  Politicians inserted:           {pol_inserted}", file=sys.stderr)
    print(f"  Cross-cycle matches reused:     {cross_cycle_matches} "
          f"(same person, multiple cycles)", file=sys.stderr)
    print(f"  Election appearances inserted:  {appear_inserted}", file=sys.stderr)
    print(f"  Skipped (no constituency):      {skipped_no_constituency}",
          file=sys.stderr)
    print(f"  Skipped (duplicate per election): {dup_appearances_skipped} "
          f"(OCR spelling variants merged)", file=sys.stderr)
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
