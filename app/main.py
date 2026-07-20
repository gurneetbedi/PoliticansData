"""
FastAPI application — PoliTrack backend.

Endpoints:
  GET  /                          homepage (HTML, Jinja2)
  GET  /browse                    browseable list with filters
  GET  /politician/{slug}         detailed profile
  GET  /compare?slugs=a,b,c       side-by-side comparison
  GET  /api/politicians           JSON API for lists
  GET  /api/politicians/{slug}    JSON profile
  GET  /api/stats                 aggregate counts
"""
from pathlib import Path

from fastapi import FastAPI, Depends, Request, HTTPException, Query
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, or_
from sqlalchemy.orm import Session, joinedload

from app.database import Base, engine, get_db
from app.models import (
    Politician, ElectionAppearance, Election, Constituency, Party, State
)
from app import services
from app.data.punjab_rs import PUNJAB_RS_MEMBERS
from app.states import ALL_STATES as _ALL_STATES, visible_states as _visible_states

# The /eci/* router is parked. After the ECI data was migrated into the
# canonical politicians/election_appearances tables, the entire site became
# ECI-sourced, so a separate "Affidavits" section was redundant. Keep the
# file (app/eci_routes.py) and templates (app/templates/eci/) in source so
# we can lift logic from them later if needed.
# from app.eci_routes import router as eci_router

Base.metadata.create_all(bind=engine)

# Absolute paths resolved from this file so static/templates work regardless
# of the cwd gunicorn happens to start in (Render/Heroku/etc. may differ).
APP_DIR = Path(__file__).resolve().parent
STATIC_DIR = APP_DIR / "static"
TEMPLATES_DIR = APP_DIR / "templates"

app = FastAPI(title="PoliTrack India", version="0.1.0")
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))


# Chief Ministers + Lok Sabha 2024 data — loaded once at boot, small
# static JSONs shipped in app/static/. Powers the right-hand "Currently
# viewing" panel on the homepage. Hot-reload uvicorn will re-import
# this file if the JSON changes; production restarts on git push anyway.
def _load_static_json(name: str) -> dict:
    try:
        import json
        return json.loads((STATIC_DIR / name).read_text(encoding="utf-8"))
    except Exception:
        return {}

CHIEF_MINISTERS  = _load_static_json("chief_ministers.json")
LOK_SABHA_2024   = _load_static_json("lok_sabha_2024.json")


def _party_seats_for_state(db, state_name: str, top_n: int = 6) -> dict:
    """Return top N parties by seat count in the state's latest assembly
    cycle. Structure: {"year": 2025, "rows": [{"party":"BJP","seats":48},…]}.

    Used by the homepage right-hand sidebar to render the party-bar
    widget when the user hovers a state.
    """
    from app.models import State, Election, ElectionAppearance, Party
    from sqlalchemy import func
    # Latest cycle year for this state (Assembly)
    latest_row = (
        db.query(func.max(Election.year))
        .join(State, State.id == Election.state_id)
        .filter(State.name == state_name, Election.house == "Assembly")
        .scalar()
    )
    if not latest_row:
        return {"year": None, "rows": []}
    # Aggregate winning seats by party for that year
    rows = (
        db.query(Party.short_name, func.count(ElectionAppearance.id).label("s"))
        .join(ElectionAppearance, ElectionAppearance.party_id == Party.id)
        .join(Election, Election.id == ElectionAppearance.election_id)
        .join(State, State.id == Election.state_id)
        .filter(
            State.name == state_name,
            Election.year == latest_row,
            Election.house == "Assembly",
            ElectionAppearance.won == True,  # noqa: E712
        )
        .group_by(Party.short_name)
        .order_by(func.count(ElectionAppearance.id).desc())
        .limit(top_n)
        .all()
    )
    return {
        "year": latest_row,
        "rows": [{"party": p or "IND", "seats": s} for p, s in rows],
    }

# (Removed: app.include_router(eci_router))
# After the migration, the canonical routes are the ECI routes; no separate
# /eci/* prefix is needed.

# Make the tracked-states list available to every template (used by the
# State Selector dropdown in base.html and any other UI that needs to
# enumerate states). This way adding a new state in app/states.py
# automatically shows up everywhere without per-template edits.
templates.env.globals["TRACKED_STATES"] = sorted(
    ({"name": _visible_states()[k].name, "key": k} for k in _visible_states()),
    key=lambda entry: entry["name"].lower(),
)


# ---------------- Jinja filters for case-description cleanup ----------------

import re as _re

def reject_empty_cases(cases):
    """
    Filter out 'No Cases' placeholder rows that myneta affidavits sometimes
    contain. A case is real if it has either IPC sections OR a description
    that contains actual legal content (not just dashes/whitespace/'no case').
    """
    if not cases:
        return []
    out = []
    for c in cases:
        desc = (getattr(c, "description", "") or "").strip()
        ipc  = (getattr(c, "ipc_sections", "") or "").strip()
        # Treat description as empty if it's just dashes/whitespace/'no case' variants
        compact = _re.sub(r"[\s\-—–]+", "", desc).lower()
        is_placeholder = (
            compact == "" or
            "nocase" in compact or
            "nocases" in compact or
            compact in {"nil", "na", "none"}
        )
        has_real_ipc = bool(ipc) and "no" not in ipc.lower()[:8]
        if not is_placeholder or has_real_ipc:
            out.append(c)
    return out


def clean_case_desc(desc):
    """
    Strip FIR numbers, police-station references, and district fragments
    from a case description so the UI shows only the legally meaningful text.
    Examples removed:
      "FIR No 0101/2016"        "FIR 12/2018"        "0101/2016,"
      "Police Station Subhanpur" "P.S. Khanna"        "Distt. Kapurthala"
    """
    if not desc:
        return ""
    s = desc
    # Remove placeholder text
    s = _re.sub(r"-+\s*no\s*cases?\s*-+", "", s, flags=_re.I)
    # FIR numbers in many forms
    s = _re.sub(r"\bFIR\s*(?:No\.?|Number)?\s*[\d/\-]+", "", s, flags=_re.I)
    # Bare case numbers like "0101/2016" or "12/18"
    s = _re.sub(r"\b\d{2,5}/\d{2,4}\b", "", s)
    # "Police Station X" / "P.S. X" up to the next comma or period
    s = _re.sub(r"\bPolice\s*Station[^,.;]*", "", s, flags=_re.I)
    s = _re.sub(r"\bP\.?\s*S\.?\s+[A-Za-z]+(?:\s+[A-Za-z]+)?", "", s, flags=_re.I)
    # "Distt." / "District X" up to comma/period
    s = _re.sub(r"\bDis(?:t(?:t|rict))?\.?\s+[A-Za-z]+(?:\s+[A-Za-z]+)?", "", s, flags=_re.I)
    # Iterative cleanup — leftover punctuation can chain ("Attempt to murder, , ,")
    # so we loop until the string stops shrinking.
    prev = None
    while s != prev:
        prev = s
        s = _re.sub(r"\s*[,;]\s*[,;]\s*", ", ", s)   # collapse duplicate commas
        s = _re.sub(r"^\s*[,;:\-]+\s*", "", s)        # leading punctuation
        s = _re.sub(r"\s*[,;:\-]+\s*$", "", s)        # trailing punctuation
        s = _re.sub(r"\s{2,}", " ", s).strip()
    return s


templates.env.filters["reject_empty_cases"] = reject_empty_cases
templates.env.filters["clean_case_desc"]    = clean_case_desc

from app.case_types import case_type_for_ipc, all_case_types
templates.env.filters["case_type"]      = case_type_for_ipc
templates.env.filters["all_case_types"] = all_case_types


# ---- Permissive state parser ------------------------------------------------
# FastAPI's strict regex validator returns a 422 when ?state= is empty or unknown,
# which breaks navigation when a link accidentally drops the value (e.g. browser
# back, copy-paste, manually typed URL). Replace with a coercing dependency.

# Derived from the registry so adding a new state is a single edit in states.py.
KNOWN_STATES = set(_ALL_STATES.keys())
# Display order for dropdowns / leaderboards / map summaries. Delhi only —
# all canonical tables now come from ECI (Delhi 2025 Assembly) after the
# migration. When more states get ECI coverage, list them here in the
# preferred display order.
TRACKED_STATE_NAMES = [s.name for s in _visible_states().values()]

def resolve_state(state: str | None = None) -> str:
    """Return a canonical state name matching ``states.name`` in the DB.

    Accepts either the state key (``"arunachal"``) or the full name
    (``"Arunachal Pradesh"``, ``"arunachal pradesh"``), case-insensitively.
    Multi-word states (Uttar Pradesh, Jammu and Kashmir, etc.) are
    handled correctly via ALL_STATES lookup — the previous
    ``.capitalize()`` fallback broke them by lowercasing every word after
    the first.
    """
    if not state:
        return "Delhi"

    s = state.strip()
    if not s:
        return "Delhi"

    # 1. Exact key match (e.g. ?state=arunachal)
    key = s.lower()
    if key in _ALL_STATES:
        return _ALL_STATES[key].name

    # 2. Full-name match, case-insensitive (e.g. ?state=Arunachal%20Pradesh)
    for cfg in _ALL_STATES.values():
        if cfg.name.lower() == key:
            return cfg.name

    # 3. Ampersand ↔ "and" normalization. `Jammu & Kashmir` is a common
    # display form users type or arrives via mangled dropdown links; the
    # canonical DB name is `Jammu and Kashmir`. Try both directions.
    key_amp_to_and = key.replace(" & ", " and ").replace("&", "and")
    key_and_to_amp = key.replace(" and ", " & ")
    for cfg in _ALL_STATES.values():
        cn = cfg.name.lower()
        if cn == key_amp_to_and or cn == key_and_to_amp:
            return cfg.name

    # 4. Loose substring match — last-resort. Handles cases where the
    # user types the abbreviation ("J&K") or a truncated form.
    for cfg in _ALL_STATES.values():
        cn = cfg.name.lower()
        if key in cn or cn in key:
            return cfg.name

    return "Delhi"


def resolve_year(year: str | None = None) -> int | None:
    """
    Permissive year parser. FastAPI's `int | None` would 422 on an empty string
    submitted by a <select> with a blank "All years" option. Treat empty / non-numeric
    values as None instead of crashing.
    """
    if year is None:
        return None
    s = str(year).strip()
    if not s:
        return None
    try:
        return int(s)
    except (TypeError, ValueError):
        return None


def latest_appearance(politician: Politician) -> ElectionAppearance | None:
    """The most recent ElectionAppearance for a politician, by election year."""
    if not politician.appearances:
        return None
    return max(politician.appearances, key=lambda a: a.election.year if a.election else 0)


# ----- HTML routes -------------------------------------------------------------

@app.get("/", response_class=HTMLResponse)
def home(
    request: Request,
    db: Session = Depends(get_db),
    view: str = Query("mla", regex="^(mla|mp|rs)$"),
    # Default to "current" (winners only) now that we have winner flags
    # loaded for Delhi 2020 + 2025. Users can toggle to "all" via the
    # scope-switch UI to see every contesting candidate. Was "all"
    # during the no-winner-data era — see git history for the reasoning.
    scope: str = Query("current", regex="^(current|all)$"),
    state: str = Depends(resolve_state),
):
    """
    The view toggle selects which legislative body to focus the page on:
      mla  → Delhi 2025 Assembly candidates (default; 70 seats; ECI-sourced)
      mp   → Lok Sabha MPs (not yet populated for Delhi from ECI)
      rs   → Rajya Sabha (curated; not yet populated for Delhi)

    The scope toggle:
      current → only politicians who won in the latest cycle (empty for now —
                ECI source doesn't include the won/lost flag; awaiting cross-ref
                with EC result data before re-enabling)
      all     → every accepted candidate in the latest cycle, with their
                declared ECI data (DEFAULT)
    """
    HOUSE = {"mla": "Assembly", "mp": "LokSabha", "rs": "RajyaSabha"}[view]

    cycles = db.query(Election).order_by(Election.year.desc()).all()
    unique_constituencies = (
        db.query(func.count(func.distinct(Constituency.id)))
        .join(ElectionAppearance, ElectionAppearance.constituency_id == Constituency.id)
        .scalar()
    )

    # Hero KPIs and leaderboards are always computed for MLA (the richest dataset);
    # the page badges them clearly. When the user toggles to MP, the leaderboards
    # show MP data. RS is a curated list shown as cards (no leaderboards).
    house_for_kpis = HOUSE if HOUSE in ("Assembly", "LokSabha") else "Assembly"
    kpis = services.hero_kpis(db, house=house_for_kpis, scope=scope, state_name=state)

    # Counts for the toggle badge labels — so users see "(117)" vs "(280)"
    current_count = services.hero_kpis(db, house=house_for_kpis, scope="current", state_name=state)["count"]
    all_count     = services.hero_kpis(db, house=house_for_kpis, scope="all", state_name=state)["count"]

    return templates.TemplateResponse("home.html", {
        "request": request,
        "view": view,
        "scope": scope,
        "state": state,
        "house": HOUSE,
        "cycles": cycles,
        "unique_constituencies": unique_constituencies,

        # Hero KPIs (4 anchor numbers)
        "kpis": kpis,
        "current_count": current_count,
        "all_count": all_count,

        # Every section is now state-scoped — Bihar page shows only Bihar data, etc.
        "top_wealth":          services.top_by_wealth(db, 10, house=house_for_kpis, scope=scope, state_name=state),
        "top_cases":           services.top_by_cases(db, 10, house=house_for_kpis, scope=scope, state_name=state),
        "wealth_multipliers":  services.wealth_multipliers(db, 10, house=house_for_kpis, state_name=state),
        "crorepati_newcomers": services.crorepati_newcomers(db, 10, house=house_for_kpis, state_name=state),
        "long_servers":        services.long_servers(db, 10, house=house_for_kpis, state_name=state),
        "clean_wealthy":       services.clean_and_wealthy(db, 10, house=house_for_kpis, scope=scope, state_name=state),
        "switchers":           services.party_switchers(db, 10, state_name=state),

        # Visualizations — also state-scoped
        "trends":   services.trends_by_cycle(db, state_name=state),
        "parties":  services.party_stats(db, state_name=state),
        "scatter":  services.scatter_points(db, state_name=state),
        "tiles":    services.constituency_tiles(db, state_name=state),
        "dots_by_year":     services.dots_by_year(db, house="Assembly", state_name=state),
        "party_seats":      services.party_seats_by_year(db, house="Assembly", state_name=state),
        "party_wealth_cycles": services.party_wealth_by_cycle(db, house="Assembly", state_name=state),
        "party_coverage":      services.party_coverage_snapshot(db, house="Assembly", state_name=state),
        "facts":    services.did_you_know(db, state_name=state),

        # India-wide stats for the choropleth (one row per tracked state).
        # Respects the current view (Assembly vs LokSabha) so switching
        # State ↔ Central Election actually recolors the map. When no LS
        # data exists, every state's KPI comes back empty and the map
        # renders as all-grey — the template shows a "no data" banner
        # in that case.
        "india_states": [
            {
                "name": s_name,
                "kpi": services.hero_kpis(db, house=house_for_kpis, scope="current", state_name=s_name),
            }
            for s_name in TRACKED_STATE_NAMES
        ],

        # Static reference data for the right-hand "Currently viewing"
        # panel: Chief Ministers by state (State Election mode) and
        # Prime Minister + LS 2024 seat share (Central Election mode).
        # See app/static/chief_ministers.json + lok_sabha_2024.json.
        "chief_ministers":  CHIEF_MINISTERS,
        "lok_sabha_2024":   LOK_SABHA_2024,

        # Per-state top-party-seat map for the sidebar's party-bar
        # widget. Structure:
        #   { "Delhi": {"year": 2025,
        #               "rows": [{"party":"BJP","seats":48}, ...]}, ... }
        # Only latest cycle per state; top 6 parties by seats.
        "party_seats": {
            s_name: _party_seats_for_state(db, s_name)
            for s_name in TRACKED_STATE_NAMES
        },

        # Per-state coverage status for the Data Coverage banner. Computed
        # fresh on each load so the banner always reflects what's actually
        # in the DB.
        "coverage": services.coverage_summary(db),

        # Zonal aggregations for the Constituency Deep-Dive panel —
        # one card per geographic zone (North / South / East / West /
        # Northeast / Central) with rolled-up MLA count + Transparency %.
        "zones": services.zone_summary(db),

        # Per-constituency dot map data for the Deep-Dive right column.
        # Empty for states that aren't geocoded yet — template falls back
        # to the tile grid in that case.
        "constituency_dots": services.constituency_dots(db, state_name=state),

        # Helpers
        "party_color": services.party_color,
        "latest":      latest_appearance,
        "rs_members":  PUNJAB_RS_MEMBERS,
    })


@app.get("/browse", response_class=HTMLResponse)
def browse(
    request: Request,
    db: Session = Depends(get_db),
    party: str | None = None,
    year: int | None = Depends(resolve_year),
    q: str | None = None,
    house: str = Query("all", regex="^(all|state|central)$"),
    sort: str = Query("name", regex="^(name|wealth|terms|cases)$"),
    order: str = Query("desc", regex="^(asc|desc)$"),
    state: str = Depends(resolve_state),
):
    # Decide which legislatures the user wants. "state" = Assembly (MLAs).
    # "central" = LokSabha + RajyaSabha (MPs). "all" leaves it open.
    HOUSE_FILTERS = {
        "state":   ["Assembly"],
        "central": ["LokSabha", "RajyaSabha"],
        "all":     ["Assembly", "LokSabha", "RajyaSabha"],
    }
    allowed_houses = HOUSE_FILTERS[house]

    # Scope the politician query to ones with at least one appearance in this
    # state AND the requested legislative body.
    state_politician_ids = (
        db.query(ElectionAppearance.politician_id)
        .join(Election, ElectionAppearance.election_id == Election.id)
        .join(State, Election.state_id == State.id)
        .filter(State.name == state)
        .filter(Election.house.in_(allowed_houses))
        .distinct()
    )

    query = (
        db.query(Politician)
        .filter(Politician.id.in_(state_politician_ids))
        .options(
            joinedload(Politician.appearances).joinedload(ElectionAppearance.election),
            joinedload(Politician.appearances).joinedload(ElectionAppearance.party),
            joinedload(Politician.appearances).joinedload(ElectionAppearance.constituency),
        )
    )
    if q:
        query = query.filter(Politician.name.ilike(f"%{q}%"))

    politicians = query.order_by(Politician.name).limit(1500).all()

    # Filter by party / year (in Python because the relationship traversal is cheap once loaded)
    if party:
        politicians = [
            p for p in politicians
            if any(a.party and a.party.short_name == party for a in p.appearances)
        ]
    if year:
        politicians = [
            p for p in politicians
            if any(a.election and a.election.year == year for a in p.appearances)
        ]

    # Defensive sort — every key returns an int/str, never None.
    def safe_name(p):
        return (p.display_name or "").lower()
    def latest_wealth(p):
        a = latest_appearance(p)
        return int(a.total_assets_inr or 0) if a else 0
    def latest_cases(p):
        a = latest_appearance(p)
        return int(a.criminal_cases_count or 0) if a else 0
    def term_count(p):
        return int(sum(1 for a in (p.appearances or []) if a.won))

    sort_keys = {
        "name":   safe_name,
        "wealth": latest_wealth,
        "terms":  term_count,
        "cases":  latest_cases,
    }
    reverse = (order == "desc")
    try:
        politicians.sort(key=sort_keys[sort], reverse=reverse)
    except Exception:
        # Fall back to name sort if anything goes wrong (e.g. mixed type comparison)
        politicians.sort(key=safe_name)

    parties = db.query(Party).order_by(Party.short_name).all()

    # Year dropdown: only election years for the selected state, descending
    years = [
        y for (y,) in (
            db.query(Election.year)
            .join(State, Election.state_id == State.id)
            .filter(State.name == state)
            .distinct()
            .order_by(Election.year.desc())
            .all()
        )
    ]

    return templates.TemplateResponse("browse.html", {
        "request": request, "politicians": politicians, "latest": latest_appearance,
        "parties": parties, "years": years,
        "selected_party": party, "selected_year": year, "q": q,
        "selected_house": house,
        "sort": sort, "order": order, "state": state,
    })


@app.get("/politician")
@app.get("/politician/")
def politician_empty():
    """Catch /politician with no slug (likely an empty-slug link in the DB)."""
    return RedirectResponse(url="/browse", status_code=302)


@app.get("/heatmap", response_class=HTMLResponse)
def heatmap(
    request: Request,
    db: Session = Depends(get_db),
    state: str = Depends(resolve_state),
):
    """State-wise Transparency Heatmap — India choropleth + selected-state constituency grid."""
    # Build a {state_name: zone_label} lookup so the sidebar can group by zone.
    state_zones = {cfg.name: cfg.zone for cfg in _visible_states().values() if cfg.zone}
    return templates.TemplateResponse("heatmap.html", {
        "request": request,
        "state":   state,
        "tiles":   services.constituency_tiles(db, state_name=state),
        "kpis":    services.hero_kpis(db, house="Assembly", scope="current", state_name=state),
        "state_zones": state_zones,
        "india_states": [
            {"name": s_name, "kpi": services.hero_kpis(db, house="Assembly", scope="current", state_name=s_name)}
            for s_name in TRACKED_STATE_NAMES
        ],
    })


@app.get("/anomalies", response_class=HTMLResponse)
def anomalies(
    request: Request,
    db: Session = Depends(get_db),
    state: str = Depends(resolve_state),
    scope: str = Query("current", regex="^(current|all)$"),
):
    """Data Pattern Analysis — flag candidates as outliers.

    scope=current → only currently-sitting MLAs (latest cycle winners only).
                    This is the default — what most users want to see.
    scope=all     → every candidate ever scraped in this state, including
                    losers and previous-cycle MLAs.
    """
    return templates.TemplateResponse("anomalies.html", {
        "request": request,
        "anomalies": services.anomaly_candidates(db, limit=50, state_name=state, scope=scope),
        "buckets":   services.anomaly_buckets(db, state_name=state, scope=scope),
        "state":     state,
        "scope":     scope,
    })


@app.get("/funding", response_class=HTMLResponse)
def funding(request: Request):
    """
    Political Funding Flows dashboard.
    Funding data is national-level (electoral bonds aggregated by party, not by state),
    so this page intentionally does NOT take a state parameter.
    """
    return templates.TemplateResponse("funding.html", {"request": request})


@app.get("/random")
def random_politician_route(db: Session = Depends(get_db)):
    """Jump to a random politician — discovery feature."""
    p = services.random_politician(db)
    if not p:
        return RedirectResponse(url="/browse", status_code=302)
    return RedirectResponse(url=f"/politician/{p.slug or p.id}", status_code=302)


@app.get("/constituency/{state}/{constituency}", response_class=HTMLResponse)
def constituency_detail(state: str, constituency: str, request: Request,
                          db: Session = Depends(get_db)):
    """Per-constituency drill-down. Shows top-3 candidates per cycle
    (winner + runner-up + 3rd place) across every cycle we have data for,
    with the winner highlighted and links to each candidate's detail page.

    URL params:
      state         — canonical state name (e.g., 'Delhi'); resolved via
                       resolve_state for case-insensitive matching
      constituency  — canonical constituency name (e.g., 'NEW DELHI').
                       URL-decoded by FastAPI; we look up by exact match
                       against `constituencies.name`.
    """
    # Resolve state through the canonical lookup (handles case + alias)
    state_canonical = resolve_state(state)

    data = services.constituency_top_candidates(
        db, state_name=state_canonical, constituency_name=constituency,
        limit=3,
    )
    if data is None:
        # Constituency not found in DB — render a friendly 404
        return templates.TemplateResponse(
            "not_found.html",
            {"request": request,
              "message": f"Constituency '{constituency}' not found in {state_canonical}."},
            status_code=404,
        )
    return templates.TemplateResponse("constituency_detail.html", {
        "request":     request,
        "state":       data["state"],
        "constituency": data["constituency"],
        "cycles":      data["cycles"],
        "house":       data["house"],
    })


@app.get("/politician/{slug}", response_class=HTMLResponse)
def politician_detail(slug: str, request: Request, db: Session = Depends(get_db)):
    base_query = (
        db.query(Politician)
        .options(
            joinedload(Politician.appearances).joinedload(ElectionAppearance.election),
            joinedload(Politician.appearances).joinedload(ElectionAppearance.party),
            joinedload(Politician.appearances).joinedload(ElectionAppearance.constituency),
        )
    )

    # Primary lookup: exact slug match.
    politician = base_query.filter(Politician.slug == slug).first()

    # Fallback 1: numeric slug treated as a myneta candidate ID.
    if not politician and slug.isdigit():
        politician = base_query.filter(
            Politician.myneta_candidate_id == int(slug)
        ).first()

    # Fallback 2: numeric slug treated as internal DB id (for repair scenarios).
    if not politician and slug.isdigit():
        politician = base_query.filter(Politician.id == int(slug)).first()

    # Fallback 3: case-insensitive slug match (in case of URL encoding weirdness).
    if not politician:
        politician = base_query.filter(
            func.lower(Politician.slug) == slug.lower()
        ).first()

    if not politician:
        # Render a friendly HTML 404 with search and "did you mean" suggestions.
        total = db.query(func.count(Politician.id)).scalar()
        # Build "similar" suggestions by matching any token from the slug
        # against politician names (e.g. "raj-kumar" -> finds names with "raj" or "kumar").
        tokens = [t for t in slug.replace("-", " ").split() if len(t) > 2]
        similar = []
        if tokens:
            filters = [Politician.name.ilike(f"%{t}%") for t in tokens]
            similar = (
                db.query(Politician)
                .filter(or_(*filters))
                .limit(6)
                .all()
            )
        return templates.TemplateResponse(
            "not_found.html",
            {"request": request, "slug": slug, "total": total, "similar": similar},
            status_code=404,
        )

    appearances_sorted = sorted(
        politician.appearances,
        key=lambda a: a.election.year if a.election else 0,
        reverse=True,
    )

    # Per-term delta: compare each appearance's wealth to the previous (older) one
    # so the detail page can show "+₹4.2 Cr (+34%) since 2017".
    asc = list(reversed(appearances_sorted))
    deltas: dict[int, dict] = {}
    for i in range(1, len(asc)):
        prev = asc[i-1].total_assets_inr or 0
        curr = asc[i].total_assets_inr or 0
        if prev > 0:
            deltas[asc[i].id] = {
                "delta": curr - prev,
                "pct":   (curr - prev) / prev * 100,
                "from_year": asc[i-1].election.year if asc[i-1].election else None,
            }

    # Asset trend data: for each (category, subcategory) seen across cycles,
    # build a series of (year -> value). Top 8 by max value, sorted so
    # immovable land/buildings sit first when present.
    trend_raw: dict[tuple, dict[int, int]] = {}
    for a in asc:
        if not a.election:
            continue
        for asset in (a.assets or []):
            key = (asset.category or "movable", asset.subcategory or "Other")
            trend_raw.setdefault(key, {})[a.election.year] = asset.value_inr or 0

    asset_trend_years = sorted({a.election.year for a in asc if a.election})
    trend_series = []
    for (cat, subcat), year_vals in trend_raw.items():
        peak = max(year_vals.values()) if year_vals else 0
        if peak == 0:
            continue
        # Express each series in lakhs for a more readable Y-axis
        data_in_lakhs = [round((year_vals.get(y, 0)) / 100000, 2) for y in asset_trend_years]
        trend_series.append({
            "label": (subcat[:50] + "…") if len(subcat) > 50 else subcat,
            "category": cat,
            "peak": peak,
            "data": data_in_lakhs,
        })
    trend_series.sort(key=lambda s: s["peak"], reverse=True)
    trend_series = trend_series[:8]

    # Derive politician's state so the "Asset breakdown not yet scraped" hint
    # can suggest the right ingest command (e.g. bihar_detail vs punjab_detail).
    state_name = "punjab"
    for a in appearances_sorted:
        if a.election and a.election.state_id:
            state_row = db.query(State).filter(State.id == a.election.state_id).first()
            if state_row:
                state_name = state_row.name.lower()
                break

    # Header state selector should show THIS politician's state, not
    # whatever state the user last visited on the dashboard. Falls back
    # to the URL param on 404s / edge cases via base.html's default.
    politician_state_display = None
    for a in appearances_sorted:
        if a.election and a.election.state_id:
            _row = db.query(State).filter(State.id == a.election.state_id).first()
            if _row:
                politician_state_display = _row.name
                break

    return templates.TemplateResponse("detail.html", {
        "request": request, "politician": politician,
        "appearances": appearances_sorted,
        "deltas": deltas,
        "asset_trend_years": asset_trend_years,
        "asset_trend_series": trend_series,
        "ingest_target": f"{state_name}_detail",
        # base.html reads this for the header dropdown badge.
        "state": politician_state_display,
    })


# ----- JSON API ---------------------------------------------------------------

@app.get("/api/search-index")
def api_search_index(db: Session = Depends(get_db)):
    """Compact JSON index of every searchable entity — loaded once by the
    home-page search bar, then all filtering happens client-side.

    Response shape (kept flat + short-keyed to minimize download size):
      {
        "politicians":   [{"n": name, "s": slug, "p": party, "c": const, "st": state}, ...],
        "constituencies": [{"n": name, "st": state}, ...],
        "parties":       [{"n": short_name, "f": full_name}, ...],
        "states":        [{"n": name}, ...],
      }
    """
    from .models import Politician, ElectionAppearance, Party, Constituency, State
    # Politicians — join through latest appearance for party/constituency/state.
    # De-duplicate by slug (a politician can have multiple appearances).
    pol_rows = (
        db.query(Politician, Party, Constituency, State)
        .join(ElectionAppearance, ElectionAppearance.politician_id == Politician.id)
        .outerjoin(Party, ElectionAppearance.party_id == Party.id)
        .outerjoin(Constituency, ElectionAppearance.constituency_id == Constituency.id)
        .outerjoin(State, Constituency.state_id == State.id)
        .all()
    )
    seen_slugs: set[str] = set()
    politicians = []
    for pol, party, const, state in pol_rows:
        key = pol.slug or str(pol.id)
        if key in seen_slugs:
            continue
        seen_slugs.add(key)
        politicians.append({
            "n":  pol.name,
            "s":  key,
            "p":  (party.short_name if party else "") or "",
            "c":  (const.name if const else "") or "",
            "st": (state.name if state else "") or "",
        })

    # Constituencies — one row per (name, state). house field lets clients
    # link to /constituency/<state>/<name>. Default to Assembly.
    const_rows = (
        db.query(Constituency, State)
        .join(State, Constituency.state_id == State.id)
        .filter(Constituency.house == "Assembly")
        .all()
    )
    constituencies = [{"n": c.name, "st": s.name} for c, s in const_rows]

    parties = [
        {"n": p.short_name, "f": p.full_name or ""}
        for p in db.query(Party).order_by(Party.short_name).all()
    ]
    states = [{"n": s.name} for s in db.query(State).order_by(State.name).all()]

    return {
        "politicians": politicians,
        "constituencies": constituencies,
        "parties": parties,
        "states": states,
    }


@app.get("/api/politicians")
def api_list(
    db: Session = Depends(get_db),
    limit: int = Query(50, le=200),
    offset: int = 0,
    q: str | None = None,
):
    query = db.query(Politician)
    if q:
        query = query.filter(Politician.name.ilike(f"%{q}%"))
    rows = query.order_by(Politician.name).offset(offset).limit(limit).all()
    return [
        {
            "slug": p.slug, "name": p.name,
            "myneta_candidate_id": p.myneta_candidate_id,
            "latest_appearance": _appearance_to_dict(latest_appearance(p)),
        }
        for p in rows
    ]


@app.get("/api/politicians/{slug}")
def api_detail(slug: str, db: Session = Depends(get_db)):
    p = db.query(Politician).filter(Politician.slug == slug).first()
    if not p:
        raise HTTPException(404)
    return {
        "slug": p.slug, "name": p.name,
        "appearances": [_appearance_to_dict(a) for a in p.appearances],
    }


@app.get("/api/autocomplete")
def api_autocomplete(q: str = "", db: Session = Depends(get_db), limit: int = 8):
    """Lightweight type-ahead. Returns name + constituency + party for matching politicians."""
    if not q or len(q) < 2:
        return []
    rows = (
        db.query(Politician)
        .options(joinedload(Politician.appearances).joinedload(ElectionAppearance.party))
        .filter(Politician.name.ilike(f"%{q}%"))
        .order_by(Politician.name)
        .limit(limit)
        .all()
    )
    out = []
    for p in rows:
        a = latest_appearance(p)
        out.append({
            "name": p.display_name,
            "slug": p.slug or str(p.id),
            "party": a.party.short_name if (a and a.party) else "",
            "constituency": a.constituency.name if (a and a.constituency) else "",
            "color": services.party_color(a.party.short_name if (a and a.party) else None),
        })
    return out


@app.get("/api/leaderboards")
def api_leaderboards(db: Session = Depends(get_db), limit: int = 10):
    """All four leaderboards in one shot for the homepage tabs."""
    def app_to_row(a):
        return {
            "name": a.politician.display_name,
            "slug": a.politician.slug or str(a.politician.id),
            "party": a.party.short_name if a.party else "",
            "color": services.party_color(a.party.short_name if a.party else None),
            "constituency": a.constituency.name if a.constituency else "",
            "wealth_cr": round((a.total_assets_inr or 0) / services.CRORE, 2),
            "cases": a.criminal_cases_count or 0,
            "year": a.election.year if a.election else None,
        }

    def mover_to_row(m):
        a = m["latest"]
        return {
            "name": m["politician"].display_name,
            "slug": m["politician"].slug or str(m["politician"].id),
            "party": a.party.short_name if a.party else "",
            "color": services.party_color(a.party.short_name if a.party else None),
            "from_year": m["first_year"],
            "to_year": m["last_year"],
            "from_cr": round(m["from_inr"] / services.CRORE, 2),
            "to_cr": round(m["to_inr"] / services.CRORE, 2),
            "delta_cr": round(m["delta"] / services.CRORE, 2),
            "pct": round(m["pct"], 1),
        }

    return {
        "wealth": [app_to_row(a) for a in services.top_by_wealth(db, limit)],
        "cases": [app_to_row(a) for a in services.top_by_cases(db, limit)],
        "gainers": [mover_to_row(m) for m in services.biggest_wealth_movers(db, limit, "gain")],
        "losers": [mover_to_row(m) for m in services.biggest_wealth_movers(db, limit, "loss")],
    }


@app.get("/api/trends")
def api_trends(db: Session = Depends(get_db)):
    return services.trends_by_cycle(db)


@app.get("/api/parties/stats")
def api_parties_stats(db: Session = Depends(get_db), year: int | None = Depends(resolve_year)):
    return services.party_stats(db, year)


@app.get("/api/scatter")
def api_scatter(db: Session = Depends(get_db)):
    return services.scatter_points(db)


@app.get("/api/map")
def api_map(db: Session = Depends(get_db), year: int | None = None):
    """
    Per-constituency MLA data, keyed by normalized constituency name.
    The frontend joins this against the GeoJSON polygons.
    """
    tiles = services.constituency_tiles(db, year)
    # Return as a dict keyed by an uppercase-stripped name so the JS join is robust
    # to formatting differences ("ABOHAR" vs "Abohar" vs "ABOHAR (SC)").
    def normalize(name: str) -> str:
        return (name or "").upper().replace("(SC)", "").replace("(ST)", "").strip()
    return {normalize(t["constituency"]): t for t in tiles}


@app.get("/api/_debug/politicians")
def debug_politicians(db: Session = Depends(get_db), limit: int = 50):
    """Diagnostic endpoint: list politicians with their actual slugs as stored
    in the DB. Useful when /politician/<slug> returns 404 unexpectedly."""
    rows = db.query(Politician).order_by(Politician.id).limit(limit).all()
    return [
        {
            "id": p.id,
            "name": p.name,
            "slug": p.slug,
            "myneta_candidate_id": p.myneta_candidate_id,
            "appearance_count": len(p.appearances),
            "link": f"/politician/{p.slug}",
        }
        for p in rows
    ]


@app.get("/api/stats")
def api_stats(db: Session = Depends(get_db)):
    return {
        "total_politicians": db.query(func.count(Politician.id)).scalar(),
        "total_appearances": db.query(func.count(ElectionAppearance.id)).scalar(),
        "with_criminal_cases": (
            db.query(func.count(func.distinct(ElectionAppearance.politician_id)))
            .filter(ElectionAppearance.criminal_cases_count > 0).scalar()
        ),
        "elections": [
            {"year": e.year, "house": e.house, "slug": e.myneta_slug}
            for e in db.query(Election).order_by(Election.year.desc()).all()
        ],
    }


def _appearance_to_dict(a: ElectionAppearance | None):
    if not a:
        return None
    return {
        "year": a.election.year if a.election else None,
        "house": a.election.house if a.election else None,
        "constituency": a.constituency.name if a.constituency else None,
        "party": a.party.short_name if a.party else None,
        "education": a.education,
        "total_assets_inr": a.total_assets_inr,
        "total_liabilities_inr": a.total_liabilities_inr,
        "criminal_cases_count": a.criminal_cases_count,
        "won": a.won,
        "source_url": a.source_url,
    }
