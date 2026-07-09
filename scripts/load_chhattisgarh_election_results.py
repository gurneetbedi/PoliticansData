"""
Load Chhattisgarh election results (winners + vote counts) from Wikipedia.

Sibling to the other state loaders — filter-first workflow supported.

Reads:
    https://en.wikipedia.org/wiki/2023_Chhattisgarh_Legislative_Assembly_election
    https://en.wikipedia.org/wiki/2018_Chhattisgarh_Legislative_Assembly_election

Chhattisgarh naming: Hindi + tribal (Gondi/Halbi in Bastar region). Standard normalizer.
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
STATE_NAME   = "Chhattisgarh"

WIKI_URLS = {
    2023: "https://en.wikipedia.org/wiki/2023_Chhattisgarh_Legislative_Assembly_election",
    2018: "https://en.wikipedia.org/wiki/2018_Chhattisgarh_Legislative_Assembly_election",
}

USER_AGENT = (
    "Lokvani/0.1 (open-source civic transparency; "
    "contact: gurneet.bedi@me.com) Python-requests"
)


CHHATTISGARH_2023_COLS = {
    # Filled in after --dump-tables diagnostic on 2026-06-27.
    # Table 13 is the master results table. NE-standard layout — no
    # turnout column, single margin cell. 13-cell subsequent rows /
    # 14-cell first-of-district. Negative indices align both.
    "table_index": 13,
    "header_rows": 2,
    "cols": {
        "constituency": -12,   # 'Manendragarh' / 'Baikunthpur' / etc.
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
CHHATTISGARH_2018_COLS = {"table_index": None, "header_rows": 2, "cols": {}}

COL_MAPS = {2023: CHHATTISGARH_2023_COLS, 2018: CHHATTISGARH_2018_COLS}


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
    cache_path = RESULTS_DIR / f"_wiki_chhattisgarh_{year}.html"
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


def parse_with_colmap(html: str, year: int, expected: set[str]) -> list[dict]:
    cfg = COL_MAPS.get(year, {})
    if cfg.get("table_index") is None:
        print(f"  ⚠ No column map for {year} yet. Run --dump-tables first,",
              file=sys.stderr)
        print(f"    update CHHATTISGARH_{year}_COLS in this script, then re-run.",
              file=sys.stderr)
        return []

    from bs4 import BeautifulSoup
    soup = BeautifulSoup(html, "html.parser")
    tables = soup.find_all("table")
    ti = cfg["table_index"]
    if ti >= len(tables):
        return []
    table = tables[ti]
    rows = table.find_all("tr")
    data_rows = rows[cfg.get("header_rows", 1):]
    cols = cfg["cols"]
    if not cols:
        return []

    positives = [v for v in cols.values() if v >= 0]
    negatives = [v for v in cols.values() if v < 0]
    min_len = max((max(positives) + 1) if positives else 0,
                  abs(min(negatives)) if negatives else 0)

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
        if not const_norm:
            skipped += 1
            continue
        if expected and const_norm not in expected:
            skipped += 1
            continue

        def _get(name):
            i = cols.get(name)
            if i is None: return ""
            try: return _cell_text(cells[i])
            except IndexError: return ""

        cands = []
        wn, wp = _get("winner_name"), _get("winner_party")
        if wn:
            cands.append({
                "rank": 1, "is_winner": True, "name": wn, "party_raw": wp,
                "votes": _extract_int(_get("winner_votes")),
                "vote_share_pct": _extract_pct(_get("winner_pct")),
            })
        rn, rp = _get("runner_name"), _get("runner_party")
        if rn:
            cands.append({
                "rank": 2, "is_winner": False, "name": rn, "party_raw": rp,
                "votes": _extract_int(_get("runner_votes")),
                "vote_share_pct": _extract_pct(_get("runner_pct")),
            })
        if not cands:
            skipped += 1
            continue
        results.append({
            "constituency_raw": const,
            "constituency_norm": const_norm,
            "candidates": cands,
        })

    print(f"  Parsed {len(results)} constituencies ({skipped} skipped)",
          file=sys.stderr)
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
    return {(_normalize_constituency(c), _normalize_name(n)): (i, n, p)
            for i, n, c, p in cur.fetchall()}


def find_match(wiki_name, const_norm, lookup, threshold=70):
    try: from rapidfuzz import fuzz
    except ImportError: sys.exit("pip install rapidfuzz")
    wn = _normalize_name(wiki_name)
    if not wn: return None
    hit = lookup.get((const_norm, wn))
    if hit:
        return (*hit, 100)
    best, best_score = None, 0
    for (c, n), v in lookup.items():
        if c != const_norm: continue
        score = max(fuzz.partial_ratio(wn, n), fuzz.token_set_ratio(wn, n),
                    fuzz.token_sort_ratio(wn, n))
        if score > best_score:
            best_score, best = score, v
    return (*best, best_score) if best and best_score >= threshold else None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--year", type=int, default=0)
    ap.add_argument("--refetch", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--dump-tables", action="store_true")
    ap.add_argument("--match-threshold", type=int, default=70)
    ap.add_argument("--db", default=str(DB_PATH))
    args = ap.parse_args()

    if not Path(args.db).exists():
        sys.exit(f"DB not found: {args.db}")
    years = [args.year] if args.year else [2023, 2018]
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(args.db)
    cur = con.cursor()
    cur.execute("SELECT c.name FROM constituencies c JOIN states s ON c.state_id = s.id WHERE s.name = ?",
                (STATE_NAME,))
    expected = {_normalize_constituency(r[0]) for r in cur.fetchall()}
    print(f"Chhattisgarh constituencies in DB: {len(expected)}", file=sys.stderr)
    if not expected and not (args.dump_tables or args.dry_run):
        sys.exit("Run migrate_to_eci_only.py first, or use --dump-tables/--dry-run")

    grand_matched = grand_unmatched = 0
    for year in years:
        print(f"\n========== Chhattisgarh {year} ==========", file=sys.stderr)
        if args.dump_tables:
            dump_tables(fetch_wiki_html(year, refetch=args.refetch))
            continue

        rp = RESULTS_DIR / f"chhattisgarh_{year}_results.json"
        if rp.exists() and not args.refetch:
            results = json.loads(rp.read_text())
        else:
            html = fetch_wiki_html(year, refetch=args.refetch)
            results = parse_with_colmap(html, year, expected)
            rp.write_text(json.dumps(results, indent=2, ensure_ascii=False))
            print(f"  Saved {len(results)} constituencies to {rp.name}",
                  file=sys.stderr)
        if not results: continue
        lookup = build_candidate_lookup(cur, year)
        print(f"  Candidate lookup: {len(lookup)} appearances", file=sys.stderr)

        wins, runs, unmatched = 0, 0, []
        for row in results:
            for cand in row["candidates"]:
                m = find_match(cand["name"], row["constituency_norm"], lookup,
                                args.match_threshold)
                if not m:
                    unmatched.append((row["constituency_raw"], cand["name"],
                                        cand.get("votes"), cand["rank"]))
                    continue
                app_id, dn, dp, sc = m
                cand["_matched"] = {"appearance_id": app_id, "db_name": dn,
                                     "db_party": dp, "fuzzy_score": sc}
                if not args.dry_run:
                    cur.execute("""UPDATE election_appearances SET won=?, votes_received=?, vote_share_pct=? WHERE id=?""",
                                (bool(cand.get("is_winner")), cand.get("votes"),
                                 cand.get("vote_share_pct"), app_id))
                if cand.get("is_winner"): wins += 1
                else: runs += 1
        if not args.dry_run:
            rp.write_text(json.dumps(results, indent=2, ensure_ascii=False))
            con.commit()
        print(f"  Winners set: {wins} · Runners-up: {runs} · Unmatched: {len(unmatched)}",
              file=sys.stderr)
        grand_matched += wins + runs
        grand_unmatched += len(unmatched)
        if unmatched:
            for c, n, v, r in unmatched[:10]:
                print(f"    [{r}] {c:22s} {n!r} votes={v}", file=sys.stderr)

    con.close()
    print(f"\n========== TOTAL ==========", file=sys.stderr)
    print(f"  Matched: {grand_matched}  Unmatched: {grand_unmatched}",
          file=sys.stderr)


if __name__ == "__main__":
    main()
