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

def trends_by_cycle(db: Session) -> list[dict]:
    """Per-cycle aggregates: # of winners, avg wealth, crorepati count, % with cases."""
    elections = db.query(Election).order_by(Election.year).all()
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

def party_stats(db: Session, election_year: Optional[int] = None) -> list[dict]:
    """Aggregate stats per party, optionally restricted to one election year."""
    q = (
        db.query(ElectionAppearance)
        .join(Party, ElectionAppearance.party_id == Party.id)
        .filter(ElectionAppearance.won.is_(True))
        .options(joinedload(ElectionAppearance.party))
    )
    if election_year:
        q = q.join(Election, ElectionAppearance.election_id == Election.id) \
             .filter(Election.year == election_year)
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

def scatter_points(db: Session) -> list[dict]:
    """Every politician's latest appearance as a (wealth, cases, party, slug) tuple."""
    apps = _latest_appearances(db)
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


def constituency_tiles(db: Session, year: Optional[int] = None) -> list[dict]:
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
    apps = _latest_appearances(db, house="Assembly")
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


# ---------------- Did You Know -----------------------------------------------

def did_you_know(db: Session) -> list[str]:
    """Auto-generated factoids computed live from the DB."""
    facts = []
    apps = _latest_appearances(db)
    if not apps:
        return ["Run the scraper to populate the database."]

    # Only consider appearances whose politician has a non-empty name —
    # avoids "faces the most criminal cases — 9 pending" with a missing name.
    named = [a for a in apps if a.politician and (a.politician.name or "").strip()]

    # Wealthiest
    if named:
        wealthiest = max(named, key=lambda a: a.total_assets_inr or 0)
        if wealthiest.total_assets_inr:
            facts.append(
                f"The wealthiest MLA in our database is {wealthiest.politician.name} "
                f"with declared assets of ₹{round((wealthiest.total_assets_inr / CRORE), 1):,} Crore."
            )

    # % with cases
    with_cases = sum(1 for a in apps if (a.criminal_cases_count or 0) > 0)
    facts.append(
        f"{round(100 * with_cases / len(apps))}% of MLAs in the database have at least one declared criminal case."
    )

    # Most cases — skip any politician without a name
    if named:
        most_cases = max(named, key=lambda a: a.criminal_cases_count or 0)
        if (most_cases.criminal_cases_count or 0) > 0:
            facts.append(
                f"{most_cases.politician.name} faces the most pending criminal cases — "
                f"{most_cases.criminal_cases_count} declared."
            )

    # Crorepati share
    crore_count = sum(1 for a in apps if (a.total_assets_inr or 0) >= CRORE)
    facts.append(
        f"{round(100 * crore_count / len(apps))}% of MLAs are crorepatis "
        f"(declared assets over ₹1 Crore)."
    )

    # Cycle range
    years = sorted({a.election.year for a in apps if a.election})
    if years:
        facts.append(
            f"Data covers Punjab assembly elections from {min(years)} to {max(years)} — "
            f"{len(years)} cycles."
        )

    # Re-contesters
    pol_count = db.query(func.count(Politician.id)).scalar()
    app_count = db.query(func.count(ElectionAppearance.id)).scalar()
    if pol_count and app_count > pol_count:
        facts.append(
            f"{app_count - pol_count} re-election entries: politicians who contested in multiple cycles."
        )

    return facts


# ---------------- Time-machine: multi-cycle data -----------------------------

def dots_by_year(db: Session, house: str = "Assembly") -> dict:
    """
    Per-cycle constituency winners, structured for the time-slider map.
    Returns {year: [{constituency, mla, party, color, wealth_cr, cases, slug}, ...]}.
    Used by the frontend to swap which dots are shown when the user drags the slider.
    """
    apps = (
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
        .all()
    )
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


def party_seats_by_year(db: Session, house: str = "Assembly") -> dict:
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
    apps = (
        db.query(ElectionAppearance)
        .join(Election, ElectionAppearance.election_id == Election.id)
        .filter(Election.house == house)
        .filter(ElectionAppearance.won.is_(True))
        .options(
            joinedload(ElectionAppearance.party),
            joinedload(ElectionAppearance.election),
        )
        .all()
    )

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
    """Four headline numbers for the hero strip. Anchored to house / scope / state."""
    apps = _latest_appearances(db, house=house, scope=scope, state_name=state_name)
    if not apps:
        return {"count": 0, "total_wealth_cr": 0, "avg_wealth_cr": 0,
                "pct_with_cases": 0, "pct_crorepati": 0, "house": house}

    wealths = [a.total_assets_inr or 0 for a in apps]
    with_cases = sum(1 for a in apps if (a.criminal_cases_count or 0) > 0)
    crorepati = sum(1 for w in wealths if w >= CRORE)

    return {
        "house": house,
        "count": len(apps),
        "total_wealth_cr": round(sum(wealths) / CRORE, 0),
        "avg_wealth_cr":   round(sum(wealths) / len(apps) / CRORE, 1) if apps else 0,
        "pct_with_cases":  round(100 * with_cases / len(apps), 0),
        "pct_crorepati":   round(100 * crorepati / len(apps), 0),
    }


def wealth_multipliers(db: Session, limit: int = 10, house: str = "Assembly") -> list[dict]:
    """
    Politicians with the biggest *percentage* wealth growth between any
    two of their appearances. Much more compelling than absolute rupee gains.
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
    rows = []
    for p in politicians:
        valid = [a for a in p.appearances
                 if a.election and a.election.house == house
                 and a.total_assets_inr is not None]
        if len(valid) < 2:
            continue
        valid.sort(key=lambda a: a.election.year)
        first, last = valid[0], valid[-1]
        # Skip noisy edge cases (zero or tiny first declaration distorts %)
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


def crorepati_newcomers(db: Session, limit: int = 10, house: str = "Assembly") -> list[dict]:
    """
    Politicians who were sub-crore in their first declared affidavit and
    are crorepati now — the "not rich before politics" story.
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
    rows = []
    for p in politicians:
        valid = [a for a in p.appearances
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


def anomaly_candidates(db: Session, limit: int = 50, house: str = "Assembly") -> list[dict]:
    """
    Surface politicians with statistically unusual asset growth as 'anomalies'.
    Reuses wealth_multipliers and attaches an integrity-index score:
        score = max(0, 100 - log10(pct + 1) * 25) - 5 * pending_cases
    A 100% growth ≈ 75 (standard). 10,000% ≈ 0 (critical). Cases reduce the score.
    """
    import math
    rows = wealth_multipliers(db, limit=limit * 3, house=house)
    out = []
    for r in rows:
        if r["pct"] <= 50:  # tiny growth — not interesting
            continue
        cases = r["latest"].criminal_cases_count or 0
        score = max(0, 100 - math.log10(r["pct"] + 1) * 25) - 5 * cases
        score = max(0, min(100, score))
        out.append({**r, "score": score})
    out.sort(key=lambda r: r["score"])  # lowest score first = highest risk
    return out[:limit]


def anomaly_buckets(db: Session, house: str = "Assembly") -> dict:
    """Return counts of candidates in each risk bucket (critical/suspicious/standard)."""
    candidates = anomaly_candidates(db, limit=500, house=house)
    return {
        "critical":    sum(1 for r in candidates if r["score"] < 30),
        "suspicious":  sum(1 for r in candidates if 30 <= r["score"] < 60),
        "standard":    sum(1 for r in candidates if r["score"] >= 60),
    }


def party_switchers(db: Session, limit: int = 20) -> list[dict]:
    """
    Find politicians who appeared with more than one party across their
    election appearances — the "Aaya Ram Gaya Ram" phenomenon.

    Returns rows with the politician's full party journey, sorted by
    number of switches (most-switches first).
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
    rows = []
    for p in politicians:
        # Build a chronological list of (year, party_name) skipping rows without party
        history = []
        for a in sorted(p.appearances, key=lambda a: a.election.year if a.election else 0):
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


def long_servers(db: Session, limit: int = 10, house: str = "Assembly") -> list[dict]:
    """Politicians who've won in the most election cycles in this house."""
    politicians = (
        db.query(Politician)
        .options(
            joinedload(Politician.appearances).joinedload(ElectionAppearance.election),
            joinedload(Politician.appearances).joinedload(ElectionAppearance.party),
            joinedload(Politician.appearances).joinedload(ElectionAppearance.constituency),
        )
        .all()
    )
    rows = []
    for p in politicians:
        wins = [a for a in p.appearances
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
