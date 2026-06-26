"""
Business-logic computations used by the homepage and the JSON API.

Kept separate from main.py so the same functions power both HTML views and
JSON endpoints, and so they can be unit-tested without spinning up FastAPI.
"""
import random
from typing import Optional

from sqlalchemy import func, distinct
from sqlalchemy.orm import Session, joinedload

from app.models import (
    Politician, ElectionAppearance, Election, Party, Constituency, State,
)
from app.states import ALL_STATES


def zone_summary(db: Session) -> list[dict]:
    """
    Aggregate every tracked state's current-cycle MLAs into geographic zones
    (North / South / East / West / Northeast / Central) and compute a single
    Transparency % per zone.

    Returns one dict per non-empty zone:
        - name:         "North"
        - mlas:          total MLAs across this zone's states
        - states:        list of state names in this zone that have data
        - transparency:  % of MLAs in the zone with zero pending criminal cases
        - avg_cases:     average pending cases per MLA across the zone

    The number of zones returned only counts those that contain at least one
    state with data. Empty zones are omitted so the panel doesn't list a
    "South Zone — 0 MLAs" placeholder until at least one southern state ships.
    """
    by_zone = {}
    for key, cfg in ALL_STATES.items():
        if not cfg.zone:
            continue
        # Primary path: winners only ("MLAs"). ECI source has no win flag
        # yet, so this is empty for Delhi today. Fall back to all accepted
        # candidates so the zone panel still populates with neutral counts.
        apps = _latest_appearances(db, house="Assembly", scope="current",
                                    state_name=cfg.name)
        fallback = False
        if not apps:
            apps = _latest_appearances(db, house="Assembly", scope="all",
                                        state_name=cfg.name)
            fallback = True
        if not apps:
            continue
        bucket = by_zone.setdefault(cfg.zone, {"mlas": 0,
                                                "verified": 0,
                                                "clean": 0,
                                                "cases": 0,
                                                "states": [],
                                                "fallback_states": []})
        for a in apps:
            bucket["mlas"] += 1
            # IMPORTANT: do NOT treat NULL criminal_cases_count as 0/clean.
            # A NULL means we haven't extracted that field yet (Phase 5
            # LLM extraction not run for that row). Only candidates whose
            # cases field is genuinely set (could be 0) count toward the
            # verified / clean / cases totals — otherwise the % is
            # massively inflated by the unverified majority.
            if a.criminal_cases_count is not None:
                bucket["verified"] += 1
                if a.criminal_cases_count == 0:
                    bucket["clean"] += 1
                bucket["cases"] += a.criminal_cases_count
        bucket["states"].append(cfg.name)
        if fallback:
            bucket["fallback_states"].append(cfg.name)

    out = []
    for zone, d in by_zone.items():
        if d["mlas"] == 0:
            continue
        is_fallback = bool(d["fallback_states"])
        verified = d["verified"]
        # transparency % is meaningful only across VERIFIED candidates.
        # If we have zero verified rows in the zone, hide the % rather
        # than show a meaningless "0% Clean" or division-by-zero error.
        if verified > 0:
            transparency = round(100 * d["clean"] / verified, 0)
            avg_cases    = round(d["cases"] / verified, 2)
        else:
            transparency = None
            avg_cases    = None
        out.append({
            "name":              zone,
            "mlas":              d["mlas"],
            "verified":          verified,
            "states":            sorted(d["states"]),
            "transparency":      transparency,
            "avg_cases":         avg_cases,
            "fallback":          is_fallback,
            "label":             "candidates" if is_fallback else "MLAs",
            # Coverage label for the template: e.g., "10 of 1224 verified"
            "verified_label":    (f"{verified} of {d['mlas']} verified"
                                   if verified < d["mlas"] else None),
        })
    # Sort by transparency descending — None last so unverified zones
    # sink to the bottom of the list.
    out.sort(key=lambda z: (z["transparency"] is None,
                              -(z["transparency"] or 0)))
    return out


def coverage_summary(db: Session) -> list[dict]:
    """
    Per-state coverage report comparing the registry (app/states.py — every
    cycle myneta has) against the local DB (cycles we've actually ingested).

    Returns one dict per state, ordered the same way as TRACKED_STATE_NAMES,
    with three pieces of information for the Data Coverage banner:
        - status:    "full"    → every declared cycle has data
                     "partial" → at least one cycle scraped, but not all
                     "none"    → no data yet (registered but not scraped)
        - loaded:    list of cycle years actually in the DB for this state
        - missing:   list of cycle years declared but absent
        - notes:     free-text from StateConfig.coverage_notes (e.g.
                     "detail enrichment ~50%") — appended even on "full" status.
    """
    # Pull every (state_name, year) pair that actually has appearance data.
    # NOT just election-row existence: previous ingest attempts may have
    # created Election rows for cycles whose candidate scrape failed, which
    # would falsely classify the state as "full" if we joined only on
    # state→elections. We join all the way through to election_appearances
    # so a state-year combo only counts if at least one candidate landed.
    have = {}
    rows = (
        db.query(State.name, Election.year)
        .join(Election, Election.state_id == State.id)
        .join(ElectionAppearance, ElectionAppearance.election_id == Election.id)
        .filter(Election.house == "Assembly")
        .distinct()
        .all()
    )
    for name, year in rows:
        have.setdefault(name, set()).add(year)

    out = []
    for key, cfg in ALL_STATES.items():
        declared_years = {c["year"] for c in cfg.assembly_cycles}
        loaded_years = have.get(cfg.name, set()) & declared_years
        missing_years = declared_years - loaded_years
        if not loaded_years:
            status = "none"
        elif missing_years:
            status = "partial"
        else:
            status = "full"
        out.append({
            "key":    key,
            "name":   cfg.name,
            "status": status,
            "loaded":  sorted(loaded_years, reverse=True),
            "missing": sorted(missing_years, reverse=True),
            "notes":   cfg.coverage_notes,
        })
    return out


# Brand colors for major Punjab parties. Used in badges, the scatter chart,
# and constituency tiles. Falls back to grey for unknown parties.
PARTY_COLORS: dict[str, str] = {
    "AAP":  "#019cdf",
    "INC":  "#19aaed",
    "BJP":  "#ff9933",
    "SAD":  "#1a3399",
    "BSP":  "#22336d",
    "SAD(B)": "#1a3399",
    "CPI":  "#c0392b",
    "CPM":  "#c0392b",
    "IND":  "#7f8c8d",
    "NOTA": "#34495e",
}

CRORE = 10_000_000  # 1 crore = 10 million rupees


def party_color(short_name: Optional[str]) -> str:
    if not short_name:
        return "#7f8c8d"
    return PARTY_COLORS.get(short_name.upper(), "#7f8c8d")


# ---------------- Leaderboards ------------------------------------------------

def _latest_appearance_subquery(db: Session, house: Optional[str] = None):
    """Subquery returning, for each politician, the appearance from their
    most recent election cycle (by year). If `house` is provided, restricts
    to appearances in that house (Assembly / LokSabha / RajyaSabha)."""
    q = db.query(
        ElectionAppearance.politician_id.label("pid"),
        func.max(Election.year).label("max_year"),
    ).join(Election, ElectionAppearance.election_id == Election.id)
    if house:
        q = q.filter(Election.house == house)
    return q.group_by(ElectionAppearance.politician_id).subquery()


def _latest_appearances(
    db: Session,
    house: Optional[str] = None,
    scope: str = "all",
    state_name: Optional[str] = None,
) -> list[ElectionAppearance]:
    """
    Return one ElectionAppearance per politician — their most recent one.

    house        — only consider appearances in this house (Assembly/LokSabha/RS)
    scope='all'      — every politician who ever won
    scope='current'  — only politicians who won in the latest cycle for this house
    state_name   — only consider appearances in the given state (e.g. "Punjab", "Bihar")
    """
    def add_state_filter(q):
        if state_name:
            q = q.join(State, Election.state_id == State.id).filter(State.name == state_name)
        return q

    if scope == "current":
        max_year_q = db.query(func.max(Election.year))
        if house:
            max_year_q = max_year_q.filter(Election.house == house)
        if state_name:
            max_year_q = max_year_q.join(State, Election.state_id == State.id).filter(State.name == state_name)
        max_year = max_year_q.scalar()
        if not max_year:
            return []
        q = (
            db.query(ElectionAppearance)
            .join(Election, ElectionAppearance.election_id == Election.id)
            .filter(Election.year == max_year)
            .filter(ElectionAppearance.won.is_(True))
            .options(
                joinedload(ElectionAppearance.politician),
                joinedload(ElectionAppearance.party),
                joinedload(ElectionAppearance.constituency),
                joinedload(ElectionAppearance.election),
            )
        )
        if house:
            q = q.filter(Election.house == house)
        q = add_state_filter(q)
        return q.all()

    sub = _latest_appearance_subquery(db, house=house)
    q = (
        db.query(ElectionAppearance)
        .join(Election, ElectionAppearance.election_id == Election.id)
        .join(sub, (sub.c.pid == ElectionAppearance.politician_id) &
                   (sub.c.max_year == Election.year))
        .options(
            joinedload(ElectionAppearance.politician),
            joinedload(ElectionAppearance.party),
            joinedload(ElectionAppearance.constituency),
            joinedload(ElectionAppearance.election),
        )
    )
    if house:
        q = q.filter(Election.house == house)
    q = add_state_filter(q)
    return q.all()


def top_by_wealth(db: Session, limit: int = 10, house: str = "Assembly", scope: str = "all", state_name: Optional[str] = None) -> list[ElectionAppearance]:
    apps = _latest_appearances(db, house=house, scope=scope, state_name=state_name)
    apps = [a for a in apps if a.total_assets_inr]
    return sorted(apps, key=lambda a: a.total_assets_inr or 0, reverse=True)[:limit]


def top_by_cases(db: Session, limit: int = 10, house: str = "Assembly", scope: str = "all", state_name: Optional[str] = None) -> list[ElectionAppearance]:
    apps = _latest_appearances(db, house=house, scope=scope, state_name=state_name)
    apps = [a for a in apps if a.criminal_cases_count and a.criminal_cases_count > 0]
    return sorted(apps, key=lambda a: a.criminal_cases_count or 0, reverse=True)[:limit]


def biggest_wealth_movers(db: Session, limit: int = 10, direction: str = "gain") -> list[dict]:
    """
    For politicians with at least 2 appearances, compute the rupee delta
    between earliest and latest declared wealth.
    direction: 'gain' returns biggest increases, 'loss' returns biggest decreases.
    """
    politicians = (
        db.query(Politician)
        .options(
            joinedload(Politician.appearances).joinedload(ElectionAppearance.election),
            joinedload(Politician.appearances).joinedload(ElectionAppearance.party),
            joinedload(Politician.appearances).joinedload(ElectionAppearance.constituency),
        )
        .all()
    )
    results = []
    for p in politicians:
        valid = [a for a in p.appearances
                 if a.election and a.total_assets_inr is not None]
        if len(valid) < 2:
            continue
        valid.sort(key=lambda a: a.election.year)
        first, last = valid[0], valid[-1]
        if not first.total_assets_inr:
            continue
        delta = (last.total_assets_inr or 0) - first.total_assets_inr
        pct = (delta / first.total_assets_inr) * 100 if first.total_assets_inr else 0
        results.append({
            "politician": p,
            "latest": last,
            "first_year": first.election.year,
            "last_year": last.election.year,
            "from_inr": first.total_assets_inr,
            "to_inr": last.total_assets_inr,
            "delta": delta,
            "pct": pct,
        })
    reverse = (direction == "gain")
    results.sort(key=lambda r: r["delta"], reverse=reverse)
    return results[:limit]


# ---------------- Trends ------------------------------------------------------

def trends_by_cycle(db: Session, state_name: Optional[str] = None) -> list[dict]:
    """Per-cycle aggregates: # of winners, avg wealth, crorepati count, % with cases.
    If state_name is provided, restricts to that state's elections."""
    q = db.query(Election)
    if state_name:
        q = q.join(State, Election.state_id == State.id).filter(State.name == state_name)
    elections = q.order_by(Election.year).all()
    out = []
    for e in elections:
        apps = (
            db.query(ElectionAppearance)
            .filter(ElectionAppearance.election_id == e.id)
            .filter(ElectionAppearance.won.is_(True))
            .all()
        )
        if not apps:
            continue
        wealths = [a.total_assets_inr or 0 for a in apps]
        with_cases = sum(1 for a in apps if (a.criminal_cases_count or 0) > 0)
        crorepati = sum(1 for w in wealths if w >= CRORE)
        avg = sum(wealths) / len(wealths) if wealths else 0
        out.append({
            "year": e.year,
            "total_winners": len(apps),
            "avg_wealth_inr": int(avg),
            "avg_wealth_cr": round(avg / CRORE, 2),
            "crorepati_count": crorepati,
            "crorepati_pct": round(100 * crorepati / len(apps), 1),
            "with_cases_count": with_cases,
            "with_cases_pct": round(100 * with_cases / len(apps), 1),
        })
    return out


# ---------------- Party comparison -------------------------------------------

def party_stats(db: Session, election_year: Optional[int] = None, state_name: Optional[str] = None) -> list[dict]:
    """Aggregate stats per party, optionally restricted to one election year/state."""
    q = (
        db.query(ElectionAppearance)
        .join(Party, ElectionAppearance.party_id == Party.id)
        .join(Election, ElectionAppearance.election_id == Election.id)
        .filter(ElectionAppearance.won.is_(True))
        .options(joinedload(ElectionAppearance.party))
    )
    if election_year:
        q = q.filter(Election.year == election_year)
    if state_name:
        q = q.join(State, Election.state_id == State.id).filter(State.name == state_name)
    apps = q.all()

    by_party: dict[str, list[ElectionAppearance]] = {}
    for a in apps:
        if a.party:
            by_party.setdefault(a.party.short_name, []).append(a)

    rows = []
    for name, group in by_party.items():
        wealths = [a.total_assets_inr or 0 for a in group]
        cases = [a.criminal_cases_count or 0 for a in group]
        rows.append({
            "party": name,
            "color": party_color(name),
            "count": len(group),
            "avg_wealth_cr": round(sum(wealths) / len(group) / CRORE, 2) if group else 0,
            "median_wealth_cr": round(sorted(wealths)[len(wealths)//2] / CRORE, 2) if wealths else 0,
            "with_cases_count": sum(1 for c in cases if c > 0),
            "with_cases_pct": round(100 * sum(1 for c in cases if c > 0) / len(group), 1) if group else 0,
            "crorepati_count": sum(1 for w in wealths if w >= CRORE),
        })
    rows.sort(key=lambda r: r["count"], reverse=True)
    return rows


# ---------------- Scatter & constituency map ---------------------------------

def scatter_points(db: Session, state_name: Optional[str] = None) -> list[dict]:
    """Every politician's latest appearance as a (wealth, cases, party, slug) tuple."""
    apps = _latest_appearances(db, state_name=state_name)
    return [
        {
            "name": a.politician.display_name,
            "slug": a.politician.slug or str(a.politician.id),
            "wealth_cr": round((a.total_assets_inr or 0) / CRORE, 2),
            "cases": a.criminal_cases_count or 0,
            "party": a.party.short_name if a.party else "IND",
            "color": party_color(a.party.short_name if a.party else None),
            "constituency": a.constituency.name if a.constituency else "",
            "year": a.election.year if a.election else None,
        }
        for a in apps
    ]


def constituency_tiles(db: Session, year: Optional[int] = None, state_name: Optional[str] = None) -> list[dict]:
    """
    Per-constituency latest data for the map. For every AC we look up the
    containing Lok Sabha PC and attach the current MP, so the homepage hover
    can show MLA + MP together.
    """
    from app.ac_to_ls import ls_pc_for_ac

    # ---- Latest MP by LS PC (across LS election years) -----------------------
    mp_by_pc: dict[str, dict] = {}
    ls_apps = (
        db.query(ElectionAppearance)
        .join(Election, ElectionAppearance.election_id == Election.id)
        .filter(Election.house == "LokSabha")
        .filter(ElectionAppearance.won.is_(True))
        .options(
            joinedload(ElectionAppearance.politician),
            joinedload(ElectionAppearance.party),
            joinedload(ElectionAppearance.constituency),
            joinedload(ElectionAppearance.election),
        )
        .all()
    )
    # For each LS PC keep the most recent winner
    for a in ls_apps:
        if not a.constituency or not a.election:
            continue
        key = a.constituency.name.upper().strip()
        existing = mp_by_pc.get(key)
        if not existing or a.election.year > existing["_year"]:
            mp_by_pc[key] = {
                "name": a.politician.display_name,
                "slug": a.politician.slug or str(a.politician.id),
                "party": a.party.short_name if a.party else "IND",
                "color": party_color(a.party.short_name if a.party else None),
                "wealth_cr": round((a.total_assets_inr or 0) / CRORE, 2),
                "cases": a.criminal_cases_count or 0,
                "year": a.election.year,
                "constituency": a.constituency.name,
                "_year": a.election.year,
            }

    # ---- Per-constituency MLA snapshots --------------------------------------
    apps = _latest_appearances(db, house="Assembly", state_name=state_name)
    if year:
        apps = [a for a in apps if a.election and a.election.year == year]

    tiles = []
    for a in apps:
        if not a.constituency or not a.election:
            continue

        # Look up the containing LS PC for this AC
        ls_pc = ls_pc_for_ac(a.constituency.name)
        mp_info = None
        if ls_pc:
            mp_info = mp_by_pc.get(ls_pc.upper().strip())

        tiles.append({
            "constituency": a.constituency.name,
            "mla": a.politician.display_name,
            "slug": a.politician.slug or str(a.politician.id),
            "party": a.party.short_name if a.party else "IND",
            "color": party_color(a.party.short_name if a.party else None),
            "wealth_cr": round((a.total_assets_inr or 0) / CRORE, 2),
            "cases": a.criminal_cases_count or 0,
            "year": a.election.year,
            "ls_pc": ls_pc or "",
            "mp": mp_info,   # may be None if we don't have LS data yet
        })
    tiles.sort(key=lambda t: t["constituency"])
    return tiles


# ---------------- Constituency dot map -----------------------------------------
# Lazy-load the centroid file once per process. The file is nested by state to
# avoid cross-state name collisions:
#     {"Punjab": {"ABOHAR": {lat, lng}}, "Bihar": {"PATNA SAHIB": {lat, lng}}}
# Backward-compatible with the older flat format ({"ABOHAR": {lat, lng}}) —
# the migration is documented in scripts/geocode_constituencies.py.
_constituency_coords_cache: dict[str, dict] = {}

def _load_constituency_coords() -> dict[str, dict]:
    """Read the coords file and normalize it to the nested-by-state shape."""
    global _constituency_coords_cache
    if _constituency_coords_cache:
        return _constituency_coords_cache
    import json
    from pathlib import Path
    path = Path(__file__).resolve().parent / "static" / "constituency_coords.json"
    if not path.exists():
        return {}
    try:
        with open(path) as f:
            data = json.load(f)
    except Exception:
        return {}

    if not isinstance(data, dict) or not data:
        return {}

    # Detect old flat format: top-level values are {lat, lng} dicts, not
    # per-state buckets. If so, wrap into the new shape under "Punjab"
    # (every historical flat-keyed file was Punjab-only).
    first_val = next(iter(data.values()), {})
    if isinstance(first_val, dict) and "lat" in first_val and "lng" in first_val:
        data = {"Punjab": data}

    # Normalize the inner keys to uppercase-stripped for case-insensitive lookups.
    out = {}
    for state, bucket in data.items():
        if not isinstance(bucket, dict):
            continue
        out[state] = {k.upper().strip(): v for k, v in bucket.items()}
    _constituency_coords_cache = out
    return _constituency_coords_cache


def constituency_dots(db: Session, state_name: Optional[str] = None) -> list[dict]:
    """
    Per-constituency dot data for the Leaflet map on the homepage.

    Primary mode: one dict per constituency that has a current-cycle winner
    (`won=True`) and a known lat/lng centroid.

    Fallback mode (post-ECI-migration): the ECI source doesn't include the
    won/lost flag, so `scope="current"` returns []. When that happens, fall
    back to one dot per constituency that has ANY accepted candidate. Each
    dot represents the constituency itself, not a specific candidate —
    `mla` becomes "N candidates" and `party` / `wealth_cr` / `cases` are
    set to safe defaults until winner data is loaded.
    """
    coords = _load_constituency_coords()
    if not coords or not state_name:
        return []

    # Per-state bucket. Empty if this state isn't geocoded yet.
    state_bucket = coords.get(state_name, {})
    if not state_bucket:
        return []

    apps = _latest_appearances(db, house="Assembly", scope="current",
                                state_name=state_name)

    # --- Fallback: no winners flagged ---------------------------------------
    # Build one dot per constituency from the all-cycle candidate pool. This
    # is what unblocks Delhi 2025 where every row has `won=False` until
    # winner data is cross-referenced.
    fallback_mode = False
    if not apps:
        fallback_mode = True
        all_apps = _latest_appearances(db, house="Assembly", scope="all",
                                        state_name=state_name)
        # Group by constituency
        by_const: dict[str, list] = {}
        for a in all_apps:
            if not a.constituency:
                continue
            by_const.setdefault(a.constituency.name, []).append(a)
        apps = [as_list[0] for as_list in by_const.values()]  # one rep per
        const_counts = {k: len(v) for k, v in by_const.items()}
    else:
        const_counts = {}

    dots = []
    for a in apps:
        if not a.constituency:
            continue
        # Strip (SC) / (ST) reservation suffixes for matching — the centroid
        # file is keyed by the plain name without reservation tags.
        plain = a.constituency.name.upper().strip()
        for suffix in (" (SC)", " (ST)", "(SC)", "(ST)"):
            plain = plain.replace(suffix, "").strip()

        loc = state_bucket.get(plain)
        if not loc:
            continue

        if fallback_mode:
            n = const_counts.get(a.constituency.name, 1)
            dots.append({
                "constituency": a.constituency.name,
                # In fallback mode the dot represents the seat itself, not
                # any one candidate. Surface the total candidate count so
                # the hover/tooltip is still informative.
                "mla":          f"{n} candidates contested",
                "slug":         None,
                # Explicit "Pending winners" label so the home.html legend
                # doesn't fall back to 'IND' for nulls (causes every
                # constituency to look like an Independent stronghold).
                "party":        "Pending winners",
                "party_color":  "#9aa0a6",  # neutral grey
                "wealth_cr":    None,
                "cases":        None,
                "lat":          loc.get("lat"),
                "lng":          loc.get("lng"),
                "fallback":     True,
            })
            continue

        if not a.politician:
            continue

        dots.append({
            "constituency": a.constituency.name,
            "mla":          a.politician.display_name,
            "slug":         a.politician.slug or str(a.politician.id),
            "party":        a.party.short_name if a.party else "IND",
            "party_color":  party_color(a.party.short_name if a.party else None),
            "wealth_cr":    round((a.total_assets_inr or 0) / CRORE, 2),
            "cases":        a.criminal_cases_count or 0,
            "lat":          loc.get("lat"),
            "lng":          loc.get("lng"),
        })
    return dots


# ---------------- Did You Know -----------------------------------------------

def did_you_know(db: Session, state_name: Optional[str] = None) -> list[str]:
    """Auto-generated factoids computed live from the DB.

    POST-ECI-MIGRATION POLICY
    -------------------------
    The wealth/cases insights from the myneta era are NOT shown here. ECI
    affidavits are extracted by regex from OCR text, and the financial /
    criminal-case fields are not reliable enough to lead with. Until we
    have a verified-figures pipeline (LLM-assisted re-extraction + spot
    checks), `did_you_know` reports only fact patterns that are robust
    to NULL values — counts of candidates, parties, constituencies, and
    coverage notes. The leaderboards on the page are also de-emphasised
    until the underlying numbers are verified.
    """
    facts = []

    # Coverage counts — robust to NULL fields, drawn straight from the
    # canonical tables. These won't surprise anyone.
    apps = _latest_appearances(db, state_name=state_name, scope="all")
    if not apps:
        return ["No election data yet for this state."]

    cands = len(apps)
    parties = len({a.party.short_name for a in apps if a.party})
    consts = len({a.constituency.name for a in apps if a.constituency})
    facts.append(
        f"{cands:,} accepted candidates contested across {consts} constituencies "
        f"in the latest cycle for this state."
    )
    if parties:
        facts.append(
            f"{parties} distinct political parties (including Independents) "
            f"fielded candidates in this cycle."
        )

    # Data source / transparency note — this is the kind of fact we *can*
    # vouch for, since it's derived from the canonical row count rather
    # than any OCR-extracted number.
    facts.append(
        "All candidate data is sourced from ECI Form 26 affidavits "
        "(affidavit.eci.gov.in) under India's Government Open Data License."
    )

    # NOTE: Wealth/cases factoids INTENTIONALLY OMITTED until we have a
    # verified-figures pipeline. The myneta-era queries used to live
    # here — see git history for the previous implementation. Don't
    # re-enable them on the unverified ECI regex output.
    return facts


# ---------------- Time-machine: multi-cycle data -----------------------------

def dots_by_year(db: Session, house: str = "Assembly", state_name: Optional[str] = None) -> dict:
    """
    Per-cycle constituency winners, structured for the time-slider map.
    Returns {year: [{constituency, mla, party, color, wealth_cr, cases, slug}, ...]}.
    Used by the frontend to swap which dots are shown when the user drags the slider.
    """
    q = (
        db.query(ElectionAppearance)
        .join(Election, ElectionAppearance.election_id == Election.id)
        .filter(Election.house == house)
        .filter(ElectionAppearance.won.is_(True))
        .options(
            joinedload(ElectionAppearance.politician),
            joinedload(ElectionAppearance.party),
            joinedload(ElectionAppearance.constituency),
            joinedload(ElectionAppearance.election),
        )
    )
    if state_name:
        q = q.join(State, Election.state_id == State.id).filter(State.name == state_name)
    apps = q.all()
    out: dict[int, list[dict]] = {}
    for a in apps:
        if not (a.constituency and a.election):
            continue
        out.setdefault(a.election.year, []).append({
            "constituency": a.constituency.name,
            "mla":  a.politician.display_name,
            "slug": a.politician.slug or str(a.politician.id),
            "party": a.party.short_name if a.party else "IND",
            "color": party_color(a.party.short_name if a.party else None),
            "wealth_cr": round((a.total_assets_inr or 0) / CRORE, 2),
            "cases": a.criminal_cases_count or 0,
        })
    return dict(sorted(out.items()))


def party_wealth_by_cycle(db: Session, house: str = "Assembly", state_name: Optional[str] = None) -> dict:
    """
    Per-cycle average wealth per party (winning candidates only). Powers the
    term-by-term asset landscape chart on the homepage.
    Returns: {"years":[2007,2012,...], "parties":[{"party":"AAP","color":"#019cdf","data":[0.0, 0.0, 12.4, 8.7]}, ...]}
    """
    q = (
        db.query(ElectionAppearance)
        .join(Election, ElectionAppearance.election_id == Election.id)
        .filter(Election.house == house)
        .filter(ElectionAppearance.won.is_(True))
        .options(
            joinedload(ElectionAppearance.party),
            joinedload(ElectionAppearance.election),
        )
    )
    if state_name:
        q = q.join(State, Election.state_id == State.id).filter(State.name == state_name)
    apps = q.all()

    years = sorted({a.election.year for a in apps if a.election})
    by_party_year: dict[str, dict[int, list[int]]] = {}
    for a in apps:
        if not a.election or not a.party:
            continue
        p = a.party.short_name
        by_party_year.setdefault(p, {}).setdefault(a.election.year, []).append(a.total_assets_inr or 0)

    # Pick top 5 parties by total wealth across all cycles
    party_totals = {p: sum(sum(yr) for yr in years_data.values()) for p, years_data in by_party_year.items()}
    top_parties = sorted(party_totals, key=party_totals.get, reverse=True)[:5]

    return {
        "years": years,
        "parties": [
            {
                "party": p,
                "color": party_color(p),
                "data": [
                    round(sum(by_party_year[p].get(y, [0])) / max(len(by_party_year[p].get(y, [0])), 1) / CRORE, 2)
                    for y in years
                ],
            }
            for p in top_parties
        ],
    }


def party_seats_by_year(db: Session, house: str = "Assembly", state_name: Optional[str] = None) -> dict:
    """
    Party-vs-year seat counts. Returns {
        "years": [2007, 2012, 2017, 2022],
        "parties": [
            {"party": "AAP", "color": "#019cdf", "seats": [0, 0, 20, 92]},
            ...
        ]
    }
    Used for the race chart.
    """
    q = (
        db.query(ElectionAppearance)
        .join(Election, ElectionAppearance.election_id == Election.id)
        .filter(Election.house == house)
        .filter(ElectionAppearance.won.is_(True))
        .options(
            joinedload(ElectionAppearance.party),
            joinedload(ElectionAppearance.election),
        )
    )
    if state_name:
        q = q.join(State, Election.state_id == State.id).filter(State.name == state_name)
    apps = q.all()

    years = sorted({a.election.year for a in apps if a.election})
    counts: dict[str, dict[int, int]] = {}
    for a in apps:
        if not a.election:
            continue
        party = a.party.short_name if a.party else "IND"
        counts.setdefault(party, {y: 0 for y in years})
        counts[party][a.election.year] = counts[party].get(a.election.year, 0) + 1

    # Sort parties by their max seats across years
    sorted_parties = sorted(counts.items(), key=lambda x: max(x[1].values()), reverse=True)
    return {
        "years": years,
        "parties": [
            {
                "party": p,
                "color": party_color(p),
                "seats": [counts[p].get(y, 0) for y in years],
            }
            for p, _ in sorted_parties
        ],
    }


# ---------------- Citizen-focused KPIs ---------------------------------------

def hero_kpis(db: Session, house: str = "Assembly", scope: str = "all", state_name: Optional[str] = None) -> dict:
    """Four headline numbers for the hero strip. Anchored to house / scope / state.

    IMPORTANT — verified-only stats
    ===============================
    The wealth / case fields are computed ONLY from candidates whose
    `total_assets_inr` / `criminal_cases_count` are populated (i.e., who
    have been through the LLM-extraction pipeline). NULL values are NOT
    treated as 0 — they would inflate the apparent count-clean and
    deflate the apparent total wealth.

    `count` is still the total candidates on file (so the user sees both
    the universe size and the verified subset side-by-side via
    `count_verified_wealth` / `count_verified_cases`).
    """
    apps = _latest_appearances(db, house=house, scope=scope, state_name=state_name)
    if not apps:
        return {"count": 0, "total_wealth_cr": 0, "avg_wealth_cr": 0,
                "pct_with_cases": 0, "pct_crorepati": 0,
                "total_cases": 0, "avg_cases_per_mla": 0,
                "count_verified_wealth": 0,
                "count_verified_cases": 0,
                "house": house}

    # Split into verified vs unverified subsets so the stats are honest
    # about their denominator.
    wealth_verified = [a for a in apps if a.total_assets_inr is not None]
    cases_verified  = [a for a in apps if a.criminal_cases_count is not None]

    if wealth_verified:
        wealths = [a.total_assets_inr for a in wealth_verified]
        total_wealth_cr = round(sum(wealths) / CRORE, 0)
        avg_wealth_cr   = round(sum(wealths) / len(wealth_verified) / CRORE, 1)
        crorepati       = sum(1 for w in wealths if w >= CRORE)
        pct_crorepati   = round(100 * crorepati / len(wealth_verified), 0)
    else:
        total_wealth_cr = 0
        avg_wealth_cr   = 0
        pct_crorepati   = 0

    if cases_verified:
        with_cases   = sum(1 for a in cases_verified if a.criminal_cases_count > 0)
        total_cases  = sum(a.criminal_cases_count for a in cases_verified)
        pct_with_cases    = round(100 * with_cases / len(cases_verified), 0)
        avg_cases_per_mla = round(total_cases / len(cases_verified), 1)
    else:
        total_cases       = 0
        pct_with_cases    = 0
        avg_cases_per_mla = 0

    return {
        "house": house,
        "count": len(apps),
        "total_wealth_cr":       total_wealth_cr,
        "avg_wealth_cr":         avg_wealth_cr,
        "pct_with_cases":        pct_with_cases,
        "pct_crorepati":         pct_crorepati,
        "total_cases":           total_cases,
        "avg_cases_per_mla":     avg_cases_per_mla,
        # NEW: how many of `count` actually contributed to wealth/case stats.
        # Templates use these to show "N of M verified" caveats.
        "count_verified_wealth": len(wealth_verified),
        "count_verified_cases":  len(cases_verified),
    }


def _politicians_in_state(db: Session, state_name: Optional[str] = None):
    """Helper — base politician query scoped to a state by election history."""
    q = (
        db.query(Politician)
        .options(
            joinedload(Politician.appearances).joinedload(ElectionAppearance.election),
            joinedload(Politician.appearances).joinedload(ElectionAppearance.party),
            joinedload(Politician.appearances).joinedload(ElectionAppearance.constituency),
        )
    )
    if state_name:
        # Only include politicians who have at least one appearance in this state
        state_politician_ids = (
            db.query(ElectionAppearance.politician_id)
            .join(Election, ElectionAppearance.election_id == Election.id)
            .join(State, Election.state_id == State.id)
            .filter(State.name == state_name)
            .distinct()
        )
        q = q.filter(Politician.id.in_(state_politician_ids))
    return q


def _filter_state_appearances(appearances, state_name: Optional[str]):
    """Filter a politician's appearances to those in a specific state."""
    if not state_name:
        return appearances
    return [a for a in appearances
            if a.election and a.election.state
            and a.election.state.name == state_name]


def wealth_multipliers(db: Session, limit: int = 10, house: str = "Assembly", state_name: Optional[str] = None) -> list[dict]:
    """
    Politicians with the biggest *percentage* wealth growth between any
    two of their appearances. Much more compelling than absolute rupee gains.
    """
    politicians = _politicians_in_state(db, state_name).all()
    rows = []
    for p in politicians:
        # Filter to appearances in this state only when state filter is on
        state_apps = _filter_state_appearances(p.appearances, state_name)
        valid = [a for a in state_apps
                 if a.election and a.election.house == house
                 and a.total_assets_inr is not None]
        if len(valid) < 2:
            continue
        valid.sort(key=lambda a: a.election.year)
        first, last = valid[0], valid[-1]
        if not first.total_assets_inr or first.total_assets_inr < 100_000:
            continue
        pct = ((last.total_assets_inr or 0) - first.total_assets_inr) / first.total_assets_inr * 100
        if pct <= 0:
            continue
        rows.append({
            "politician": p,
            "latest": last,
            "first_year": first.election.year,
            "last_year": last.election.year,
            "from_cr": round(first.total_assets_inr / CRORE, 2),
            "to_cr":   round((last.total_assets_inr or 0) / CRORE, 2),
            "pct":     round(pct, 0),
        })
    rows.sort(key=lambda r: r["pct"], reverse=True)
    return rows[:limit]


def crorepati_newcomers(db: Session, limit: int = 10, house: str = "Assembly", state_name: Optional[str] = None) -> list[dict]:
    """
    Politicians who were sub-crore in their first declared affidavit and
    are crorepati now — the "not rich before politics" story.
    """
    politicians = _politicians_in_state(db, state_name).all()
    rows = []
    for p in politicians:
        state_apps = _filter_state_appearances(p.appearances, state_name)
        valid = [a for a in state_apps
                 if a.election and a.election.house == house
                 and a.total_assets_inr is not None]
        if len(valid) < 2:
            continue
        valid.sort(key=lambda a: a.election.year)
        first, last = valid[0], valid[-1]
        if (first.total_assets_inr or 0) >= CRORE:
            continue   # was already crorepati at start
        if (last.total_assets_inr or 0) < CRORE:
            continue   # still not crorepati
        rows.append({
            "politician": p,
            "latest": last,
            "first_year": first.election.year,
            "last_year": last.election.year,
            "from_cr": round((first.total_assets_inr or 0) / CRORE, 2),
            "to_cr":   round((last.total_assets_inr or 0) / CRORE, 2),
            "pct":     round(
                ((last.total_assets_inr or 0) - (first.total_assets_inr or 0))
                / max(first.total_assets_inr, 1) * 100, 0
            ),
        })
    rows.sort(key=lambda r: r["to_cr"], reverse=True)
    return rows[:limit]


def anomaly_candidates(db: Session, limit: int = 50, house: str = "Assembly",
                       state_name: Optional[str] = None,
                       scope: str = "current") -> list[dict]:
    """
    Surface "stand-out" politicians within a single election cycle.

    `scope` controls the population we search:
        "current" — only currently-sitting MLAs (winners of the latest cycle).
        "all"     — every appearance ever scraped in this state (winners + losers,
                    every cycle). Useful for historical analyses.

    History: this used to compare wealth across cycles via wealth_multipliers().
    After the candidate_id-collision cleanup (scripts/split_merged_politicians.py)
    every politician has exactly one ElectionAppearance, so cross-cycle comparison
    isn't possible until we add a name-based re-linking pass.

    Until then, the score is a single-cycle outlier index:
        - heavy penalty for pending criminal cases (×8 per case)
        - moderate penalty for wealth far above the state's median
            (uses log of (wealth / state_median) so a 100x outlier ≈ -33 pts)

    Score is clamped to [0, 100]; 0 = critical risk, 100 = squeaky-clean.
    """
    import math
    apps = _latest_appearances(db, house=house, scope=scope, state_name=state_name)
    if not apps:
        return []

    wealths = sorted(a.total_assets_inr or 0 for a in apps)
    n = len(wealths)
    state_median = wealths[n // 2] or 1   # avoid div-by-zero on degenerate data

    out = []
    for a in apps:
        if not a.politician:
            continue
        cases = a.criminal_cases_count or 0
        wealth = a.total_assets_inr or 0
        wealth_ratio = max(1.0, wealth / state_median)
        wealth_pen = math.log10(wealth_ratio + 1) * 15
        score = max(0, min(100, 100 - (cases * 8) - wealth_pen))

        # Skip uninteresting middle-of-the-road rows
        if cases == 0 and wealth_ratio < 3:
            continue

        out.append({
            "politician": a.politician,
            "latest":     a,
            "score":      score,
            "cases":      cases,
            "wealth_cr":  round(wealth / CRORE, 2),
            "wealth_x_median": round(wealth_ratio, 1),
        })

    out.sort(key=lambda r: r["score"])   # lowest score first = highest risk
    return out[:limit]


def anomaly_buckets(db: Session, house: str = "Assembly",
                    state_name: Optional[str] = None,
                    scope: str = "current") -> dict:
    """Return counts of candidates in each risk bucket (critical/suspicious/standard).
    `scope` matches anomaly_candidates: "current" = sitting MLAs only, "all" = every
    appearance scraped for this state across all cycles."""
    candidates = anomaly_candidates(db, limit=500, house=house, state_name=state_name, scope=scope)
    return {
        "critical":    sum(1 for r in candidates if r["score"] < 30),
        "suspicious":  sum(1 for r in candidates if 30 <= r["score"] < 60),
        "standard":    sum(1 for r in candidates if r["score"] >= 60),
    }


def party_switchers(db: Session, limit: int = 20, state_name: Optional[str] = None) -> list[dict]:
    """
    Find politicians who appeared with more than one party across their
    election appearances — the "Aaya Ram Gaya Ram" phenomenon.

    Returns rows with the politician's full party journey, sorted by
    number of switches (most-switches first).
    """
    politicians = _politicians_in_state(db, state_name).all()
    rows = []
    for p in politicians:
        # Build a chronological list of (year, party_name) skipping rows without party,
        # filtered to the state we care about when state_name is set.
        state_apps = _filter_state_appearances(p.appearances, state_name)
        history = []
        for a in sorted(state_apps, key=lambda a: a.election.year if a.election else 0):
            if a.party and a.election:
                history.append({
                    "year": a.election.year,
                    "house": a.election.house,
                    "party": a.party.short_name,
                    "color": party_color(a.party.short_name),
                    "constituency": a.constituency.name if a.constituency else "",
                    "won": a.won,
                })
        unique_parties = list({h["party"] for h in history})
        if len(unique_parties) < 2:
            continue
        # Count switches (consecutive different parties)
        switches = sum(1 for i in range(1, len(history)) if history[i]["party"] != history[i-1]["party"])
        rows.append({
            "politician": p,
            "history": history,
            "unique_parties": unique_parties,
            "switches": switches,
            "from_party": history[0]["party"],
            "to_party": history[-1]["party"],
            "first_year": history[0]["year"],
            "last_year": history[-1]["year"],
        })
    rows.sort(key=lambda r: (r["switches"], r["last_year"]), reverse=True)
    return rows[:limit]


def long_servers(db: Session, limit: int = 10, house: str = "Assembly", state_name: Optional[str] = None) -> list[dict]:
    """Politicians who've won in the most election cycles in this house."""
    politicians = _politicians_in_state(db, state_name).all()
    rows = []
    for p in politicians:
        state_apps = _filter_state_appearances(p.appearances, state_name)
        wins = [a for a in state_apps
                if a.election and a.election.house == house and a.won]
        if len(wins) < 2:
            continue
        wins.sort(key=lambda a: a.election.year)
        latest = wins[-1]
        rows.append({
            "politician": p,
            "latest": latest,
            "wins": len(wins),
            "first_year": wins[0].election.year,
            "last_year": latest.election.year,
        })
    rows.sort(key=lambda r: (r["wins"], r["last_year"]), reverse=True)
    return rows[:limit]


def clean_and_wealthy(db: Session, limit: int = 10, house: str = "Assembly",
                       min_wealth_cr: float = 5.0, scope: str = "all",
                       state_name: Optional[str] = None) -> list[dict]:
    """Politicians with wealth >= threshold AND zero pending criminal cases."""
    apps = _latest_appearances(db, house=house, scope=scope, state_name=state_name)
    rows = []
    for a in apps:
        if (a.total_assets_inr or 0) < min_wealth_cr * CRORE:
            continue
        if (a.criminal_cases_count or 0) > 0:
            continue
        rows.append({
            "politician": a.politician,
            "appearance": a,
            "wealth_cr": round((a.total_assets_inr or 0) / CRORE, 2),
        })
    rows.sort(key=lambda r: r["wealth_cr"], reverse=True)
    return rows[:limit]


# ---------------- Random discovery -------------------------------------------

def random_politician(db: Session) -> Optional[Politician]:
    ids = [pid for (pid,) in db.query(Politician.id).all()]
    if not ids:
        return None
    return db.query(Politician).filter(Politician.id == random.choice(ids)).first()


# ---------------- Per-constituency drill-down ---------------------------------

def constituency_top_candidates(
    db: Session,
    state_name: str,
    constituency_name: str,
    limit: int = 3,
) -> dict:
    """Per-constituency cross-cycle data for the click-through page.

    Returns:
        {
            "state": "Delhi",
            "constituency": "NEW DELHI",
            "cycles": [
                {
                    "year": 2025,
                    "house": "Assembly",
                    "total_candidates_on_file": int,
                    "candidates": [
                        {
                            "rank": 1,
                            "won": True,
                            "politician": Politician,
                            "appearance": ElectionAppearance,
                            "party_short": "BJP",
                            "party_color": "#fd761a",
                            "votes": 30088,
                            "vote_share_pct": 45.65,
                        },
                        ...top N...
                    ],
                },
                # ... previous cycles ...
            ],
        }

    Candidates within each cycle are sorted by votes_received DESC
    (NULLs sink to bottom). For cycles where we don't have vote counts,
    we still return the available candidates but mark them as
    `votes_unknown`. The cycle dict is None-free; everything is in the
    most renderable shape for the template.
    """
    # Normalize the constituency name once — we match by `name = ?`
    # against the canonical (post-migration) name stored in `constituencies`.
    cons = (
        db.query(Constituency)
        .join(State, Constituency.state_id == State.id)
        .filter(State.name == state_name)
        .filter(Constituency.name == constituency_name)
        .first()
    )
    if not cons:
        return None  # caller handles 404

    cycles_out = []
    elections = (
        db.query(Election)
        .filter(Election.state_id == cons.state_id)
        .filter(Election.house == cons.house)
        .order_by(Election.year.desc())
        .all()
    )
    for e in elections:
        # All accepted candidates in this (constituency, election)
        apps = (
            db.query(ElectionAppearance)
            .filter(ElectionAppearance.constituency_id == cons.id)
            .filter(ElectionAppearance.election_id == e.id)
            .options(
                joinedload(ElectionAppearance.politician),
                joinedload(ElectionAppearance.party),
            )
            .all()
        )
        if not apps:
            continue

        # Sort: votes_received DESC (NULL last), then won=True first, then name
        def _sort_key(a):
            v = a.votes_received
            return (
                0 if v is not None else 1,  # known votes first
                -(v or 0),                    # higher votes earlier
                0 if a.won else 1,            # winner edge case
                (a.politician.name if a.politician else ""),
            )
        apps_sorted = sorted(apps, key=_sort_key)

        candidates_out = []
        for rank, app in enumerate(apps_sorted[:limit], 1):
            party_short = app.party.short_name if app.party else "IND"
            candidates_out.append({
                "rank":           rank,
                "won":            bool(app.won),
                "politician":     app.politician,
                "appearance":     app,
                "party_short":    party_short,
                "party_color":    party_color(party_short),
                "votes":          app.votes_received,
                "vote_share_pct": app.vote_share_pct,
                "votes_unknown":  app.votes_received is None,
            })

        cycles_out.append({
            "year":                     e.year,
            "house":                    e.house,
            "total_candidates_on_file": len(apps),
            "candidates":               candidates_out,
        })

    return {
        "state":         state_name,
        "constituency":  constituency_name,
        "house":         cons.house,
        "constituency_id": cons.id,
        "cycles":        cycles_out,
    }
