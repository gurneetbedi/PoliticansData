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

Base.metadata.create_all(bind=engine)

# Absolute paths resolved from this file so static/templates work regardless
# of the cwd gunicorn happens to start in (Render/Heroku/etc. may differ).
APP_DIR = Path(__file__).resolve().parent
STATIC_DIR = APP_DIR / "static"
TEMPLATES_DIR = APP_DIR / "templates"

app = FastAPI(title="PoliTrack India", version="0.1.0")
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))


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

KNOWN_STATES = {"punjab", "bihar", "goa", "sikkim", "delhi"}

def resolve_state(state: str | None = None) -> str:
    """Return a canonical state name. Falls back to 'Punjab' for empty/unknown."""
    if not state:
        return "Punjab"
    s = state.strip().lower()
    if s not in KNOWN_STATES:
        return "Punjab"
    return s.capitalize()


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
    scope: str = Query("current", regex="^(current|all)$"),
    state: str = Depends(resolve_state),
):
    """
    The view toggle selects which legislative body to focus the page on:
      mla  → Punjab Assembly (default; 117 seats; richest dataset)
      mp   → Punjab Lok Sabha MPs (13 seats; requires `python -m app.ingest punjab_ls`)
      rs   → Punjab Rajya Sabha (7 seats; data lives in app/data/punjab_rs.py)

    The scope toggle:
      current → only politicians who won in the latest cycle (default — what users expect)
      all     → every politician who ever won, with their most recent declared data
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
        "facts":    services.did_you_know(db, state_name=state),

        # India-wide stats for the choropleth (one row per tracked state).
        # Goa is included so once ingested it auto-appears on the map.
        "india_states": [
            {
                "name": s_name,
                "kpi": services.hero_kpis(db, house="Assembly", scope="current", state_name=s_name),
            }
            for s_name in ("Punjab", "Bihar", "Goa", "Sikkim", "Delhi")
        ],

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
    return templates.TemplateResponse("heatmap.html", {
        "request": request,
        "state":   state,
        "tiles":   services.constituency_tiles(db, state_name=state),
        "kpis":    services.hero_kpis(db, house="Assembly", scope="current", state_name=state),
        "india_states": [
            {"name": s_name, "kpi": services.hero_kpis(db, house="Assembly", scope="current", state_name=s_name)}
            for s_name in ("Punjab", "Bihar", "Goa", "Sikkim", "Delhi")
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

    return templates.TemplateResponse("detail.html", {
        "request": request, "politician": politician,
        "appearances": appearances_sorted,
        "deltas": deltas,
        "asset_trend_years": asset_trend_years,
        "asset_trend_series": trend_series,
        "ingest_target": f"{state_name}_detail",
    })


# ----- JSON API ---------------------------------------------------------------

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
