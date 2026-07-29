"""Scrape state cabinet ministers from Wikipedia + upsert with change tracking.

For each state in WIKIPEDIA_CABINET_URLS:
  1. Fetch the Wikipedia article HTML
  2. Find the first wikitable that has columns matching {Minister, Portfolio, Party}
  3. Parse each row → (minister_name, portfolios_list, party)
  4. For each portfolio, classify to a GPI pillar via gpi_minister_mapping
  5. Upsert to state_ministers with change detection:
     - New (state, portfolio_key, minister_name) → INSERT new row (sworn_in=today if unknown)
     - Existing row with SAME minister → UPDATE (refresh scraped_at, party if changed)
     - Existing row where a DIFFERENT minister now holds the portfolio →
         mark old row end_date=today, INSERT new row for incoming minister
     - Ministers who no longer appear on the page → mark end_date=today

Usage:
    python scripts/gpi_scrape_ministers.py                    # all states
    python scripts/gpi_scrape_ministers.py --state Punjab     # one state
    python scripts/gpi_scrape_ministers.py --dry-run          # no writes
"""
from __future__ import annotations
import argparse
import sys
import time
import re
from datetime import date, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from scripts.gpi_minister_wiki_urls import WIKIPEDIA_CABINET_URLS
from scripts.gpi_minister_mapping import classify_portfolio


HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (LokvaniGPI bot; +https://github.com/gurneetbedi/PoliticansData) "
        "Wikipedia state-cabinet scraper"
    ),
}

RAW_HTML_DIR = ROOT / "data" / "ministers" / "html"


def fetch_page(url: str) -> str:
    """Fetch and cache the HTML. Returns HTML text."""
    import requests
    RAW_HTML_DIR.mkdir(parents=True, exist_ok=True)
    slug = re.sub(r"\W+", "_", url.split("/")[-1])[:80]
    cache = RAW_HTML_DIR / f"{slug}.html"
    if cache.exists() and cache.stat().st_size > 1000:
        return cache.read_text(encoding="utf-8")

    r = requests.get(url, headers=HEADERS, timeout=30)
    r.raise_for_status()
    cache.write_text(r.text, encoding="utf-8")
    time.sleep(1)   # be nice to Wikipedia
    return r.text


def find_ministers_table(html: str):
    """Return the first BeautifulSoup <table> that looks like a ministers roster.

    Wikipedia's cabinet articles use varying column labels. We accept any
    wikitable whose header includes:
      • some name-of-minister column ("minister", "name")
      AND
      • some portfolio column ("portfolio", "department", "ministry")

    Header cells may be in <th> OR <td> depending on Wikipedia's markup.
    """
    from bs4 import BeautifulSoup
    soup = BeautifulSoup(html, "html.parser")

    for tbl in soup.select("table.wikitable"):
        first_row = tbl.select_one("tr")
        if not first_row:
            continue
        headers = [c.get_text(" ", strip=True).lower()
                   for c in first_row.select("th, td")]
        header_str = " | ".join(headers)

        has_name      = ("minister" in header_str or "name" in header_str)
        has_portfolio = ("portfolio" in header_str
                          or "department" in header_str
                          or "ministry" in header_str
                          or "designation" in header_str)
        # Exclude tables that are just district / constituency breakdowns
        # by requiring the roster to have both dimensions.
        if has_name and has_portfolio:
            # Extra guard: skip the "resigned"/"reshuffled" tables that also
            # match these criteria — they use "Reason" or "Status" columns.
            if "reason" in header_str or "status" in header_str:
                continue
            return tbl
    return None


def _clean_cell(txt: str) -> str:
    """Strip references [1], footnotes, extra whitespace."""
    txt = re.sub(r"\[\d+\]", "", txt)
    txt = re.sub(r"\s+", " ", txt)
    return txt.strip()


def _expand_colspan(row) -> list:
    """Return a list of BeautifulSoup cells expanded to logical column positions.

    A header cell with colspan=2 becomes two entries at the same logical
    columns; an empty position is left as None so we can align data rows
    against the same logical grid.
    """
    expanded = []
    for c in row.find_all(["th", "td"]):
        try:
            span = int(c.get("colspan", "1") or "1")
        except ValueError:
            span = 1
        for i in range(span):
            expanded.append(c if i == 0 else None)   # first slot carries value, rest are duplicates
    return expanded


def parse_ministers_table(tbl) -> list[dict]:
    """Return list of {name, portfolios: [str], party, is_cm}.

    Handles Wikipedia's messy cabinet-table markup:
      • Header cells with colspan (Kerala uses colspan=2 on the Name column)
      • Section-divider rows with a single colspan-spanning cell
        (e.g., "Chief Minister" / "Cabinet Ministers" dividers)
      • Trailing empty column pairs used for citations

    Uses logical column positions (expanded by colspan) rather than raw cell
    indices to keep name / portfolio / party aligned even when the table's
    physical cell count varies row-to-row.
    """
    rows = tbl.select("tr")
    if not rows:
        return []

    # Build the logical column → header label map, using colspan expansion.
    expanded_hdr = _expand_colspan(rows[0])
    headers = []
    for c in expanded_hdr:
        headers.append(c.get_text(" ", strip=True).lower() if c is not None else "")
    n_cols = len(headers)

    def _idx(needles: list[str]) -> int | None:
        for i, h in enumerate(headers):
            for n in needles:
                if n and n in h:
                    return i
        return None

    name_i = _idx(["minister", "name"])
    # Prefer Department/Portfolio (actual ministry list) over Designation
    # (which is just the role name, e.g., "Chief Minister" or "Minister for X").
    # NOTE: `_idx` can return 0 for the first column — must check `is None`
    # explicitly, NOT `or`, which is falsy for 0 (bit us on Punjab which has
    # Portfolio at index 0).
    portfolio_i = _idx(["portfolio", "department", "ministry"])
    if portfolio_i is None:
        portfolio_i = _idx(["designation"])
    party_i = _idx(["party"])

    if name_i is None or portfolio_i is None:
        return []

    out = []
    for row in rows[1:]:
        # Skip section dividers: 1 cell with a big colspan.
        raw_cells = row.find_all(["th", "td"])
        if len(raw_cells) <= 1:
            continue

        expanded = _expand_colspan(row)
        # If the expanded row is shorter than the header (some rows have
        # trailing missing cells), pad with None so index lookups don't crash.
        while len(expanded) < n_cols:
            expanded.append(None)

        def _get(i: int) -> str:
            if i is None or i >= len(expanded) or expanded[i] is None:
                return ""
            return _clean_cell(expanded[i].get_text(" | ", strip=True))

        name          = _get(name_i)
        portfolio_raw = _get(portfolio_i)
        party         = _get(party_i)

        # If party at the computed index is empty, scan the next few cells —
        # Kerala's table has an untitled spacer column between Department
        # and Party in data rows that shifts the alignment by one.
        if not party and party_i is not None:
            for probe in range(party_i + 1, min(party_i + 3, len(expanded))):
                candidate = _get(probe)
                if candidate and len(candidate) < 40:
                    party = candidate
                    break

        if not name or len(name) < 3:
            continue

        # Skip rows where "name" ended up being just a section header
        if name.lower() in {"chief minister", "cabinet ministers", "cabinet minister"}:
            continue

        # Some tables put "(Chief Minister)" beside the name — detect + strip
        is_cm = ("chief minister" in name.lower()
                  or "chief minister" in portfolio_raw.lower())
        name = re.sub(r"\s*\(.*?\)\s*", " ", name).strip()

        # Split portfolios on ", " or "|" (from get_text separator).
        # NOTE: Kerala style uses space-separated department names inside one
        # cell without commas ("Health Water Resources Housing"). For those,
        # we return the whole cell as a single portfolio label — better than
        # over-splitting and confusing the mapping.
        if "|" in portfolio_raw or "," in portfolio_raw:
            portfolios = [p.strip() for chunk in portfolio_raw.split("|") for p in chunk.split(",")]
        else:
            portfolios = [portfolio_raw.strip()]
        portfolios = [p for p in portfolios if len(p) > 2]

        out.append({
            "name":       name,
            "portfolios": portfolios,
            "party":      party or None,
            "is_cm":      is_cm,
        })
    return out


def upsert_ministers(session, state, rows: list[dict], source_url: str,
                      dry_run: bool) -> dict:
    """Upsert with change tracking. Returns counts dict.

    For each (portfolio_key) currently listed on the page:
      - If the DB has an active row (end_date=None) for that key with the
        SAME minister → refresh metadata (party, scraped_at).
      - If DB has an active row with a DIFFERENT minister → close it
        (end_date=today) and insert a new row for the incoming minister.
      - If DB has no active row for that key → insert new.

    For active rows in DB whose portfolio_key is NOT in the current scrape
    → close them (end_date=today) — the reshuffle removed that portfolio,
    or renamed it, or the minister was dropped.
    """
    from app.gpi_models import StateMinister

    today = date.today()

    counts = {"inserted": 0, "updated": 0, "closed": 0, "unmapped": 0,
                "dupes_cleaned": 0}

    # Two sets:
    #   seen_keys — every portfolio_key that appeared on the page (used at
    #     the end to close active rows whose portfolio no longer appears)
    #   handled_keys — portfolio_keys already processed in this run, so we
    #     don't insert a 2nd active row for the same key when a minister
    #     holds multiple portfolios that classify to the same pillar (e.g.,
    #     "Health" and "Medical Education" both → health)
    seen_keys: set[str] = set()
    handled_keys: set[str] = set()

    for row in rows:
        for raw_portfolio in row["portfolios"]:
            cls = classify_portfolio(raw_portfolio)
            if cls is None:
                counts["unmapped"] += 1
                continue
            portfolio_key, pillar_code = cls
            seen_keys.add(portfolio_key)

            if portfolio_key in handled_keys:
                # Already recorded this portfolio for this state during this
                # run — skip so we don't create duplicates.
                continue
            handled_keys.add(portfolio_key)

            # Fetch ALL active rows for this (state, portfolio_key). Multiple
            # can exist due to historic dupes from earlier buggy runs — close
            # extras and keep the most recently scraped as the survivor.
            active_rows = (
                session.query(StateMinister)
                .filter_by(state_id=state.id, portfolio_key=portfolio_key,
                            end_date=None)
                .order_by(StateMinister.scraped_at.desc())
                .all()
            )
            existing = active_rows[0] if active_rows else None
            if len(active_rows) > 1:
                # Close the older dupes
                for extra in active_rows[1:]:
                    extra.end_date = today
                    counts["dupes_cleaned"] += 1

            if existing:
                if existing.minister_name.strip().lower() == row["name"].strip().lower():
                    # Same minister — just refresh
                    existing.party            = row["party"] or existing.party
                    existing.is_cm            = row["is_cm"] or existing.is_cm
                    existing.portfolio_display = raw_portfolio
                    existing.source_url        = source_url
                    existing.scraped_at        = datetime.utcnow()
                    counts["updated"] += 1
                else:
                    # Change of minister → close old, insert new
                    existing.end_date = today
                    counts["closed"] += 1
                    session.add(StateMinister(
                        state_id          = state.id,
                        portfolio_key     = portfolio_key,
                        portfolio_display = raw_portfolio,
                        pillar_code       = pillar_code,
                        minister_name     = row["name"],
                        party             = row["party"],
                        is_cm             = row["is_cm"],
                        sworn_in_date     = today,
                        source_url        = source_url,
                        source_type       = "wikipedia",
                    ))
                    counts["inserted"] += 1
            else:
                session.add(StateMinister(
                    state_id          = state.id,
                    portfolio_key     = portfolio_key,
                    portfolio_display = raw_portfolio,
                    pillar_code       = pillar_code,
                    minister_name     = row["name"],
                    party             = row["party"],
                    is_cm             = row["is_cm"],
                    sworn_in_date     = today,
                    source_url        = source_url,
                    source_type       = "wikipedia",
                ))
                counts["inserted"] += 1

    # Close any active rows whose portfolio_key no longer appears
    stale = (
        session.query(StateMinister)
        .filter_by(state_id=state.id, end_date=None)
        .filter(~StateMinister.portfolio_key.in_(seen_keys))
        .all()
    )
    for row in stale:
        row.end_date = today
        counts["closed"] += 1

    if not dry_run:
        session.commit()
    else:
        session.rollback()

    return counts


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                  formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--state", help="Only scrape this state (default: all)")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--force", action="store_true",
                    help="Re-fetch HTML even if cached")
    args = ap.parse_args()

    try:
        import requests   # noqa
        from bs4 import BeautifulSoup   # noqa
    except ImportError:
        raise SystemExit("pip install requests beautifulsoup4")

    from app.database import SessionLocal
    from app.models import State

    session = SessionLocal()

    total_states = 0
    grand = {"inserted": 0, "updated": 0, "closed": 0, "unmapped": 0, "dupes_cleaned": 0}
    for state_name, url in WIKIPEDIA_CABINET_URLS.items():
        if args.state and state_name != args.state:
            continue

        st = session.query(State).filter_by(name=state_name).one_or_none()
        if not st:
            print(f"  ✗ {state_name}: state not in DB, skipping")
            continue

        print(f"→ {state_name}  {url}")
        try:
            if args.force:
                slug = re.sub(r"\W+", "_", url.split("/")[-1])[:80]
                cache = RAW_HTML_DIR / f"{slug}.html"
                if cache.exists():
                    cache.unlink()
            html = fetch_page(url)
        except Exception as e:
            print(f"  ✗ fetch error: {type(e).__name__}: {e}")
            continue

        tbl = find_ministers_table(html)
        if tbl is None:
            print(f"  ✗ no wikitable with (Minister, Portfolio, Party) columns found — inspect the page manually")
            continue

        rows = parse_ministers_table(tbl)
        if not rows:
            print(f"  ✗ table found but no minister rows parsed")
            continue

        counts = upsert_ministers(session, st, rows, url, args.dry_run)
        cleaned = counts.get("dupes_cleaned", 0)
        cleaned_str = f" cleaned={cleaned}" if cleaned else ""
        print(f"  ✓ {len(rows)} minister rows · "
              f"inserted={counts['inserted']} updated={counts['updated']} "
              f"closed={counts['closed']} unmapped={counts['unmapped']}{cleaned_str}")
        for k, v in counts.items():
            grand[k] += v
        total_states += 1

    session.close()
    print()
    print(f"═══════════════════════════════════════════════════════")
    print(f"Total states processed: {total_states}")
    print(f"  Inserted: {grand['inserted']}")
    print(f"  Updated:  {grand['updated']}")
    print(f"  Closed:   {grand['closed']} (portfolio changes)")
    print(f"  Cleaned:  {grand['dupes_cleaned']} (historic duplicate rows closed)")
    print(f"  Unmapped: {grand['unmapped']} (portfolio didn't match any GPI pillar)")
    if args.dry_run:
        print("\n(dry run — no writes)")


if __name__ == "__main__":
    main()
