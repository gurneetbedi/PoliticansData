"""
Load Arunachal Pradesh election results (winners + vote counts) from Wikipedia.

Sibling to the other state loaders.

Reads:
    https://en.wikipedia.org/wiki/2024_Arunachal_Pradesh_Legislative_Assembly_election
    https://en.wikipedia.org/wiki/2019_Arunachal_Pradesh_Legislative_Assembly_election

Arunachali naming: tribal-region names, often two-word patterns
("Pema Khandu", "Chowna Mein"), minimal honorifics. Standard
normalizer handles all cases; threshold=70 is generous.

Note: 2024 had 8 uncontested seats. Those get no runner-up entry
in Wikipedia and are counted as "skipped" rows — expected behavior.
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
RESULTS_DIR  = PROJECT_ROOT / "data/eci/results"
STATE_NAME   = "Arunachal Pradesh"

WIKI_URLS = {
    2024: "https://en.wikipedia.org/wiki/2024_Arunachal_Pradesh_Legislative_Assembly_election",
    2019: "https://en.wikipedia.org/wiki/2019_Arunachal_Pradesh_Legislative_Assembly_election",
}

USER_AGENT = (
    "Lokvani/0.1 (open-source civic transparency; "
    "contact: gurneet.bedi@me.com) Python-requests"
)


# Filled in after --dump-tables diagnostic.
ARUNACHAL_2024_COLS = {
    # Filled in after --dump-tables diagnostic on 2026-06-27.
    # Table 9 is the master results table. Layout matches NE standard —
    # no turnout column, single margin cell. 13-cell rows uniformly
    # (district shown only in subheader rows, filtered out). Note: AP
    # 2024 had 10 unopposed seats which don't have runner-up rows;
    # those are silently skipped by the parser.
    "table_index": 9,
    "header_rows": 2,
    "cols": {
        "constituency": -12,   # 'Lumla' / 'Tawang' / etc.
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
ARUNACHAL_2019_COLS = {"table_index": None, "header_rows": 2, "cols": {}}

COL_MAPS = {
    2024: ARUNACHAL_2024_COLS,
    2019: ARUNACHAL_2019_COLS,
}


def _normalize_name(name: str) -> str:
    if not name:
        return ""
    s = name.upper().strip()
    for prefix in ("DR. ", "DR ", "ADV. ", "ADV ", "ADVOCATE ",
                    "SHRI ", "SHRIMATI ", "SMT. ", "SMT ",
                    "MR. ", "MR ", "MS. ", "MS ", "MRS. ", "MRS ",
                    "PROF. ", "PROF "):
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
    cache_path = RESULTS_DIR / f"_wiki_arunachal_{year}.html"
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


def parse_with_colmap(html: str, year: int,
                       expected: set[str]) -> list[dict]:
    cfg = COL_MAPS.get(year, {})
    if cfg.get("table_index") is None:
        print(f"  ⚠ No column map for {year} yet. Run --dump-tables first,",
              file=sys.stderr)
        print(f"    update ARUNACHAL_{year}_COLS in this script, then re-run.",
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
                threshold: int = 70) -> tuple | None:
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
                    help="2024 or 2019. Default: both.")
    ap.add_argument("--refetch", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--dump-tables", action="store_true")
    ap.add_argument("--match-threshold", type=int, default=70)
    ap.add_argument("--db", default=str(DB_PATH))
    args = ap.parse_args()

    if not Path(args.db).exists():
        sys.exit(f"DB not found: {args.db}")

    years = [args.year] if args.year else [2024, 2019]
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)

    con = sqlite3.connect(args.db)
    cur = con.cursor()

    cur.execute("""
        SELECT c.name FROM constituencies c
        JOIN states s ON c.state_id = s.id
        WHERE s.name = ?
    """, (STATE_NAME,))
    expected = {_normalize_constituency(r[0]) for r in cur.fetchall()}
    print(f"Arunachal Pradesh constituencies in DB: {len(expected)}",
          file=sys.stderr)
    if not expected:
        sys.exit("No Arunachal Pradesh constituencies in DB. "
                 "Run migrate_to_eci_only.py first.")

    grand_total_matched = 0
    grand_total_unmatched = 0

    for year in years:
        print(f"\n========== Arunachal Pradesh {year} ==========",
              file=sys.stderr)

        if args.dump_tables:
            html = fetch_wiki_html(year, refetch=args.refetch)
            dump_tables(html)
            continue

        results_path = RESULTS_DIR / f"arunachal_{year}_results.json"
        if results_path.exists() and not args.refetch:
            print(f"  Using cached parse: {results_path.name}", file=sys.stderr)
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
        print(f"  Candidate lookup: {len(lookup)} AP appearances for {year}",
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
