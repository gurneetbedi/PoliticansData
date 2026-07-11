"""
Load Punjab election results (winners + vote counts) from Wikipedia and
apply them to election_appearances.

Sibling to load_delhi_election_results.py — same shape, Punjab-specific
table parser. Reads:
    https://en.wikipedia.org/wiki/2022_Punjab_Legislative_Assembly_election
    https://en.wikipedia.org/wiki/2017_Punjab_Legislative_Assembly_election

Filters the DB candidate lookup to state='Punjab' so a 'Manpreet Singh' in
Patiala (Punjab) is never accidentally matched against a 'Manpreet Singh'
in some Delhi constituency.

FIRST-RUN WORKFLOW
==================
The Punjab Wikipedia tables have not been profiled yet — we don't know
the column layout. Run with --dump-tables first to see what's there:

    python scripts/load_punjab_election_results.py --year 2022 --dump-tables

That prints every wikitable's header row + a sample data row. Pick the
constituency-results table, note its column positions, then update the
PUNJAB_2022_COLS dict below. Re-run without --dump-tables.

USAGE
=====
    pip install requests beautifulsoup4 rapidfuzz

    # First time — see table structure to fill in column map
    python scripts/load_punjab_election_results.py --year 2022 --dump-tables

    # Dry-run after tuning column map
    python scripts/load_punjab_election_results.py --year 2022 --dry-run

    # Real run
    python scripts/load_punjab_election_results.py --year 2022
"""
from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
import time
from pathlib import Path


# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DB_PATH      = PROJECT_ROOT / "lokvani.db"
RESULTS_DIR  = PROJECT_ROOT / "data/eci/results"
STATE_NAME   = "Punjab"

WIKI_URLS = {
    2022: "https://en.wikipedia.org/wiki/2022_Punjab_Legislative_Assembly_election",
    2017: "https://en.wikipedia.org/wiki/2017_Punjab_Legislative_Assembly_election",
}

USER_AGENT = (
    "Lokvani/0.1 (open-source civic transparency; "
    "contact: gurneet.bedi@me.com) "
    "Python-requests"
)


# ---------------------------------------------------------------------------
# Column maps per cycle — fill in after running --dump-tables once.
# Format: {"table_index": int, "header_rows": int, "cols": {field: idx, ...}}
# Set table_index = None to disable the parser (until you've tuned it).
# ---------------------------------------------------------------------------

# Filled in after dump-tables diagnostic on 2026-06-26.
# Wikipedia's Table 24 is the master per-constituency results table.
# It has a two-row header (Constituency / Winner / Runner-up grouping
# in row 0, then Candidate/Party/Votes/% subheaders in row 1) plus
# district subheader rows interleaved with the data rows. Those
# subheader rows have only 1 cell and are filtered out automatically
# because the parser requires len(cells) > max(col_index).
PUNJAB_2022_COLS = {
    "table_index": 24,
    "header_rows": 2,
    "cols": {
        "constituency":   1,
        # cols 2 = turnout % (ignored)
        "winner_name":    3,
        # col 4 = empty party-color box
        "winner_party":   5,
        "winner_votes":   6,
        "winner_pct":     7,
        "runner_name":    8,
        # col 9 = empty party-color box
        "runner_party":  10,
        "runner_votes":  11,
        "runner_pct":    12,
        # cols 13/14 = margin votes / margin % (ignored)
    },
}

PUNJAB_2017_COLS = {
    "table_index": None,
    "header_rows": 1,
    "cols": {},
}

COL_MAPS = {
    2022: PUNJAB_2022_COLS,
    2017: PUNJAB_2017_COLS,
}


# ---------------------------------------------------------------------------
# Normalization — match migrate_to_eci_only.py exactly
# ---------------------------------------------------------------------------

def _normalize_name(name: str) -> str:
    if not name:
        return ""
    s = name.upper().strip()
    for prefix in ("DR. ", "DR ", "ADV. ", "ADV ", "ADVOCATE ",
                    "SHRI ", "SHRIMATI ", "SMT. ", "SMT ",
                    "MR. ", "MR ", "MS. ", "MS ", "MRS. ", "MRS ",
                    "S. ", "SARDAR ", "BIBI ", "PROF. ", "PROF "):
        if s.startswith(prefix):
            s = s[len(prefix):]
    for marker in (" S/O ", " D/O ", " W/O ", " S.O ", " D.O ", " W.O "):
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


# ---------------------------------------------------------------------------
# HTML fetching
# ---------------------------------------------------------------------------

def fetch_wiki_html(year: int, refetch: bool = False) -> str:
    cache_path = RESULTS_DIR / f"_wiki_punjab_{year}.html"
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


# ---------------------------------------------------------------------------
# Diagnostic: dump every table's headers + first data row so we can pick
# the right one and figure out column positions.
# ---------------------------------------------------------------------------

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
        # Probably a second header row
        if len(rows) > 1:
            h2 = [_cell_text(c)[:32]
                  for c in rows[1].find_all(["th", "td"])]
            print(f"             header[1] ({len(h2)}): {h2}",
                  file=sys.stderr)
        # First few data rows
        for di in (2, 3, 4):
            if di >= len(rows):
                break
            sample = [_cell_text(c)[:32]
                      for c in rows[di].find_all(["th", "td"])]
            print(f"             row[{di}] ({len(sample)}): {sample}",
                  file=sys.stderr)
        print(file=sys.stderr)


# ---------------------------------------------------------------------------
# Generic parser — reads PUNJAB_<YEAR>_COLS to pick table + columns.
# Returns list of {constituency_raw, constituency_norm, candidates: [...]}
# ---------------------------------------------------------------------------

def parse_with_colmap(html: str, year: int,
                       expected: set[str]) -> list[dict]:
    cfg = COL_MAPS.get(year, {})
    if cfg.get("table_index") is None:
        print(f"  ⚠ No column map for {year} yet. Run --dump-tables first,",
              file=sys.stderr)
        print(f"    update PUNJAB_{year}_COLS in this script, then re-run.",
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

    results: list[dict] = []
    skipped = 0
    for row in data_rows:
        cells = row.find_all(["th", "td"])
        if len(cells) <= max(cols.values()):
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
            return _cell_text(cells[i]) if i is not None and i < len(cells) else ""

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


# ---------------------------------------------------------------------------
# DB lookup + matching — filtered to state='Punjab'
# ---------------------------------------------------------------------------

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


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument("--year", type=int, default=0,
                    help="2022 or 2017. Default: both.")
    ap.add_argument("--refetch", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--dump-tables", action="store_true",
                    help="Show every table's structure (for tuning col maps).")
    ap.add_argument("--match-threshold", type=int, default=75)
    ap.add_argument("--db", default=str(DB_PATH))
    args = ap.parse_args()

    if not Path(args.db).exists():
        sys.exit(f"DB not found: {args.db}")

    years = [args.year] if args.year else [2022, 2017]
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)

    con = sqlite3.connect(args.db)
    cur = con.cursor()

    # Known Punjab constituency set (filters out non-results tables)
    cur.execute("""
        SELECT c.name
        FROM constituencies c
        JOIN states s ON c.state_id = s.id
        WHERE s.name = ?
    """, (STATE_NAME,))
    expected = {_normalize_constituency(r[0]) for r in cur.fetchall()}
    print(f"Punjab constituencies in DB: {len(expected)}", file=sys.stderr)
    if not expected:
        sys.exit("No Punjab constituencies in DB. Run "
                 "migrate_to_eci_only.py with Punjab data in provisional first.")

    grand_total_matched = 0
    grand_total_unmatched = 0

    for year in years:
        print(f"\n========== Punjab {year} ==========", file=sys.stderr)

        if args.dump_tables:
            html = fetch_wiki_html(year, refetch=args.refetch)
            dump_tables(html)
            continue

        # Cache parsed results
        results_path = RESULTS_DIR / f"punjab_{year}_results.json"
        if results_path.exists() and not args.refetch:
            print(f"  Using cached parse: {results_path.name}",
                  file=sys.stderr)
            results = json.loads(results_path.read_text())
        else:
            html = fetch_wiki_html(year, refetch=args.refetch)
            print(f"  Parsing constituency results ...", file=sys.stderr)
            results = parse_with_colmap(html, year, expected)
            results_path.write_text(json.dumps(results, indent=2,
                                                 ensure_ascii=False))
            print(f"  Saved {len(results)} constituencies to "
                  f"{results_path.name}", file=sys.stderr)

        if not results:
            print(f"  ⚠ No parsed results for {year} — skipping",
                  file=sys.stderr)
            continue

        lookup = build_candidate_lookup(cur, year)
        print(f"  Candidate lookup: {len(lookup)} Punjab appearances for {year}",
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
