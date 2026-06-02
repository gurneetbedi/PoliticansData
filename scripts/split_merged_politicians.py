"""
One-time cleanup: undo the cross-election candidate-id collisions in politrack.db.

Background
----------
The scraper was treating myneta's `candidate_id` as globally unique, but myneta
re-numbers candidates per election. So Punjab2022 candidate_id=2 ("Sandeep Jakhar")
and Goa2022 candidate_id=2 ("Anamika") both wrote into the same Politician row,
mashing their data together. This affected ~9% of politicians across states and
~44% across cycles.

Fix strategy
------------
1. Read every ElectionAppearance.
2. For each, parse `source_url` to extract (election_slug, candidate_id).
3. Look up the cached candidate page (every URL is in data/cache/myneta/) and
   extract the REAL name from the <h2> tag.
4. Group appearances by (election_slug, candidate_id) — every group becomes one
   new Politician row with the real name.
5. Reassign each ElectionAppearance.politician_id to its new Politician.
6. Delete all original Politician rows.

After this script:
  - One Politician per appearance (no cross-cycle linkage; that's OK for now).
  - Every politician's name matches their actual myneta candidate page.
  - assets / liabilities / criminal_cases stay attached to the right appearance,
    so all child data follows the rename automatically.

Run:
    python scripts/split_merged_politicians.py [--dry-run]

Then push the cleaned SQLite to Neon:
    export DATABASE_URL="postgresql://..."
    python scripts/sqlite_to_postgres.py --reset
"""
import hashlib
import re
import sqlite3
import sys
from pathlib import Path

from bs4 import BeautifulSoup
from slugify import slugify

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DB_PATH      = PROJECT_ROOT / "politrack.db"
CACHE_DIR    = PROJECT_ROOT / "data" / "cache" / "myneta"

DRY_RUN = "--dry-run" in sys.argv


def cache_path(url: str) -> Path:
    """Same hashing scheme as app/scrapers/myneta_client.py — sha1 of the URL."""
    return CACHE_DIR / f"{hashlib.sha1(url.encode()).hexdigest()}.html"


URL_RE = re.compile(r"/([^/]+)/candidate\.php\?candidate_id=(\d+)")

def parse_source_url(url: str):
    """Return (election_slug, candidate_id) or (None, None)."""
    if not url:
        return None, None
    m = URL_RE.search(url)
    if not m:
        return None, None
    return m.group(1), int(m.group(2))


_GARBAGE_NAMES = {
    "home", "donate", "share on", "myneta", "myneta.info",
    "crime-o-meter", "assets & liabilities", "educational details",
    "details of criminal cases", "elections",
}

def _looks_like_garbage(s: str) -> bool:
    """Reject non-name strings (section headers, page chrome, year markers)."""
    if not s:
        return True
    low = s.lower()
    if any(low.startswith(g) for g in _GARBAGE_NAMES):
        return True
    # 4-digit year on its own usually means it's an election header like "Punjab 2022"
    if re.search(r"\b(19|20)\d{2}\b", s):
        return True
    return False


def _clean_name(s: str) -> str:
    """Strip (Winner)/(Loser) suffixes and collapse whitespace."""
    s = re.sub(r"\s*\((Winner|Loser|Runner.?up)\)\s*$", "", s, flags=re.I)
    s = re.sub(r"\s+", " ", s).strip()
    return s


def extract_name(html: str) -> str:
    """
    Pull the candidate name from a myneta candidate.php page.

    Strategy: try the <title> first because its format is the most consistent
    across all 17 election templates we ingest. Falls back to <h2> and <h3>,
    skipping any candidate that looks like page chrome / a section heading /
    an election year marker.

      <title>: "Sandeep Jakhar(Indian National Congress(INC)):Constituency-..."
      <h2>:    "SANDEEP JAKHAR(Winner)"   (newer templates)
      <h3>:    "Punjab 2022"               (election header — REJECT)
    """
    soup = BeautifulSoup(html, "lxml")

    # 1. <title> is most reliable. Take everything before the first "(" or ":".
    title = soup.find("title")
    if title:
        text = title.get_text(strip=True)
        m = re.match(r"^([^()\[\]:]+)", text)
        if m:
            cand = _clean_name(m.group(1))
            if cand and not _looks_like_garbage(cand):
                return cand

    # 2. Walk all h2 → h3 tags in document order, return the first one that
    #    survives the garbage filter.
    for tag in soup.find_all(["h2", "h3"]):
        cand = _clean_name(tag.get_text(strip=True))
        if cand and not _looks_like_garbage(cand):
            return cand

    return ""


# --- Build a unique slug --------------------------------------------------
# We append the election slug + candidate id so the URL is stable across
# script runs and across re-loads. Format: <name-slug>-<election>-<candid>
def build_slug(name: str, election_slug: str, candidate_id: int, taken: set) -> str:
    base = slugify(name) if name else "candidate"
    suffix = f"-{election_slug.lower()}-{candidate_id}"
    s = (base + suffix)[:240]
    # Almost always unique already; guard against pathological collisions
    n = 2
    out = s
    while out in taken:
        out = f"{s}-{n}"
        n += 1
    taken.add(out)
    return out


def main():
    if not DB_PATH.exists():
        sys.exit(f"DB not found at {DB_PATH}")

    con = sqlite3.connect(str(DB_PATH))
    con.row_factory = sqlite3.Row
    cur = con.cursor()

    appearances = cur.execute("""
        SELECT id, politician_id, source_url
        FROM election_appearances
        WHERE source_url IS NOT NULL
    """).fetchall()
    print(f"Found {len(appearances):,} appearances with source_url")

    # Pass 1 — derive (slug, candidate_id, name) for every appearance
    rows = []
    cache_misses = parse_misses = name_misses = 0
    for app in appearances:
        slug, cand_id = parse_source_url(app["source_url"])
        if not slug:
            parse_misses += 1
            continue
        path = cache_path(app["source_url"])
        if not path.exists():
            cache_misses += 1
            continue
        try:
            html = path.read_text(encoding="utf-8", errors="ignore")
        except Exception as e:
            cache_misses += 1
            continue
        name = extract_name(html)
        if not name:
            name_misses += 1
            name = f"Candidate {cand_id} ({slug})"
        rows.append({
            "app_id":   app["id"],
            "slug":     slug,
            "cand_id":  cand_id,
            "name":     name,
        })

    print(f"  parse-misses: {parse_misses}")
    print(f"  cache-misses: {cache_misses}")
    print(f"  name-misses:  {name_misses}  (used a placeholder)")
    print(f"  resolved:     {len(rows):,}")

    # Pass 2 — for each unique (slug, cand_id), allocate a new politician
    groups = {}   # (slug, cand_id) -> { name, app_ids: [...] }
    for r in rows:
        key = (r["slug"], r["cand_id"])
        g = groups.setdefault(key, {"name": r["name"], "app_ids": []})
        g["app_ids"].append(r["app_id"])
        # If we somehow get different names for the same (slug, cand_id),
        # take the longer one (usually the more complete version).
        if len(r["name"]) > len(g["name"]):
            g["name"] = r["name"]

    print(f"\nWill create {len(groups):,} new Politician rows "
          f"(was {cur.execute('SELECT COUNT(*) FROM politicians').fetchone()[0]:,} polluted rows)")

    if DRY_RUN:
        # Show a sample of what would happen for spot-checking
        print("\nDRY RUN — first 5 splits:")
        for i, ((slug, cand_id), g) in enumerate(list(groups.items())[:5]):
            print(f"  {slug:15} cand_id={cand_id:5}  name={g['name']!r}  apps={g['app_ids']}")
        print("\nNo writes performed. Remove --dry-run to apply.")
        return

    # Drop the legacy UNIQUE constraint on myneta_candidate_id ----------------
    # The model was changed (Column has only index=True now), but SQLite's
    # `create_all` is purely additive — it won't drop the auto-generated
    # unique index from when the DB was first created. Without this step, the
    # very first split (e.g. two candidate_id=2 rows from different elections)
    # will hit the legacy constraint and the whole transaction rolls back.
    print("\nChecking for legacy UNIQUE constraints to drop...")
    cur.execute("PRAGMA index_list(politicians)")
    legacy_indexes = []
    for row in cur.fetchall():
        # PRAGMA index_list returns: (seq, name, unique, origin, partial)
        idx_name, is_unique = row[1], row[2]
        if not is_unique:
            continue
        cur.execute(f"PRAGMA index_info('{idx_name}')")
        idx_cols = [r[2] for r in cur.fetchall()]
        # Only drop UNIQUE indexes that target myneta_candidate_id specifically.
        # Leave the implicit PK index ("id") alone, and leave the slug unique
        # alone (we still want unique slugs).
        if idx_cols == ["myneta_candidate_id"]:
            legacy_indexes.append(idx_name)

    if legacy_indexes:
        for n in legacy_indexes:
            cur.execute(f"DROP INDEX IF EXISTS '{n}'")
            print(f"  dropped UNIQUE index {n}")
        # Re-create as a NON-unique index so lookups stay fast.
        cur.execute("CREATE INDEX IF NOT EXISTS ix_politicians_myneta_candidate_id "
                    "ON politicians (myneta_candidate_id)")
        print("  recreated as non-unique index")
        con.commit()
    else:
        print("  none found (already migrated)")

    # Apply -----------------------------------------------------------------
    # We do this in one transaction so the DB never sees a partial state.
    print("\nApplying changes (single transaction)...")
    try:
        cur.execute("BEGIN")

        # 1. Insert new politicians. We use myneta_candidate_id = cand_id but it's
        #    no longer constrained to be globally unique (the unique constraint
        #    must be relaxed in models.py — done in the same commit).
        taken_slugs = set()
        new_ids = {}   # (slug, cand_id) -> new politician_id

        # Allocate IDs starting above current MAX(id) so we don't collide with
        # existing politicians while the swap is mid-flight.
        max_existing_id = cur.execute("SELECT COALESCE(MAX(id), 0) FROM politicians").fetchone()[0]
        next_id = max_existing_id + 1

        for (slug, cand_id), g in groups.items():
            slug_str = build_slug(g["name"], slug, cand_id, taken_slugs)
            cur.execute("""
                INSERT INTO politicians (id, name, slug, myneta_candidate_id)
                VALUES (?, ?, ?, ?)
            """, (next_id, g["name"], slug_str, cand_id))
            new_ids[(slug, cand_id)] = next_id
            next_id += 1

        # 2. Reassign appearances to their new politician
        for (slug, cand_id), g in groups.items():
            new_pid = new_ids[(slug, cand_id)]
            cur.executemany(
                "UPDATE election_appearances SET politician_id = ? WHERE id = ?",
                [(new_pid, app_id) for app_id in g["app_ids"]],
            )

        # 3. Delete the original (polluted) politicians — anything <= max_existing_id
        cur.execute("DELETE FROM politicians WHERE id <= ?", (max_existing_id,))

        con.commit()
        print(f"  Inserted {len(groups):,} new politicians, reassigned "
              f"{sum(len(g['app_ids']) for g in groups.values()):,} appearances, "
              f"deleted {max_existing_id:,} originals.")
    except Exception as e:
        con.rollback()
        sys.exit(f"FAILED, rolled back: {e}")

    # Sanity check
    print("\nPost-cleanup sanity check:")
    n = cur.execute("SELECT COUNT(*) FROM politicians").fetchone()[0]
    print(f"  politicians: {n:,}")
    n_cross = cur.execute("""
        SELECT COUNT(*) FROM (
            SELECT p.id FROM politicians p
            JOIN election_appearances ea ON ea.politician_id = p.id
            JOIN elections e ON e.id = ea.election_id
            JOIN states s ON s.id = e.state_id
            GROUP BY p.id HAVING COUNT(DISTINCT s.name) > 1
        )
    """).fetchone()[0]
    print(f"  cross-state politicians: {n_cross}  (should be 0)")

    print("\nDone. Next: push to Neon with:")
    print("    python scripts/sqlite_to_postgres.py --reset")


if __name__ == "__main__":
    main()
