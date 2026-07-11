"""
Load Delhi 2020 + 2025 election results (winners + vote counts) from Wikipedia
and apply them to election_appearances.

WHAT IT DOES
============
1. Fetches Wikipedia's results pages for both Delhi cycles:
     - https://en.wikipedia.org/wiki/2020_Delhi_Legislative_Assembly_election
     - https://en.wikipedia.org/wiki/2025_Delhi_Legislative_Assembly_election

2. Parses the per-constituency results table. Two table shapes are
   common on these pages — we handle both:
     SHAPE A: one row per constituency, with winner + runner-up in
              separate columns ("Constituency | Winner | Party | Votes |
              Runner-up | Party | Votes | Margin")
     SHAPE B: one row per CANDIDATE, with constituency repeated
              (rare on Indian election pages but exists for some)

3. Saves the raw parsed data to:
     data/eci/results/delhi_<year>_results.json
   for inspection / audit / re-use without re-hitting Wikipedia.

4. Fuzzy-matches Wikipedia names against our `politicians` table
   using rapidfuzz partial-ratio ≥ 88. Uses the same normalization
   helpers as migrate_to_eci_only.py (S/O suffix stripping, honorific
   removal, etc.).

5. UPDATEs `election_appearances`:
     - won = True for the highest-vote candidate per constituency
     - votes_received = the vote count
     - vote_share_pct = % share when listed

6. Prints stats: matches, unmatched, per-cycle winner counts.

USAGE
=====
    pip install requests beautifulsoup4 rapidfuzz

    # Dry-run first (no DB writes; shows match quality)
    python scripts/load_delhi_election_results.py --dry-run

    # Real run
    python scripts/load_delhi_election_results.py

    # One cycle only
    python scripts/load_delhi_election_results.py --year 2025

    # Force re-fetch from Wikipedia (default uses cached JSON if present)
    python scripts/load_delhi_election_results.py --refetch

GIT-IGNORED
===========
The raw cached Wikipedia JSONs land in data/eci/results/, gitignored
the same way as the raw PDFs (they're regenerable, large-ish, and the
DB they produce is the actual source of truth for the app).
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

WIKI_URLS = {
    2020: "https://en.wikipedia.org/wiki/2020_Delhi_Legislative_Assembly_election",
    2025: "https://en.wikipedia.org/wiki/2025_Delhi_Legislative_Assembly_election",
}

USER_AGENT = (
    "Lokvani/0.1 (open-source civic transparency; "
    "contact: gurneet.bedi@me.com) "
    "Python-requests"
)


# ---------------------------------------------------------------------------
# Normalization — match migrate_to_eci_only.py exactly
# ---------------------------------------------------------------------------

def _normalize_name(name: str) -> str:
    if not name:
        return ""
    s = name.upper().strip()
    for prefix in ("DR. ", "DR ", "ADV. ", "ADV ", "ADVOCATE ",
                    "SHRI ", "SHRIMATI ", "SMT. ", "SMT ",
                    "MR. ", "MR ", "MS. ", "MS ", "MRS. ", "MRS "):
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
    _ALIASES = {"NARELA": "NERELA"}
    return _ALIASES.get(s, s)


# ---------------------------------------------------------------------------
# Wikipedia fetch + parse
# ---------------------------------------------------------------------------

def fetch_wiki_html(year: int, refetch: bool = False) -> str:
    """Return the HTML for the year's Wikipedia page. Caches to disk."""
    cache_path = RESULTS_DIR / f"_wiki_{year}.html"
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


# Heuristics for parsing the results table. Wikipedia's per-constituency
# table for Indian elections typically has these column names:
_WINNER_COL_HEADERS = {"winner", "winning candidate", "elected member",
                        "winning party", "candidate"}
_RUNNERUP_COL_HEADERS = {"runner-up", "runnerup", "runner up",
                          "second place", "loser"}
_PARTY_COL_HEADERS = {"party"}
_VOTES_COL_HEADERS = {"votes", "vote", "votes received"}
_CONST_COL_HEADERS = {"constituency", "ac", "seat", "constituency name",
                       "no.", "no."}


def _cell_text(td) -> str:
    """Extract clean text from a Wikipedia table cell."""
    # Drop reference superscripts like [1], [a], etc.
    for sup in td.find_all("sup"):
        sup.decompose()
    return td.get_text(" ", strip=True)


def _extract_int(text: str) -> int | None:
    """Pull the first int from a string like '30,088 (45.65%)' → 30088."""
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


def _find_table_with_text(soup, needles: list[str]):
    """Return the first <table> whose visible text contains ALL needles."""
    for table in soup.find_all("table"):
        text = table.get_text(" ", strip=True).lower()
        if all(n.lower() in text for n in needles):
            return table
    return None


def parse_2020(html: str, expected: set[str]) -> list[dict]:
    """Delhi 2020 master 'List of constituencies' table. 15-column layout
    (after accounting for the empty color-box cells Wikipedia inserts
    between candidate name and party):

        col[0]=#                       (serial number)
        col[1]=Constituency name
        col[2]=Turnout %
        col[3]=Winner candidate name
        col[4]=(empty — party color swatch)
        col[5]=Winner party
        col[6]=Winner votes
        col[7]=Winner %
        col[8]=Runner-up candidate name
        col[9]=(empty — color swatch)
        col[10]=Runner-up party
        col[11]=Runner-up votes
        col[12]=Runner-up %
        col[13]=Margin (votes)
        col[14]=Margin %

    Some rows are district separators (1 cell with colspan=14, e.g.
    'North Delhi District'). We skip those by length check.
    """
    from bs4 import BeautifulSoup
    soup = BeautifulSoup(html, "html.parser")
    table = _find_table_with_text(
        soup, ["assembly constituency", "winner", "runner-up", "margin"]
    )
    if table is None:
        print(f"  ⚠ 2020: master 'List of constituencies' table not found",
              file=sys.stderr)
        return []
    rows = table.find_all("tr")
    out = []
    seen_const = set()
    for row in rows:
        cells = row.find_all(["td", "th"])
        # Data rows have 13-15 cells; district separators have 1; header
        # rows have ≤ 10. Anything < 12 is not a data row.
        if len(cells) < 12:
            continue
        cell_texts = [_cell_text(c) for c in cells]
        const_raw = cell_texts[1]
        const_norm = _normalize_constituency(const_raw)
        if const_norm not in expected:
            continue
        if const_norm in seen_const:
            continue
        seen_const.add(const_norm)

        candidates = [
            {
                "name":           cell_texts[3],
                "party":          cell_texts[5],
                "votes":          _extract_int(cell_texts[6]),
                "vote_share_pct": _extract_pct(cell_texts[7]),
                "is_winner":      True,
                "rank":           1,
            },
            {
                "name":           cell_texts[8],
                "party":          cell_texts[10],
                "votes":          _extract_int(cell_texts[11]),
                "vote_share_pct": _extract_pct(cell_texts[12]) if len(cell_texts) > 12 else None,
                "is_winner":      False,
                "rank":           2,
            },
        ]
        out.append({
            "constituency_raw":   const_raw,
            "constituency_norm":  const_norm,
            "candidates":         candidates,
        })
    return out


def parse_2025(html: str, expected: set[str]) -> list[dict]:
    """Delhi 2025 master 'List of constituencies' table. 14-column layout
    (one fewer than 2020 — no Turnout column):

        col[0]=#                       (serial number)
        col[1]=Constituency name
        col[2]=Winner candidate name
        col[3]=(empty — color swatch)
        col[4]=Winner party
        col[5]=Winner votes
        col[6]=Winner %
        col[7]=Runner-up candidate name
        col[8]=(empty — color swatch)
        col[9]=Runner-up party
        col[10]=Runner-up votes
        col[11]=Runner-up %
        col[12]=Margin (votes)
        col[13]=Margin %

    The 2025 page also has a similar 'District' table (4 cols) we want
    to AVOID — we filter by requiring the table contains the 'Winner'
    AND 'Runner-up' AND 'Margin' header texts.
    """
    from bs4 import BeautifulSoup
    soup = BeautifulSoup(html, "html.parser")

    # Pick the table whose visible text contains Winner/Runner-up/Margin
    # but NOT 'Turnout' (the turnout-only table also matches Constituency).
    table = None
    for t in soup.find_all("table"):
        text = t.get_text(" ", strip=True).lower()
        if ("winner" in text and "runner-up" in text and "margin" in text
                and len(t.find_all("tr")) > 50):
            table = t
            break
    if table is None:
        print(f"  ⚠ 2025: master winners table not found", file=sys.stderr)
        return []

    print(f"  ✓ 2025: master winners table found "
          f"({len(table.find_all('tr'))} rows)", file=sys.stderr)

    out = []
    seen_const = set()
    for row in table.find_all("tr"):
        cells = row.find_all(["td", "th"])
        if len(cells) < 12:
            continue
        cell_texts = [_cell_text(c) for c in cells]
        const_raw = cell_texts[1]
        const_norm = _normalize_constituency(const_raw)
        if const_norm not in expected:
            continue
        if const_norm in seen_const:
            continue
        seen_const.add(const_norm)

        candidates = [
            {
                "name":           cell_texts[2],
                "party":          cell_texts[4],
                "votes":          _extract_int(cell_texts[5]),
                "vote_share_pct": _extract_pct(cell_texts[6]),
                "is_winner":      True,
                "rank":           1,
            },
            {
                "name":           cell_texts[7],
                "party":          cell_texts[9],
                "votes":          _extract_int(cell_texts[10]),
                "vote_share_pct": _extract_pct(cell_texts[11]) if len(cell_texts) > 11 else None,
                "is_winner":      False,
                "rank":           2,
            },
        ]
        out.append({
            "constituency_raw":   const_raw,
            "constituency_norm":  const_norm,
            "candidates":         candidates,
        })
    return out


def parse_constituency_results(html: str, year: int,
                                  expected_constituencies: set[str]) -> list[dict]:
    """Dispatcher — calls the year-specific parser."""
    if year == 2020:
        return parse_2020(html, expected_constituencies)
    if year == 2025:
        return parse_2025(html, expected_constituencies)
    return []


# ---------------------------------------------------------------------------
# DB lookup + matching
# ---------------------------------------------------------------------------

def build_candidate_lookup(cur: sqlite3.Cursor, year: int) -> dict:
    """Return {(norm_const, norm_name): (appearance_id, candidate_name, party_short)}."""
    cur.execute("""
        SELECT ea.id, p.name, c.name, par.short_name
        FROM election_appearances ea
        JOIN politicians  p ON ea.politician_id   = p.id
        JOIN constituencies c ON ea.constituency_id = c.id
        JOIN elections     e ON ea.election_id     = e.id
        LEFT JOIN parties  par ON ea.party_id       = par.id
        WHERE e.year = ?
    """, (year,))
    out = {}
    for app_id, name, const, party in cur.fetchall():
        key = (_normalize_constituency(const), _normalize_name(name))
        out[key] = (app_id, name, party)
    return out


def find_match(wiki_name: str, const_norm: str, lookup: dict,
                threshold: int = 75) -> tuple | None:
    """Fuzzy-match a Wikipedia name to our DB candidate for the same constituency."""
    try:
        from rapidfuzz import fuzz
    except ImportError:
        sys.exit("pip install rapidfuzz")

    wiki_norm = _normalize_name(wiki_name)
    if not wiki_norm:
        return None

    # First: exact normalized match
    key = (const_norm, wiki_norm)
    if key in lookup:
        app_id, db_name, party = lookup[key]
        return (app_id, db_name, party, 100)

    # Else: fuzzy across this constituency only
    best_score = 0
    best_match = None
    for (c, n), (app_id, db_name, party) in lookup.items():
        if c != const_norm:
            continue
        # Try multiple match strategies; take the best
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
    ap = argparse.ArgumentParser(description=__doc__,
                                  formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--year", type=int, default=0,
                    help="Process only this cycle (2020 or 2025). Default: both.")
    ap.add_argument("--refetch", action="store_true",
                    help="Re-fetch from Wikipedia even if cached HTML exists.")
    ap.add_argument("--dry-run", action="store_true",
                    help="Show matches; do not write to DB.")
    ap.add_argument("--db", default=str(DB_PATH))
    ap.add_argument("--match-threshold", type=int, default=75,
                    help="Minimum fuzzy-match score (0-100). Default 75.")
    ap.add_argument("--dump-tables", action="store_true",
                    help="List every <table> on each page with header row "
                         "+ first data row. Useful when parser misses the "
                         "right table.")
    args = ap.parse_args()

    if not Path(args.db).exists():
        sys.exit(f"DB not found: {args.db}")

    years = [args.year] if args.year else [2020, 2025]
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)

    con = sqlite3.connect(args.db)
    cur = con.cursor()

    # Known constituency set for table detection
    cur.execute("SELECT name FROM constituencies")
    expected = {_normalize_constituency(r[0]) for r in cur.fetchall()}

    grand_total_matched = 0
    grand_total_unmatched = 0

    for year in years:
        print(f"\n========== {year} ==========", file=sys.stderr)

        # Optional debug dump — every table's structure
        if args.dump_tables:
            html = fetch_wiki_html(year, refetch=args.refetch)
            from bs4 import BeautifulSoup
            soup = BeautifulSoup(html, "html.parser")
            for ti, table in enumerate(soup.find_all("table")):
                rows = table.find_all("tr")
                if len(rows) < 5:
                    continue
                header_cells = [_cell_text(c)[:40]
                                 for c in rows[0].find_all(["th", "td"])]
                print(f"  Table {ti}: {len(rows)} rows, headers: "
                      f"{header_cells}", file=sys.stderr)
                if len(rows) > 2:
                    sample = [_cell_text(c)[:40]
                              for c in rows[2].find_all(["th", "td"])]
                    print(f"             row 2 sample:   {sample}",
                          file=sys.stderr)
            continue

        # Fetch + parse (cache the raw results JSON)
        results_json_path = RESULTS_DIR / f"delhi_{year}_results.json"
        if results_json_path.exists() and not args.refetch:
            print(f"  Using cached parse: {results_json_path}", file=sys.stderr)
            results = json.loads(results_json_path.read_text())
        else:
            html = fetch_wiki_html(year, refetch=args.refetch)
            print(f"  Parsing master results table ...", file=sys.stderr)
            results = parse_constituency_results(html, year, expected)
            results_json_path.write_text(json.dumps(results, indent=2,
                                                      ensure_ascii=False))
            print(f"  Saved {len(results)} constituencies to "
                  f"{results_json_path.name}", file=sys.stderr)

        if not results:
            print(f"  ⚠ No parsed results for {year} — skipping",
                  file=sys.stderr)
            continue

        # Build candidate lookup for this year
        lookup = build_candidate_lookup(cur, year)
        print(f"  Candidate lookup: {len(lookup)} appearances for {year}",
              file=sys.stderr)

        # Match + UPDATE
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
                    "appearance_id":   app_id,
                    "db_name":         db_name,
                    "db_party":        party,
                    "fuzzy_score":     score,
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

        # Persist enriched results JSON (with _matched annotations)
        if not args.dry_run:
            results_json_path.write_text(json.dumps(results, indent=2,
                                                     ensure_ascii=False))

        if not args.dry_run:
            con.commit()

        # Stats
        print(f"  Winners set:     {winners_set}", file=sys.stderr)
        print(f"  Runners-up set:  {runnersup_set}", file=sys.stderr)
        print(f"  Unmatched:       {len(unmatched)}", file=sys.stderr)
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
