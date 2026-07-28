"""One-shot resolver for the 261 residual gaps.

For each state's data/reports/gaps_<slug>_<year>.csv, this script:

  1. Loads the Gemini extraction JSON for each gap candidate.
  2. Uses the MANIFEST as authoritative source of (name, const, party).
  3. Tries to match to an existing election_appearances row via a
     party-aware lookup:
        primary key:  (norm_name, norm_const, norm_party) — 3-tuple.
        Uses BOTH short_name and full_name of the party in the DB
        so 'Bharatiya Janata Party' from manifest matches 'BJP' short_name.
  4. If a row is found:
        UPDATE it directly with the wealth/education/cases from Gemini.
        Bypasses the apply matcher entirely.
  5. If NO row is found (JAYVEER-style, candidate absent from seed):
        INSERT a new politician row + election_appearance row using
        manifest data for name/const/party and Gemini for the fields.

Writes to LOCAL SQLite (lokvani.db) — the DB the running site reads.
Never touches Postgres. Idempotent: re-running is safe.

Dry-run by default. --commit applies. Per-state --state / --year OK.

Usage:
    # See what would happen across all states
    python scripts/resolve_collision_gaps.py

    # Just one state
    python scripts/resolve_collision_gaps.py --state "Uttar Pradesh" --year 2022

    # Commit
    python scripts/resolve_collision_gaps.py --commit
"""
from __future__ import annotations
import argparse
import csv
import json
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

try:
    from slugify import slugify
except ImportError:
    def slugify(s):
        return re.sub(r"[^a-z0-9]+", "-", (s or "").lower()).strip("-")

ROOT = Path(__file__).resolve().parent.parent
DB_PATH = ROOT / "lokvani.db"


def _norm_name(s: str) -> str:
    if not s:
        return ""
    s = s.upper().strip()
    for prefix in ("DR. ", "DR ", "ADV. ", "ADV ", "ADVOCATE ",
                    "SHRI ", "SHRIMATI ", "SMT. ", "SMT ",
                    "MR. ", "MR ", "MS. ", "MS ", "MRS. ", "MRS "):
        if s.startswith(prefix):
            s = s[len(prefix):]
    for marker in (" S/O ", " D/O ", " W/O "):
        if marker in s:
            s = s.split(marker)[0]
    s = re.sub(r"\([^)]*\)", "", s)
    return re.sub(r"[^A-Z0-9]+", "", s)


def _norm_const(s: str) -> str:
    if not s:
        return ""
    s = s.upper().strip()
    for suf in ("(SC)", "(ST)", " SC", " ST"):
        if s.endswith(suf):
            s = s[: -len(suf)].strip()
    return re.sub(r"[^A-Z0-9]+", "", s)


def _norm_party(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", (s or "").lower())


def load_manifest(cycle_dir: Path) -> dict[str, dict]:
    """Basename -> manifest row."""
    idx = {}
    mf = cycle_dir / "manifest.jsonl"
    if not mf.exists():
        return idx
    for line in mf.read_text().splitlines():
        try:
            r = json.loads(line)
        except Exception:
            continue
        p = r.get("pdf_path") or ""
        if p:
            idx[Path(p).name] = r
    return idx


def build_appearance_lookup(cur, state: str, year: int) -> dict:
    """{(norm_name, norm_const, norm_party): appearance_id} for this state+year."""
    cur.execute("""
        SELECT ea.id, p.name, c.name,
               COALESCE(par.short_name, ''), COALESCE(par.full_name, '')
        FROM election_appearances ea
        JOIN politicians p ON ea.politician_id = p.id
        JOIN elections e ON ea.election_id = e.id
        JOIN constituencies c ON ea.constituency_id = c.id
        JOIN states s ON c.state_id = s.id
        LEFT JOIN parties par ON ea.party_id = par.id
        WHERE s.name = ? AND e.year = ?
    """, (state, year))
    by_full = {}
    by_nc = {}
    for app_id, name, const, ps, pf in cur.fetchall():
        n = _norm_name(name)
        c = _norm_const(const)
        for p in (_norm_party(ps), _norm_party(pf)):
            if p:
                by_full[(n, c, p)] = app_id
        by_nc.setdefault((n, c), []).append((app_id, ps, pf))
    return {"by_full": by_full, "by_nc": by_nc}


def get_or_create_id(cur, table, where_kwargs, insert_kwargs):
    where_clause = " AND ".join(f"{k} = ?" for k in where_kwargs)
    cur.execute(f"SELECT id FROM {table} WHERE {where_clause}",
                 list(where_kwargs.values()))
    row = cur.fetchone()
    if row:
        return row[0]
    all_kwargs = {**where_kwargs, **insert_kwargs}
    cols = ", ".join(all_kwargs)
    placeholders = ", ".join("?" for _ in all_kwargs)
    cur.execute(f"INSERT INTO {table} ({cols}) VALUES ({placeholders})",
                 list(all_kwargs.values()))
    return cur.lastrowid


def _i_or_none(v):
    if v is None:
        return None
    if isinstance(v, (int, float)):
        return int(v) if v == v else None  # NaN guard
    if isinstance(v, str):
        s = v.strip().replace(",", "").replace("₹", "")
        if not s or s.lower() in ("null", "nil", "n/a", "na"):
            return None
        try:
            return int(float(s))
        except ValueError:
            return None
    return None


def _grand_total(d, key_prefix):
    if not d:
        return None
    total = _i_or_none(d.get(f"{key_prefix}_inr"))
    if total is not None:
        return total
    parts = [_i_or_none(d.get(f"{key_prefix}_{k}_inr")) for k in
             ("self", "spouse", "huf", "dependents")]
    parts = [p for p in parts if p is not None]
    return sum(parts) if parts else None


def compute_wealth(ext):
    """Extract wealth fields from Gemini extraction dict."""
    am = ext.get("assets_movable") or {}
    ai = ext.get("assets_immovable") or {}
    li = ext.get("liabilities") or {}
    cp = ext.get("criminal_pending") or {}
    ident = ext.get("identity") or {}
    edu = ext.get("education") or {}
    prof = ext.get("profession") or {}

    mov = _grand_total(am, "total_movable_assets")
    imm = _grand_total(ai, "total_immovable_assets")
    liab = _grand_total(li, "total_liabilities")
    total = None
    if mov is not None or imm is not None:
        total = (mov or 0) + (imm or 0)

    return {
        "total_assets_inr":      total,
        "movable_assets_inr":    mov,
        "immovable_assets_inr":  imm,
        "total_liabilities_inr": liab,
        "criminal_cases_count":  _i_or_none(cp.get("pending_cases_count")),
        "serious_cases_count":   _i_or_none(cp.get("serious_cases_count")),
        "age":                   _i_or_none(ident.get("age_years")),
        "education":             edu.get("highest_degree"),
        "profession":            prof.get("occupation_summary") or prof.get("primary_occupation"),
    }


def resolve_gaps_for_state(cur, state: str, year: int,
                             cycle_slug: str, slug_year: str,
                             commit: bool) -> dict:
    stats = {"updated": 0, "inserted": 0, "no_gemini_data": 0,
             "no_manifest_row": 0, "skipped_has_wealth": 0}
    gap_csv = ROOT / f"data/reports/gaps_{slug_year}.csv"
    if not gap_csv.exists():
        print(f"  (no gap CSV — skip)")
        return stats
    cycle_dir = ROOT / f"data/eci/raw_pdfs/{cycle_slug}"
    if not cycle_dir.exists():
        cycle_dir = None
        for d in (ROOT / "data/eci/raw_pdfs").iterdir():
            if d.name.startswith(cycle_slug.split("-")[0]) and d.name.endswith(str(year)):
                cycle_dir = d
                break
    if not cycle_dir:
        return stats
    lx_dir = ROOT / f"data/eci/for_ai/llm_extracted/{slug_year}"

    manifest = load_manifest(cycle_dir)
    lookup = build_appearance_lookup(cur, state, year)
    by_full, by_nc = lookup["by_full"], lookup["by_nc"]

    now = datetime.now(timezone.utc).isoformat()

    # Look up election_id for this state/year (needed for INSERTs)
    cur.execute("""
        SELECT e.id, s.id FROM elections e
        JOIN states s ON e.state_id = s.id
        WHERE s.name = ? AND e.year = ?
        LIMIT 1
    """, (state, year))
    row = cur.fetchone()
    election_id, state_id = (row[0], row[1]) if row else (None, None)

    for row in csv.DictReader(gap_csv.open()):
        pdf = row["pdf"]
        stem = pdf[:-4] if pdf.endswith(".pdf") else pdf
        lx_path = lx_dir / (stem + ".json")
        if not lx_path.exists():
            continue
        try:
            g = json.loads(lx_path.read_text())
        except Exception:
            continue
        ext = g.get("extraction") or {}
        wealth = compute_wealth(ext)
        # Skip if Gemini didn't extract anything usable
        if wealth["total_assets_inr"] is None and wealth["education"] is None:
            stats["no_gemini_data"] += 1
            continue

        # Manifest is authoritative for name/const/party
        mfrow = manifest.get(pdf) or {}
        name  = (mfrow.get("name")  or row.get("candidate")     or "").strip()
        const = (mfrow.get("constituency") or row.get("constituency") or "").strip()
        party = (mfrow.get("party") or row.get("party")         or "").strip()
        if not name or not const:
            stats["no_manifest_row"] += 1
            continue
        n = _norm_name(name)
        c = _norm_const(const)
        p = _norm_party(party)

        # 1. Try exact 3-tuple match
        app_id = by_full.get((n, c, p)) if p else None
        # 2. Fallback: (name, const) with party alias check
        if not app_id:
            for aid, ps, pf in by_nc.get((n, c), []):
                for form in (_norm_party(ps), _norm_party(pf)):
                    if form and (form in p or p in form):
                        app_id = aid
                        break
                if app_id:
                    break
        # 3. Fallback: single candidate for (name, const)
        if not app_id and len(by_nc.get((n, c), [])) == 1:
            app_id = by_nc[(n, c)][0][0]

        if app_id:
            # CRITICAL: only fill rows where wealth is NULL. Prevents
            # overwriting valid wealth data on rows that were correctly
            # populated by an earlier apply run.
            if commit:
                cur.execute("SELECT total_assets_inr FROM election_appearances "
                             "WHERE id = ?", (app_id,))
                r = cur.fetchone()
                if r and r[0] is not None:
                    # Row already has wealth — skip this row entirely to
                    # avoid overwriting. The apply matcher landed the
                    # correct wealth here already.
                    stats.setdefault("skipped_has_wealth", 0)
                    stats["skipped_has_wealth"] += 1
                    continue
                cur.execute("""
                    UPDATE election_appearances SET
                        total_assets_inr      = ?,
                        movable_assets_inr    = ?,
                        immovable_assets_inr  = ?,
                        total_liabilities_inr = ?,
                        criminal_cases_count  = COALESCE(?, criminal_cases_count),
                        serious_cases_count   = COALESCE(?, serious_cases_count),
                        age                   = COALESCE(?, age),
                        education             = COALESCE(?, education),
                        profession            = COALESCE(?, profession)
                    WHERE id = ? AND total_assets_inr IS NULL
                """, (
                    wealth["total_assets_inr"], wealth["movable_assets_inr"],
                    wealth["immovable_assets_inr"], wealth["total_liabilities_inr"],
                    wealth["criminal_cases_count"], wealth["serious_cases_count"],
                    wealth["age"], wealth["education"], wealth["profession"],
                    app_id,
                ))
            stats["updated"] += 1
        else:
            # INSERT new politician + appearance row (JAYVEER-style)
            if not election_id or not state_id:
                print(f"  ✗ can't INSERT {name}: no election_id for {state} {year}")
                continue
            if commit:
                # Schema check (from PRAGMA):
                #   parties         — id, short_name, full_name  (no timestamps)
                #   constituencies  — id, name, state_id, house, reserved_for
                #   politicians     — has created_at + updated_at
                #   election_appearances — has scraped_at, no created_at
                party_id = get_or_create_id(cur, "parties",
                    {"short_name": party or "UNKNOWN"},
                    {"full_name": party or None})
                const_id = get_or_create_id(cur, "constituencies",
                    {"name": const, "state_id": state_id, "house": "Assembly"},
                    {})
                pol_id = get_or_create_id(cur, "politicians",
                    {"name": name},
                    {"slug": slugify(f"{name}-{state}-{year}"),
                     "created_at": now, "updated_at": now})
                cur.execute("""
                    INSERT INTO election_appearances
                        (politician_id, election_id, constituency_id, party_id,
                         age, education, profession, won,
                         total_assets_inr, total_liabilities_inr,
                         movable_assets_inr, immovable_assets_inr,
                         criminal_cases_count, serious_cases_count,
                         scraped_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, 0, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    pol_id, election_id, const_id, party_id,
                    wealth["age"], wealth["education"], wealth["profession"],
                    wealth["total_assets_inr"], wealth["total_liabilities_inr"],
                    wealth["movable_assets_inr"], wealth["immovable_assets_inr"],
                    wealth["criminal_cases_count"], wealth["serious_cases_count"],
                    now,
                ))
            stats["inserted"] += 1

    return stats


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                  formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--state", default="")
    ap.add_argument("--year", type=int, default=0)
    ap.add_argument("--commit", action="store_true")
    args = ap.parse_args()

    if not DB_PATH.exists():
        raise SystemExit(f"No SQLite at {DB_PATH}")

    con = sqlite3.connect(str(DB_PATH))
    cur = con.cursor()

    # State-cycle catalog
    STATES = [
        ("Andhra Pradesh", 2024, "andhrapradesh-2024", "andhrapradesh_2024"),
        ("Arunachal Pradesh", 2024, "arunachal-2024", "arunachal_2024"),
        ("Assam", 2026, "assam-2026", "assam_2026"),
        ("Bihar", 2025, "bihar-2025", "bihar_2025"),
        ("Chhattisgarh", 2023, "chhattisgarh-2023", "chhattisgarh_2023"),
        ("Delhi", 2020, "delhi-2020", "delhi_2020"),
        ("Delhi", 2025, "delhi-2025", "delhi_2025"),
        ("Goa", 2022, "goa-2022", "goa_2022"),
        ("Gujarat", 2022, "gujarat-2022", "gujarat_2022"),
        ("Haryana", 2019, "haryana-2019", "haryana_2019"),
        ("Haryana", 2024, "haryana-2024", "haryana_2024"),
        ("Himachal Pradesh", 2022, "himachal-2022", "himachal_2022"),
        ("Jammu and Kashmir", 2024, "jk-2024", "jk_2024"),
        ("Jharkhand", 2024, "jharkhand-2024", "jharkhand_2024"),
        ("Karnataka", 2023, "karnataka-2023", "karnataka_2023"),
        ("Kerala", 2026, "kerala-2026", "kerala_2026"),
        ("Madhya Pradesh", 2023, "madhyapradesh-2023", "madhyapradesh_2023"),
        ("Maharashtra", 2024, "maharashtra-2024", "maharashtra_2024"),
        ("Manipur", 2022, "manipur-2022", "manipur_2022"),
        ("Meghalaya", 2023, "meghalaya-2023", "meghalaya_2023"),
        ("Mizoram", 2023, "mizoram-2023", "mizoram_2023"),
        ("Nagaland", 2023, "nagaland-2023", "nagaland_2023"),
        ("Odisha", 2024, "odisha-2024", "odisha_2024"),
        ("Puducherry", 2021, "puducherry-2021", "puducherry_2021"),
        ("Punjab", 2022, "punjab-2022", "punjab_2022"),
        ("Rajasthan", 2023, "rajasthan-2023", "rajasthan_2023"),
        ("Sikkim", 2019, "sikkim-2019", "sikkim_2019"),
        ("Sikkim", 2024, "sikkim-2024", "sikkim_2024"),
        ("Tamil Nadu", 2026, "tamilnadu-2026", "tamilnadu_2026"),
        ("Telangana", 2023, "telangana-2023", "telangana_2023"),
        ("Tripura", 2023, "tripura-2023", "tripura_2023"),
        ("Uttarakhand", 2022, "uttarakhand-2022", "uttarakhand_2022"),
        ("Uttar Pradesh", 2022, "uttarpradesh-2022", "uttarpradesh_2022"),
        ("West Bengal", 2026, "westbengal-2026", "westbengal_2026"),
    ]
    if args.state:
        STATES = [s for s in STATES if s[0] == args.state
                  and (not args.year or s[1] == args.year)]

    tot = {"updated": 0, "inserted": 0, "no_gemini_data": 0, "no_manifest_row": 0}
    for state, year, cycle_slug, slug_year in STATES:
        print(f"\n→ {state} {year}")
        r = resolve_gaps_for_state(cur, state, year, cycle_slug, slug_year,
                                    args.commit)
        for k in tot:
            tot[k] += r[k]
        print(f"   updated: {r['updated']}  inserted: {r['inserted']}  "
              f"no_gemini: {r['no_gemini_data']}  no_manifest: {r['no_manifest_row']}  "
              f"skipped(has_wealth): {r.get('skipped_has_wealth', 0)}")

    if args.commit:
        con.commit()
        print(f"\n✓ Committed. Totals: updated={tot['updated']}  "
              f"inserted={tot['inserted']}  "
              f"skipped(no_gemini)={tot['no_gemini_data']}  "
              f"skipped(no_manifest)={tot['no_manifest_row']}")
    else:
        print(f"\n--- DRY RUN. Totals: would update={tot['updated']}  "
              f"would insert={tot['inserted']}  "
              f"skipped(no_gemini)={tot['no_gemini_data']}  "
              f"skipped(no_manifest)={tot['no_manifest_row']}")
    con.close()


if __name__ == "__main__":
    main()
