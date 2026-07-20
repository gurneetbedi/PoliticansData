"""
Apply LLM-extracted Form 26 fields to the canonical DB tables.

Reads every per-candidate JSON produced by `llm_extract_via_gemini.py`
and:

  - UPDATEs `election_appearances` with the 6 headline numeric fields
    (total_assets_inr, movable_assets_inr, immovable_assets_inr,
    total_liabilities_inr, criminal_cases_count, serious_cases_count)
    plus age, education, profession.

  - INSERTs detail rows into `assets` (per vehicle, per property),
    `liabilities` (per loan/dues), and `criminal_cases` (per pending
    and convicted case).

  - ADDS two columns to `election_appearances` if not present:
    `extraction_notes` (free text from the LLM) and
    `low_confidence_fields` (CSV of field names Gemini flagged).
    These power a "needs review" UI marker on uncertain fields.

MATCHING
========
Each LLM JSON has (state, election_year, affidavit_id). We use that
to find the candidate row in `eci_candidates_provisional`, then
normalize (name, constituency) and look up the matching
`election_appearance` row built by `migrate_to_eci_only.py`. The
normalization functions (name and constituency) are identical to
the ones in that migration script — keeping them in sync is important.

IDEMPOTENT
==========
Re-running clears the existing detail rows for an appearance before
inserting the new ones, so you can re-run safely if the LLM output
changes.

USAGE
=====
  # Apply all cycles
  python scripts/apply_llm_extraction.py

  # Apply a specific cycle only
  python scripts/apply_llm_extraction.py --cycles delhi_2025
  python scripts/apply_llm_extraction.py --cycles delhi_2020 delhi_2025

  # Dry run — show what would change, don't write
  python scripts/apply_llm_extraction.py --dry-run
"""
from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
from collections import defaultdict
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parent.parent
DB_PATH      = PROJECT_ROOT / "lokvani.db"
LLM_BASE     = PROJECT_ROOT / "data/eci/for_ai/llm_extracted"


# ---------------------------------------------------------------------------
# Normalization — MUST stay in sync with migrate_to_eci_only.py
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
# Schema bootstrap — add the two LLM-metadata columns if missing
# ---------------------------------------------------------------------------

def ensure_llm_columns(cur: sqlite3.Cursor):
    cols = {c[1] for c in cur.execute(
        "PRAGMA table_info(election_appearances)").fetchall()}
    if "extraction_notes" not in cols:
        cur.execute("ALTER TABLE election_appearances "
                    "ADD COLUMN extraction_notes TEXT")
        print("  Added column: election_appearances.extraction_notes",
              file=sys.stderr)
    if "low_confidence_fields" not in cols:
        cur.execute("ALTER TABLE election_appearances "
                    "ADD COLUMN low_confidence_fields TEXT")
        print("  Added column: election_appearances.low_confidence_fields",
              file=sys.stderr)
    # Per-relative wealth breakdown — powers the "Self / Spouse / HUF /
    # Dependents" reconciliation card on the politician detail page.
    for col in [
        "assets_self_inr", "assets_spouse_inr",
        "assets_huf_inr",  "assets_dependents_inr",
        "liabilities_self_inr", "liabilities_spouse_inr",
        "liabilities_huf_inr",  "liabilities_dependents_inr",
    ]:
        if col not in cols:
            cur.execute(f"ALTER TABLE election_appearances ADD COLUMN {col} INTEGER")
            print(f"  Added column: election_appearances.{col}", file=sys.stderr)


# ---------------------------------------------------------------------------
# Lookup tables — build once, query many
# ---------------------------------------------------------------------------

def _normalize_party(name: str) -> str:
    """Party normalization — lowercase, non-alnum stripped."""
    import re
    return re.sub(r"[^a-z0-9]+", "", (name or "").lower())


def build_appearance_lookup(cur: sqlite3.Cursor, year: int) -> dict:
    """Return TWO lookups:
      by_full: {(norm_name, norm_const, norm_party): appearance_id}
      by_nc:   {(norm_name, norm_const): [(appearance_id, party), ...]}

    The primary match uses `by_full` — party is a tiebreaker for
    same-name / same-constituency collisions (which happen when 2+
    candidates in one constituency share a normalized name).

    `by_nc` is the fallback for cases where the Gemini/provisional row
    lacks party information — we return a list so callers can decide
    whether it's ambiguous."""
    cur.execute("""
        SELECT ea.id, p.name, c.name, COALESCE(par.short_name, '')
        FROM election_appearances ea
        JOIN politicians    p   ON ea.politician_id   = p.id
        JOIN constituencies c   ON ea.constituency_id = c.id
        JOIN elections      e   ON ea.election_id     = e.id
        LEFT JOIN parties   par ON ea.party_id        = par.id
        WHERE e.year = ?
    """, (year,))
    by_full: dict[tuple[str, str, str], int] = {}
    by_nc: dict[tuple[str, str], list[tuple[int, str]]] = {}
    for app_id, name, const, party in cur.fetchall():
        n = _normalize_name(name)
        c = _normalize_constituency(const)
        p = _normalize_party(party)
        by_full[(n, c, p)] = app_id
        by_nc.setdefault((n, c), []).append((app_id, p))
    return {"by_full": by_full, "by_nc": by_nc}


def get_provisional_lookup(cur: sqlite3.Cursor, state: str, year: int) -> dict:
    """Return {affidavit_id: (name, constituency, party)} from provisional."""
    cur.execute("""
        SELECT affidavit_id, candidate_name, constituency, party
        FROM eci_candidates_provisional
        WHERE state = ? AND election_year = ?
              AND affidavit_status = 'Accepted'
    """, (state, year))
    return {row[0]: (row[1], row[2], row[3] if len(row) > 3 else "")
            for row in cur.fetchall()}


# ---------------------------------------------------------------------------
# Per-affidavit application
# ---------------------------------------------------------------------------

def _i_or_none(v):
    """Coerce LLM value to int or None. Tolerates strings with commas."""
    if v is None:
        return None
    if isinstance(v, int):
        return v
    if isinstance(v, float):
        return int(v) if not (v != v) else None  # NaN guard
    if isinstance(v, str):
        s = v.strip().replace(",", "").replace("₹", "")
        if not s or s.lower() in ("null", "nil", "n/a", "na"):
            return None
        try:
            return int(float(s))
        except ValueError:
            return None
    return None


def _grand_total(d: dict, key_prefix: str) -> int | None:
    """Sum self + spouse + huf + dependents if grand-total is missing."""
    grand = _i_or_none(d.get(f"{key_prefix}_inr"))
    if grand is not None:
        return grand
    parts = [
        _i_or_none(d.get(f"{key_prefix}_self_inr")),
        _i_or_none(d.get(f"{key_prefix}_spouse_inr")),
        _i_or_none(d.get(f"{key_prefix}_huf_inr")),
        _i_or_none(d.get(f"{key_prefix}_dependents_inr")),
    ]
    parts = [p for p in parts if p is not None]
    return sum(parts) if parts else None


def apply_one(cur: sqlite3.Cursor, llm_json: dict,
                appearance_lookup: dict, provisional_lookup: dict) -> str:
    """Apply one LLM extraction. Returns status string."""
    aff_id = llm_json.get("affidavit_id")
    extraction = llm_json.get("extraction")
    if not extraction or llm_json.get("skipped_corrupt"):
        return "skipped_no_extraction"

    # Match the LLM row to its election_appearance via provisional.
    # Primary key: (name, const, party). Fallback: (name, const) if
    # provisional has no party or the party-keyed lookup misses.
    prov = provisional_lookup.get(aff_id)
    if not prov:
        return "no_provisional_row"
    # Backward-compat: old provisional format returned 2-tuple. Detect
    # length before unpacking.
    if len(prov) == 3:
        name, const, party = prov
    else:
        name, const = prov
        party = ""
    n = _normalize_name(name)
    c = _normalize_constituency(const)
    p = _normalize_party(party)

    by_full = appearance_lookup["by_full"] if isinstance(appearance_lookup, dict) and "by_full" in appearance_lookup else appearance_lookup
    by_nc   = appearance_lookup.get("by_nc", {}) if isinstance(appearance_lookup, dict) else {}

    appearance_id = None
    # Primary: name+const+party
    if p:
        appearance_id = by_full.get((n, c, p))
    # Fallback: name+const only. If exactly one appearance row matches,
    # use it. If multiple, we can't safely pick — leave unmatched to
    # avoid attaching wealth to the wrong candidate.
    if not appearance_id:
        candidates = by_nc.get((n, c), [])
        if len(candidates) == 1:
            appearance_id = candidates[0][0]
        elif len(candidates) > 1 and p:
            # Try fuzzy party matching (e.g. "BJP" vs "Bharatiya Janata Party")
            for aid, ap in candidates:
                if ap and (ap in p or p in ap):
                    appearance_id = aid
                    break

    if not appearance_id:
        return f"no_appearance_match ({name} / {const} / {party})"

    # ---- Compute headline fields ----------------------------------------
    movable    = extraction.get("assets_movable")   or {}
    immovable  = extraction.get("assets_immovable") or {}
    liab       = extraction.get("liabilities")      or {}
    crim_pend  = extraction.get("criminal_pending") or {}
    crim_past  = extraction.get("criminal_past")    or {}

    mov_total = _grand_total(movable,   "total_movable_assets")
    imm_total = _grand_total(immovable, "total_immovable_assets")
    liab_total = _grand_total(liab,     "total_liabilities")

    # total_assets_inr = movable + immovable (the displayed grand total)
    total_assets = None
    if mov_total is not None or imm_total is not None:
        total_assets = (mov_total or 0) + (imm_total or 0)

    # Per-relative breakdown = movable_<who> + immovable_<who>. Populated
    # so the detail page can show a Self / Spouse / HUF / Dependents card
    # that reconciles the itemised assets to the grand total.
    def _sum_pair(m_key, i_key):
        m = _i_or_none(movable.get(m_key))
        i = _i_or_none(immovable.get(i_key))
        if m is None and i is None:
            return None
        return (m or 0) + (i or 0)

    assets_self       = _sum_pair("total_movable_assets_self_inr",
                                    "total_immovable_assets_self_inr")
    assets_spouse     = _sum_pair("total_movable_assets_spouse_inr",
                                    "total_immovable_assets_spouse_inr")
    assets_huf        = _sum_pair("total_movable_assets_huf_inr",
                                    "total_immovable_assets_huf_inr")
    assets_dependents = _sum_pair("total_movable_assets_dependents_inr",
                                    "total_immovable_assets_dependents_inr")

    liab_self       = _i_or_none(liab.get("total_liabilities_self_inr"))
    liab_spouse     = _i_or_none(liab.get("total_liabilities_spouse_inr"))
    liab_huf        = _i_or_none(liab.get("total_liabilities_huf_inr"))
    liab_dependents = _i_or_none(liab.get("total_liabilities_dependents_inr"))

    pending_count  = _i_or_none(crim_pend.get("pending_cases_count")) or 0
    convicted_count = _i_or_none(crim_past.get("convicted_cases_count")) or 0
    # "Serious" cases = convicted (Section 8 disqualification trigger) +
    # charges_framed-flagged pending cases. For simplicity, use convicted
    # as the serious-cases baseline; framed pending need detail-table check.
    serious_count = convicted_count
    if crim_pend.get("has_charges_framed"):
        # Inspect pending_cases_detail for charges_framed=True rows
        for c in (extraction.get("pending_cases_detail") or []):
            if c.get("charges_framed"):
                serious_count += 1

    # Education + profession (text fields) from LLM
    education = (extraction.get("education")  or {}).get("highest_qualification")
    profession = (extraction.get("profession") or {}).get("profession_self")
    age = _i_or_none((extraction.get("identity") or {}).get("age_years"))

    # LLM metadata
    meta = extraction.get("extraction_metadata") or {}
    notes = meta.get("extraction_notes") or []
    low_conf = meta.get("fields_low_confidence") or []

    # ---- UPDATE election_appearances ------------------------------------
    cur.execute("""
        UPDATE election_appearances
        SET total_assets_inr      = ?,
            movable_assets_inr    = ?,
            immovable_assets_inr  = ?,
            total_liabilities_inr = ?,
            criminal_cases_count  = ?,
            serious_cases_count   = ?,
            education             = COALESCE(?, education),
            profession            = COALESCE(?, profession),
            age                   = COALESCE(?, age),
            extraction_notes      = ?,
            low_confidence_fields = ?,
            assets_self_inr       = ?,
            assets_spouse_inr     = ?,
            assets_huf_inr        = ?,
            assets_dependents_inr = ?,
            liabilities_self_inr       = ?,
            liabilities_spouse_inr     = ?,
            liabilities_huf_inr        = ?,
            liabilities_dependents_inr = ?
        WHERE id = ?
    """, (
        total_assets, mov_total, imm_total, liab_total,
        pending_count, serious_count,
        education, profession, age,
        " | ".join(notes) if notes else None,
        ",".join(low_conf) if low_conf else None,
        assets_self, assets_spouse, assets_huf, assets_dependents,
        liab_self,   liab_spouse,   liab_huf,   liab_dependents,
        appearance_id,
    ))

    # ---- Detail tables: clear existing, insert from LLM extraction ------
    cur.execute("DELETE FROM criminal_cases WHERE appearance_id = ?",
                (appearance_id,))
    cur.execute("DELETE FROM assets         WHERE appearance_id = ?",
                (appearance_id,))
    cur.execute("DELETE FROM liabilities    WHERE appearance_id = ?",
                (appearance_id,))

    # criminal_cases — pending + convicted
    for c in (extraction.get("pending_cases_detail") or []):
        cur.execute("""
            INSERT INTO criminal_cases
              (appearance_id, ipc_sections, description, case_number,
               court, charges_framed, is_serious, status)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            appearance_id,
            ",".join(c.get("ipc_sections") or []) or None,
            "; ".join(filter(None, [c.get("fir_number")] +
                                       (c.get("other_acts") or []))) or None,
            c.get("case_number"),
            c.get("court_name"),
            bool(c.get("charges_framed")),
            bool(c.get("charges_framed")),
            c.get("current_status") or "Pending",
        ))
    for c in (extraction.get("convicted_cases_detail") or []):
        cur.execute("""
            INSERT INTO criminal_cases
              (appearance_id, ipc_sections, description, case_number,
               court, charges_framed, is_serious, status)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            appearance_id,
            ",".join(c.get("ipc_sections") or []) or None,
            c.get("sentence"),
            c.get("case_number"),
            c.get("court_name"),
            True, True,           # convicted ⇒ charges_framed + serious
            "Convicted",
        ))

    # assets — vehicles + immovable details
    #
    # Schema convention (matched by detail.html icon mapping):
    #   category    = "movable" | "immovable"
    #   subcategory = human-readable type (Vehicle, Residential Building,
    #                 Agricultural Land, etc.). The template's icon-picker
    #                 looks for substrings 'vehicle', 'building', 'land',
    #                 'cash', 'bank', 'jewel', 'invest', etc. in
    #                 subcategory.lower() — so we put those keywords in
    #                 subcategory, not in category.
    #   description = location/declarant/details bundled together
    for v in (extraction.get("vehicles_detail") or []):
        desc_parts = [v.get("description"), v.get("declarant")]
        cur.execute("""
            INSERT INTO assets (appearance_id, category, subcategory,
                                description, value_inr)
            VALUES (?, ?, ?, ?, ?)
        """, (
            appearance_id, "movable", "Vehicle",
            " · ".join(filter(None, desc_parts)) or None,
            _i_or_none(v.get("value_inr")),
        ))

    for p in (extraction.get("immovable_detail") or []):
        # Normalize property_type to a human-readable subcategory the
        # template's icon-picker will match. E.g. "residential_building"
        # → "Residential Building" → matches 'building' → home icon.
        ptype_raw = (p.get("property_type") or "").strip()
        if ptype_raw:
            subcat = ptype_raw.replace("_", " ").replace("-", " ").title()
        else:
            subcat = "Property"

        descr_parts = []
        if p.get("location_city"):
            descr_parts.append(p["location_city"])
        if p.get("location_district") and \
                p.get("location_district") != p.get("location_city"):
            descr_parts.append(p["location_district"])
        if p.get("area_description"):
            descr_parts.append(p["area_description"])
        if p.get("declarant"):
            descr_parts.append(p["declarant"])
        if p.get("ancestral_or_self_acquired"):
            descr_parts.append(p["ancestral_or_self_acquired"])

        cur.execute("""
            INSERT INTO assets (appearance_id, category, subcategory,
                                description, value_inr)
            VALUES (?, ?, ?, ?, ?)
        """, (
            appearance_id, "immovable", subcat,
            " · ".join(descr_parts) or None,
            _i_or_none(p.get("value_inr")),
        ))

    # liabilities
    for l in (extraction.get("liabilities_detail") or []):
        cur.execute("""
            INSERT INTO liabilities (appearance_id, creditor, description,
                                       amount_inr)
            VALUES (?, ?, ?, ?)
        """, (
            appearance_id, l.get("lender"),
            " · ".join(filter(None,
                                [l.get("liability_type"),
                                 l.get("declarant"),
                                 l.get("purpose")])) or None,
            _i_or_none(l.get("amount_inr")),
        ))

    return "applied"


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                  formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--cycles", nargs="*", default=None,
                    help="Subdirs of data/eci/for_ai/llm_extracted/ to "
                         "process (e.g. 'delhi_2025 delhi_2020'). Default: "
                         "every cycle subdir found.")
    ap.add_argument("--db", default=str(DB_PATH))
    ap.add_argument("--dry-run", action="store_true",
                    help="Compute matches and print stats; don't write.")
    ap.add_argument("--allowlist-scope", action="store_true",
                    help="Restrict each cycle to its top-N allowlist "
                         "(data/allowlists/<slug_year>[_topN].txt). "
                         "Extractions of fringe candidates outside the "
                         "allowlist are skipped. Recommended for quality "
                         "control — matches the state_status_report scope.")
    args = ap.parse_args()

    db = Path(args.db)
    if not db.exists():
        sys.exit(f"DB not found: {db}")
    if not LLM_BASE.exists():
        sys.exit(f"LLM output base dir not found: {LLM_BASE}")

    # Determine cycle subdirs to process
    if args.cycles:
        cycle_dirs = [LLM_BASE / c for c in args.cycles]
    else:
        cycle_dirs = sorted(p for p in LLM_BASE.iterdir() if p.is_dir())
    if not cycle_dirs:
        sys.exit(f"No cycle subdirs in {LLM_BASE}. "
                 "Run llm_extract_via_gemini.py first.")

    con = sqlite3.connect(str(db))
    cur = con.cursor()

    if not args.dry_run:
        ensure_llm_columns(cur)
        con.commit()

    counts: dict[str, int] = defaultdict(int)

    for cycle_dir in cycle_dirs:
        # Find any one JSON to learn (state, year) for this cycle
        sample_path = next(cycle_dir.glob("*.json"), None)
        if not sample_path:
            print(f"  (skip empty cycle dir: {cycle_dir.name})", file=sys.stderr)
            continue
        sample = json.loads(sample_path.read_text())
        state = sample.get("state")
        year  = sample.get("election_year")
        if not state or not year:
            print(f"  ⚠ cycle {cycle_dir.name}: sample JSON missing "
                  f"state/year — skipping", file=sys.stderr)
            continue

        print(f"\n→ Cycle: {state} / {year}  ({cycle_dir.name})",
              file=sys.stderr)

        appearance_lookup  = build_appearance_lookup(cur, year)
        provisional_lookup = get_provisional_lookup(cur, state, year)
        _by_full = appearance_lookup.get("by_full", {}) \
            if isinstance(appearance_lookup, dict) else {}
        print(f"  Appearance rows in DB for {year}: {len(_by_full)}",
              file=sys.stderr)
        print(f"  Provisional rows: {len(provisional_lookup)}",
              file=sys.stderr)

        # Optional: restrict to the top-N allowlist for this cycle
        allow_stems: set[str] | None = None
        if args.allowlist_scope:
            allow_dir = PROJECT_ROOT / "data/allowlists"
            slug_year = cycle_dir.name   # e.g. rajasthan_2023
            allow_path = allow_dir / f"{slug_year}.txt"
            if not allow_path.exists():
                matches = sorted(allow_dir.glob(f"{slug_year}_top*.txt"))
                allow_path = matches[0] if matches else None
            if allow_path and allow_path.exists():
                allow_stems = {ln.strip()[:-4]
                               for ln in allow_path.read_text().splitlines()
                               if ln.strip().endswith(".pdf")}
                print(f"  Allowlist scope: {allow_path.name} "
                      f"({len(allow_stems)} candidates)", file=sys.stderr)
            else:
                print(f"  ⚠ --allowlist-scope requested but no allowlist "
                      f"file for {slug_year} — skipping this cycle",
                      file=sys.stderr)
                continue

        for llm_path in sorted(cycle_dir.glob("*.json")):
            if allow_stems is not None and llm_path.stem not in allow_stems:
                # Out of scope for this run — silently skip
                continue
            try:
                llm_json = json.loads(llm_path.read_text())
            except json.JSONDecodeError as e:
                print(f"  ✗ {llm_path.name}: bad JSON: {e}", file=sys.stderr)
                counts["bad_json"] += 1
                continue

            if args.dry_run:
                # Just probe the match — mirrors apply_one's 3-tuple key
                # with name+const fallback, so dry-run stats match what
                # commit would actually do.
                aff_id = llm_json.get("affidavit_id")
                prov = provisional_lookup.get(aff_id)
                if not prov:
                    counts["no_provisional_row"] += 1
                    continue
                name = prov[0]
                const = prov[1]
                party = prov[2] if len(prov) >= 3 else ""
                n = _normalize_name(name)
                c = _normalize_constituency(const)
                p = _normalize_party(party)
                by_full = appearance_lookup.get("by_full", appearance_lookup) \
                    if isinstance(appearance_lookup, dict) else {}
                by_nc = appearance_lookup.get("by_nc", {}) \
                    if isinstance(appearance_lookup, dict) else {}
                hit_id = by_full.get((n, c, p)) if p else None
                if not hit_id:
                    cands = by_nc.get((n, c), [])
                    if len(cands) == 1:
                        hit_id = cands[0][0]
                    elif len(cands) > 1 and p:
                        for aid, ap in cands:
                            if ap and (ap in p or p in ap):
                                hit_id = aid
                                break
                if hit_id:
                    counts["would_apply"] += 1
                else:
                    counts["no_appearance_match"] += 1
                continue

            try:
                status = apply_one(cur, llm_json,
                                     appearance_lookup, provisional_lookup)
            except Exception as e:
                print(f"  ✗ {llm_path.name}: {type(e).__name__}: {e}",
                      file=sys.stderr)
                counts["error"] += 1
                continue
            counts[status] += 1

        if not args.dry_run:
            con.commit()
            print(f"  ✓ committed cycle {state} {year}", file=sys.stderr)

    con.close()

    print(f"\n========== APPLY SUMMARY ==========", file=sys.stderr)
    for k, v in sorted(counts.items()):
        print(f"  {k:30s}  {v}", file=sys.stderr)

    if args.dry_run:
        print("\n(dry-run — no DB writes)", file=sys.stderr)


if __name__ == "__main__":
    main()
