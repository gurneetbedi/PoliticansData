"""
Load Haryana election results (winners + vote counts) from Wikipedia.

Sibling to the other state loaders.

Reads:
    https://en.wikipedia.org/wiki/2024_Haryana_Legislative_Assembly_election
    https://en.wikipedia.org/wiki/2019_Haryana_Legislative_Assembly_election
    https://en.wikipedia.org/wiki/2014_Haryana_Legislative_Assembly_election

Haryana naming: standard North Indian patterns (Jat surnames, Punjabi
surnames, common Hindu first-name conventions). Standard normalizer
handles all cases.
"""
from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parent.parent
DB_PATH      = PROJECT_ROOT / "politrack.db"
RESULTS_DIR  = PROJECT_ROOT / "data/eci/results"
STATE_NAME   = "Haryana"

WIKI_URLS = {
    2024: "https://en.wikipedia.org/wiki/2024_Haryana_Legislative_Assembly_election",
    2019: "https://en.wikipedia.org/wiki/2019_Haryana_Legislative_Assembly_election",
    2014: "https://en.wikipedia.org/wiki/2014_Haryana_Legislative_Assembly_election",
}

USER_AGENT = (
    "Lokvani/0.1 (open-source civic transparency; "
    "contact: gurneet.bedi@me.com) Python-requests"
)


# Filled in after --dump-tables diagnostic on 2026-06-27.
# Table 14 is the master results table for 2019. Layout is identical
# to Goa — no District column, no Turnout column, single margin cell.
# All data rows are uniform 14 cells (no rowspan wrinkles).
HARYANA_2024_COLS = {
    # Filled in after --dump-tables diagnostic on 2026-06-27.
    # Table 11 is the master results table. Layout matches Puducherry
    # 2021 / Sikkim 2019 — has a Turnout% column, 14-cell rows, single
    # margin cell. District subheader rows have 1 cell each and are
    # filtered out automatically.
    "table_index": 11,
    "header_rows": 2,
    "cols": {
        "constituency": -13,   # 'Kalka' / 'Panchkula' / etc.
        # -12 = turnout % (ignored)
        "winner_name":  -11,
        # -10 = empty party color box
        "winner_party":  -9,
        "winner_votes":  -8,
        "winner_pct":    -7,
        "runner_name":   -6,
        # -5 = empty party color box
        "runner_party":  -4,
        "runner_votes":  -3,
        "runner_pct":    -2,
        # -1 = margin (ignored)
    },
}
HARYANA_2019_COLS = {
    "table_index": 14,
    "header_rows": 2,
    "cols": {
        "constituency": -13,   # 'Kalka' / 'Panchkula' / etc.
        "winner_name":  -12,
        # -11 = empty party color box
        "winner_party": -10,
        "winner_votes":  -9,
        "winner_pct":    -8,
        "runner_name":   -7,
        # -6 = empty party color box
        "runner_party":  -5,
        "runner_votes":  -4,
        "runner_pct":    -3,
        # -2 = margin votes, -1 = margin % (both ignored)
    },
}
HARYANA_2014_COLS = {"table_index": None, "header_rows": 2, "cols": {}}

COL_MAPS = {
    2024: HARYANA_2024_COLS,
    2019: HARYANA_2019_COLS,
    2014: HARYANA_2014_COLS,
}


def _normalize_name(name: str) -> str:
    if not name:
        return ""
    s = name.upper().strip()
    for prefix in ("DR. ", "DR ", "ADV. ", "ADV ", "ADVOCATE ",
                    "SHRI ", "SHRIMATI ", "SMT. ", "SMT ",
                    "MR. ", "MR ", "MS. ", "MS ", "MRS. ", "MRS ",
                    "PROF. ", "PROF ", "CH. ", "CH ", "CHAUDHARY ",
                    "PANDIT ", "PT. ", "PT "):
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
    for suf in ("(SC)", "(ST)", " SC", " ST"):
        if s.endswith(suf):
            s = s[: -len(suf)].strip()
    s = re.sub(r"[^A-Z0-9]+", "", s)
    return s


def fetch_wiki_html(year: int, refetch: bool = False) -> str:
    cache_path = RESULTS_DIR / f"_wiki_haryana_{year}.html"
    if cache_path.exists() and not refetch:
        return cache_path.read_text()
    try:
        import requests
    except ImportError:
        sys.exit("pip install requests beautifulsoup4 rapidfuzz")
    print(f"Fetching {WIKI_URLS[year]} ...", file=sys.stderr)
    r = requests.get(WIKI_URLS[year], headers={"User-Agent": USER_AGENT},
                      timeout=30)
    r.raise_for_status()
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    cache_path.write_text(r.text)
    return r.text


def _cell_text(td) -> str:
    for sup in td.find_all("sup"):
        sup.decompose()
    return td.get_text(" ", strip=True)


def _extract_int(text: str) -> int | None:
    if not text:
        return None
    m = re.search(r"\d[\d,]*", text)
    if not m:
        return None
    try:
        return int(m.group(0).replace(",", ""))
    except ValueError:
        return None


def _extract_pct(text: str) -> float | None:
    if not text:
        return None
    m = re.search(r"(\d+(?:\.\d+)?)\s*%", text)
    if not m:
        return None
    try:
        return float(m.group(1))
    except ValueError:
        return None


def dump_tables(html: str) -> None:
    from bs4 import BeautifulSoup
    soup = BeautifulSoup(html, "html.parser")
    for ti, table in enumerate(soup.find_all("table")):
        rows = table.find_all("tr")
        if len(rows) < 5:
            continue
        header_cells = [_cell_text(c)[:32]
                         for c in rows[0].find_all(["th", "td"])]
        print(f"  Table {ti}: {len(rows)} rows, header[0] ({len(header_cells)} cells): "
              f"{header_cells}", file=sys.stderr)
        if len(rows) > 1:
            h2 = [_cell_text(c)[:32]
                  for c in rows[1].find_all(["th", "td"])]
            print(f"             header[1] ({len(h2)}): {h2}",
                  file=sys.stderr)
        for di in (2, 3, 4):
            if di >= len(rows):
                break
            sample = [_cell_text(c)[:32]
                      for c in rows[di].find_all(["th", "td"])]
            print(f"             row[{di}] ({len(sample)}): {sample}",
                  file=sys.stderr)
        print(file=sys.stderr)


def parse_candidate_list_haryana_2024(html: str,
                                        expected: set[str]) -> dict:
    """Parse Wikipedia Table 5 for Haryana 2024 to get all major-party
    candidates per constituency (BJP/INC/INLD/JJP/BSP/etc.). Returns a
    dict {constituency_norm: [{name, party}, ...]}.

    Table 5 layout (variable-length rows):
      - First row of each district has 13-15 cells: district, #, name,
        then (color, party, candidate) triples for each contesting party.
      - Subsequent rows in a district have 12-14 cells (no district cell
        due to rowspan).

    We detect the row shape by whether cell[0] is numeric (subsequent
    row) or alphabetic (first-of-district row).
    """
    from bs4 import BeautifulSoup
    soup = BeautifulSoup(html, "html.parser")
    tables = soup.find_all("table")
    if 5 >= len(tables):
        print(f"  ⚠ Table 5 not present (only {len(tables)} tables)",
              file=sys.stderr)
        return {}
    table = tables[5]
    rows = table.find_all("tr")

    result: dict[str, list[dict]] = {}
    for row in rows[1:]:   # skip the header row
        cells = row.find_all(["th", "td"])
        cells_text = [_cell_text(c) for c in cells]
        if len(cells_text) < 5:
            # Skip short subheaders like the row that only has 4 cells
            continue

        # Detect row shape: first-of-district has non-numeric cell[0]
        if cells_text[0].isdigit():
            # Subsequent row: [#, ConstName, then party triples]
            const_name  = cells_text[1]
            party_start = 2
        else:
            # First-of-district: [District, #, ConstName, then party triples]
            const_name  = cells_text[2]
            party_start = 3

        const_norm = _normalize_constituency(const_name)
        if const_norm not in expected:
            continue

        # Parse (color, party, name) triples for the rest of the row
        candidates = []
        i = party_start
        while i + 2 < len(cells_text):
            # cells[i]     = party-color box (usually empty)
            party = cells_text[i + 1]
            name  = cells_text[i + 2]
            if party and name:
                candidates.append({"name": name, "party": party})
            i += 3

        result[const_norm] = candidates

    return result


def merge_haryana_2024_top_3(results: list[dict],
                              extra_candidates: dict) -> list[dict]:
    """Merge extra major-party candidates from Table 5 into the winner/
    runner-up results from Table 11. Adds them as rank=3+ (without vote
    counts, since Table 5 doesn't carry them). Skips any name that's
    already ranked 1 or 2 (fuzzy match via normalized name)."""
    try:
        from rapidfuzz import fuzz
    except ImportError:
        return results  # graceful degrade — no merge if lib missing

    def _norm(s):
        return _normalize_name(s)

    added = 0
    for row in results:
        const_norm = row.get("constituency_norm", "")
        extras = extra_candidates.get(const_norm, [])
        if not extras:
            continue
        existing_norms = {_norm(c.get("name", ""))
                          for c in row.get("candidates", [])}
        next_rank = max((c.get("rank", 0)
                         for c in row.get("candidates", [])), default=2) + 1
        for ec in extras:
            ec_norm = _norm(ec["name"])
            if not ec_norm:
                continue
            # Skip if fuzzily matches any existing name
            hit = False
            for en in existing_norms:
                if fuzz.token_set_ratio(ec_norm, en) >= 85:
                    hit = True
                    break
            if hit:
                continue
            row.setdefault("candidates", []).append({
                "rank":            next_rank,
                "is_winner":       False,
                "name":            ec["name"],
                "party_raw":       ec["party"],
                "votes":           None,
                "vote_share_pct":  None,
            })
            existing_norms.add(ec_norm)
            next_rank += 1
            added += 1
    print(f"  Merged {added} extra rank-3+ candidates from Table 5",
          file=sys.stderr)
    return results


def parse_with_colmap(html: str, year: int,
                       expected: set[str]) -> list[dict]:
    cfg = COL_MAPS.get(year, {})
    if cfg.get("table_index") is None:
        print(f"  ⚠ No column map for {year} yet. Run --dump-tables first,",
              file=sys.stderr)
        print(f"    update HARYANA_{year}_COLS in this script, then re-run.",
              file=sys.stderr)
        return []

    from bs4 import BeautifulSoup
    soup = BeautifulSoup(html, "html.parser")
    tables = soup.find_all("table")
    ti = cfg["table_index"]
    if ti >= len(tables):
        print(f"  ⚠ table_index={ti} out of range (only {len(tables)} tables)",
              file=sys.stderr)
        return []
    table = tables[ti]
    rows = table.find_all("tr")
    header_rows = cfg.get("header_rows", 1)
    data_rows = rows[header_rows:]
    cols = cfg["cols"]
    if not cols:
        return []

    positives = [v for v in cols.values() if v >= 0]
    negatives = [v for v in cols.values() if v < 0]
    min_len_pos = (max(positives) + 1) if positives else 0
    min_len_neg = abs(min(negatives)) if negatives else 0
    min_len = max(min_len_pos, min_len_neg)

    results: list[dict] = []
    skipped = 0
    for row in data_rows:
        cells = row.find_all(["th", "td"])
        if len(cells) < min_len:
            skipped += 1
            continue
        try:
            const = _cell_text(cells[cols["constituency"]])
        except (KeyError, IndexError):
            skipped += 1
            continue
        const_norm = _normalize_constituency(const)
        if not const_norm or const_norm not in expected:
            skipped += 1
            continue

        def _get(name):
            i = cols.get(name)
            if i is None:
                return ""
            try:
                return _cell_text(cells[i])
            except IndexError:
                return ""

        winner_name  = _get("winner_name")
        winner_party = _get("winner_party")
        winner_votes = _extract_int(_get("winner_votes"))
        winner_pct   = _extract_pct(_get("winner_pct"))
        runner_name  = _get("runner_name")
        runner_party = _get("runner_party")
        runner_votes = _extract_int(_get("runner_votes"))
        runner_pct   = _extract_pct(_get("runner_pct"))

        cands = []
        if winner_name:
            cands.append({
                "rank": 1, "is_winner": True,
                "name": winner_name, "party_raw": winner_party,
                "votes": winner_votes, "vote_share_pct": winner_pct,
            })
        if runner_name:
            cands.append({
                "rank": 2, "is_winner": False,
                "name": runner_name, "party_raw": runner_party,
                "votes": runner_votes, "vote_share_pct": runner_pct,
            })
        if not cands:
            skipped += 1
            continue
        results.append({
            "constituency_raw": const,
            "constituency_norm": const_norm,
            "candidates": cands,
        })

    print(f"  Parsed {len(results)} constituencies "
          f"({skipped} rows skipped/unrecognized)", file=sys.stderr)
    return results


def build_candidate_lookup(cur: sqlite3.Cursor, year: int) -> dict:
    cur.execute("""
        SELECT ea.id, p.name, c.name, par.short_name
        FROM election_appearances ea
        JOIN politicians  p  ON ea.politician_id  = p.id
        JOIN constituencies c ON ea.constituency_id = c.id
        JOIN elections     e ON ea.election_id    = e.id
        JOIN states        s ON c.state_id        = s.id
        LEFT JOIN parties par ON ea.party_id      = par.id
        WHERE e.year = ? AND s.name = ?
    """, (year, STATE_NAME))
    out = {}
    for app_id, name, const, party in cur.fetchall():
        key = (_normalize_constituency(const), _normalize_name(name))
        out[key] = (app_id, name, party)
    return out


def find_match(wiki_name: str, const_norm: str, lookup: dict,
                threshold: int = 75) -> tuple | None:
    try:
        from rapidfuzz import fuzz
    except ImportError:
        sys.exit("pip install rapidfuzz")

    wiki_norm = _normalize_name(wiki_name)
    if not wiki_norm:
        return None

    key = (const_norm, wiki_norm)
    if key in lookup:
        app_id, db_name, party = lookup[key]
        return (app_id, db_name, party, 100)

    best_score = 0
    best_match = None
    for (c, n), (app_id, db_name, party) in lookup.items():
        if c != const_norm:
            continue
        scores = [
            fuzz.partial_ratio(wiki_norm, n),
            fuzz.token_set_ratio(wiki_norm, n),
            fuzz.token_sort_ratio(wiki_norm, n),
        ]
        score = max(scores)
        if score > best_score:
            best_score = score
            best_match = (app_id, db_name, party, score)
    if best_match and best_score >= threshold:
        return best_match
    return None


def main():
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument("--year", type=int, default=0,
                    help="2024, 2019, or 2014. Default: all three.")
    ap.add_argument("--refetch", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--dump-tables", action="store_true")
    ap.add_argument("--match-threshold", type=int, default=75)
    ap.add_argument("--db", default=str(DB_PATH))
    args = ap.parse_args()

    if not Path(args.db).exists():
        sys.exit(f"DB not found: {args.db}")

    years = [args.year] if args.year else [2024, 2019, 2014]
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)

    con = sqlite3.connect(args.db)
    cur = con.cursor()

    cur.execute("""
        SELECT c.name FROM constituencies c
        JOIN states s ON c.state_id = s.id
        WHERE s.name = ?
    """, (STATE_NAME,))
    expected = {_normalize_constituency(r[0]) for r in cur.fetchall()}
    print(f"Haryana constituencies in DB: {len(expected)}", file=sys.stderr)
    if not expected:
        sys.exit("No Haryana constituencies in DB. "
                 "Run migrate_to_eci_only.py first.")

    grand_total_matched = 0
    grand_total_unmatched = 0

    for year in years:
        print(f"\n========== Haryana {year} ==========", file=sys.stderr)

        if args.dump_tables:
            html = fetch_wiki_html(year, refetch=args.refetch)
            dump_tables(html)
            continue

        results_path = RESULTS_DIR / f"haryana_{year}_results.json"
        if results_path.exists() and not args.refetch:
            print(f"  Using cached parse: {results_path.name}", file=sys.stderr)
            results = json.loads(results_path.read_text())
        else:
            html = fetch_wiki_html(year, refetch=args.refetch)
            print(f"  Parsing constituency results ...", file=sys.stderr)
            results = parse_with_colmap(html, year, expected)

            # Year-specific enhancement: for 2024, also parse Table 5
            # (candidate list per party) so we have rank-3+ candidates
            # from strong 3rd-party finishers (INLD, JJP, BSP, IND).
            # This lets --top-n 3 in build_top_n_allowlist.py actually
            # add data on interesting 3rd-place candidates.
            if year == 2024:
                print(f"  Enhancing with Table 5 (party-candidate list) ...",
                      file=sys.stderr)
                extra = parse_candidate_list_haryana_2024(html, expected)
                results = merge_haryana_2024_top_3(results, extra)

            results_path.write_text(json.dumps(results, indent=2,
                                                 ensure_ascii=False))
            print(f"  Saved {len(results)} constituencies to "
                  f"{results_path.name}", file=sys.stderr)

        if not results:
            print(f"  ⚠ No parsed results for {year} — skipping",
                  file=sys.stderr)
            continue

        lookup = build_candidate_lookup(cur, year)
        print(f"  Candidate lookup: {len(lookup)} Haryana appearances for {year}",
              file=sys.stderr)

        winners_set = 0
        runnersup_set = 0
        unmatched: list[tuple] = []

        for const_row in results:
            const_norm = const_row["constituency_norm"]
            const_raw  = const_row["constituency_raw"]
            for cand in const_row["candidates"]:
                match = find_match(cand["name"], const_norm, lookup,
                                     args.match_threshold)
                if not match:
                    unmatched.append((const_raw, cand["name"],
                                       cand.get("votes"), cand["rank"]))
                    continue
                app_id, db_name, party, score = match
                cand["_matched"] = {
                    "appearance_id":  app_id,
                    "db_name":        db_name,
                    "db_party":       party,
                    "fuzzy_score":    score,
                }
                if not args.dry_run:
                    cur.execute("""
                        UPDATE election_appearances
                        SET won = ?,
                            votes_received = ?,
                            vote_share_pct = ?
                        WHERE id = ?
                    """, (
                        bool(cand.get("is_winner")),
                        cand.get("votes"),
                        cand.get("vote_share_pct"),
                        app_id,
                    ))
                if cand.get("is_winner"):
                    winners_set += 1
                else:
                    runnersup_set += 1

        if not args.dry_run:
            results_path.write_text(json.dumps(results, indent=2,
                                                 ensure_ascii=False))
            con.commit()

        print(f"  Winners set:    {winners_set}", file=sys.stderr)
        print(f"  Runners-up set: {runnersup_set}", file=sys.stderr)
        print(f"  Unmatched:      {len(unmatched)}", file=sys.stderr)
        grand_total_matched += winners_set + runnersup_set
        grand_total_unmatched += len(unmatched)

        if unmatched:
            print(f"  Sample unmatched (first 10):", file=sys.stderr)
            for const, name, votes, rank in unmatched[:10]:
                print(f"    [{rank}] {const:22s}  {name!r}  votes={votes}",
                      file=sys.stderr)
            if len(unmatched) > 10:
                print(f"    ... and {len(unmatched) - 10} more",
                      file=sys.stderr)

    con.close()

    print(f"\n========== TOTAL ==========", file=sys.stderr)
    print(f"  Matched + applied:  {grand_total_matched}", file=sys.stderr)
    print(f"  Unmatched:          {grand_total_unmatched}", file=sys.stderr)
    if args.dry_run:
        print(f"  (dry-run — no DB writes)", file=sys.stderr)


if __name__ == "__main__":
    main()
