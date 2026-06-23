"""
FastAPI routes for the ECI-sourced candidate data.

Reads from `eci_candidates_provisional` (built by load_eci_to_db.py +
backfill_eci_from_manifest.py). NEVER reads from the myneta-sourced
tables (`politicians`, `election_appearances`, etc.) — those exist in the
DB but are used only for internal cross-checking in `scripts/`.

Three view tiers, each with its own reliability disclaimer level in the
Jinja template:

  Tier 1 — VERIFIED (no disclaimer):
    name, party, constituency, affidavit_status, eStamp cert
    (sourced from ECI listing card / eStamp cover — never auto-extracted)

  Tier 2 — AUTO-EXTRACTED, USUALLY OK (mild disclaimer):
    age, father_or_husband, education, profession_self
    (regex extraction with mid hit rates; usually right when present)

  Tier 3 — AUTO-EXTRACTED, MAY BE INACCURATE (strong disclaimer):
    pending_cases, convictions, movable_self, movable_spouse,
    liabilities_bank, liabilities_disputed
    (regex extraction with low hit rates AND known accuracy issues —
    must be verified against the source PDF before citing)
"""
from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Request, HTTPException
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import text

from app.database import engine


router = APIRouter(prefix="/eci", tags=["eci"])

TEMPLATES = Jinja2Templates(
    directory=str(Path(__file__).resolve().parent / "templates")
)


# ---------------------------------------------------------------------------
# Field-reliability declaration — used by the templates to decide which
# tier each rendered field belongs to. Keeping the source-of-truth here
# means we change a single dictionary when extraction quality improves.
# ---------------------------------------------------------------------------

FIELD_TIERS = {
    # Tier 1 — ECI listing card / eStamp cover, verified, no disclaimer
    "candidate_name":           {"tier": 1, "label": "Name"},
    "party":                    {"tier": 1, "label": "Party"},
    "constituency":             {"tier": 1, "label": "Constituency"},
    "affidavit_status":         {"tier": 1, "label": "Affidavit status"},
    "estamp_cert":              {"tier": 1, "label": "eStamp certificate"},
    "estamp_date":              {"tier": 1, "label": "Affidavit dated"},

    # Tier 2 — Regex extraction, generally reliable, mild disclaimer
    "father_or_husband":        {"tier": 2, "label": "Father / Husband"},
    "age":                      {"tier": 2, "label": "Age"},
    "education":                {"tier": 2, "label": "Education"},
    "profession_self":          {"tier": 2, "label": "Profession (self)"},

    # Tier 3 — Regex extraction, known accuracy issues, STRONG disclaimer
    "pending_cases":            {"tier": 3, "label": "Pending criminal cases"},
    "convictions":              {"tier": 3, "label": "Convictions"},
    "movable_self":             {"tier": 3, "label": "Movable assets (self)"},
    "movable_spouse":           {"tier": 3, "label": "Movable assets (spouse)"},
    "liabilities_bank":         {"tier": 3, "label": "Bank liabilities"},
    "liabilities_disputed":     {"tier": 3, "label": "Disputed liabilities"},
}


# ---------------------------------------------------------------------------
# DB helpers — raw SQL because eci_candidates_provisional was created
# outside the SQLAlchemy ORM (it's a flat-table experimental layer).
# ---------------------------------------------------------------------------

def _query_one(sql: str, params: dict) -> dict | None:
    with engine.connect() as con:
        result = con.execute(text(sql), params).mappings().fetchone()
    return dict(result) if result else None


def _query_all(sql: str, params: dict | None = None) -> list[dict]:
    with engine.connect() as con:
        result = con.execute(text(sql), params or {}).mappings().fetchall()
    return [dict(r) for r in result]


def _format_inr(v) -> str | None:
    """Format an integer as Indian-rupee comma grouping. e.g. 6724979 → ₹67,24,979."""
    if v is None or v == "":
        return None
    try:
        n = int(v)
    except (ValueError, TypeError):
        return str(v)
    s = str(abs(n))
    # Indian grouping: last 3 digits, then groups of 2
    if len(s) > 3:
        head, tail = s[:-3], s[-3:]
        groups = []
        while len(head) > 2:
            groups.append(head[-2:])
            head = head[:-2]
        if head:
            groups.append(head)
        head = ",".join(reversed(groups))
        s = f"{head},{tail}"
    return f"{'-' if n < 0 else ''}₹{s}"


def _source_pdf_url(source_pdf: str | None) -> str | None:
    """Return a publicly-accessible URL for the source affidavit PDF.
    For now we just link to the original ECI portal — when we host PDFs
    ourselves we'd serve from /static/affidavits/<filename>."""
    if not source_pdf:
        return None
    # TODO when we publish PDFs: return f"/static/affidavits/{source_pdf}"
    return "https://affidavit.eci.gov.in/"


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@router.get("/", response_class=HTMLResponse)
async def eci_landing(request: Request):
    """List of states/elections covered by the ECI pipeline.
    Today: just Delhi 2025. As we add states, this auto-extends."""
    rows = _query_all("""
        SELECT state, election_year, election_type,
               COUNT(*) AS candidate_count
        FROM eci_candidates_provisional
        GROUP BY state, election_year, election_type
        ORDER BY state, election_year DESC
    """)
    return TEMPLATES.TemplateResponse(
        "eci/landing.html",
        {"request": request, "elections": rows},
    )


@router.get("/{state}/{year}", response_class=HTMLResponse)
async def constituency_list(request: Request, state: str, year: int):
    """List of constituencies in an election with candidate counts."""
    constituencies = _query_all("""
        SELECT constituency, COUNT(*) AS candidate_count
        FROM eci_candidates_provisional
        WHERE state = :state AND election_year = :year
              AND affidavit_status = 'Accepted'
        GROUP BY constituency
        ORDER BY constituency
    """, {"state": state.title(), "year": year})

    if not constituencies:
        raise HTTPException(404, f"No data for {state} {year}")

    return TEMPLATES.TemplateResponse(
        "eci/constituency_list.html",
        {
            "request": request, "state": state.title(),
            "year": year, "constituencies": constituencies,
        },
    )


@router.get("/{state}/{year}/{constituency}", response_class=HTMLResponse)
async def constituency_detail(request: Request, state: str, year: int,
                                constituency: str):
    """All Accepted candidates for one constituency."""
    candidates = _query_all("""
        SELECT id, candidate_name, party, fields_present_count,
               quality_status, source_pdf, affidavit_id, estamp_cert
        FROM eci_candidates_provisional
        WHERE state = :state AND election_year = :year
              AND constituency = :constituency
              AND affidavit_status = 'Accepted'
        ORDER BY candidate_name
    """, {
        "state": state.title(), "year": year,
        "constituency": constituency.upper(),
    })

    if not candidates:
        raise HTTPException(404, f"No candidates for {constituency}")

    return TEMPLATES.TemplateResponse(
        "eci/constituency_detail.html",
        {
            "request": request, "state": state.title(), "year": year,
            "constituency": constituency.upper(), "candidates": candidates,
        },
    )


@router.get("/candidate/{candidate_id}", response_class=HTMLResponse)
async def candidate_detail(request: Request, candidate_id: int):
    """Full profile for one ECI candidate — every field we have, with
    appropriate disclaimer tier per field."""
    row = _query_one(
        "SELECT * FROM eci_candidates_provisional WHERE id = :id",
        {"id": candidate_id},
    )
    if not row:
        raise HTTPException(404, f"Candidate {candidate_id} not found")

    # Build a tier → fields lookup so the template can iterate cleanly
    tier1: list[dict] = []
    tier2: list[dict] = []
    tier3: list[dict] = []

    for db_field, meta in FIELD_TIERS.items():
        value = row.get(db_field)
        # Apply Indian rupee formatting for amounts
        is_money = db_field in (
            "movable_self", "movable_spouse",
            "liabilities_bank", "liabilities_disputed",
        )
        display_value = _format_inr(value) if is_money else value
        entry = {
            "key": db_field, "label": meta["label"],
            "value": display_value, "is_money": is_money,
            "is_empty": value is None or value == "",
        }
        if meta["tier"] == 1:
            tier1.append(entry)
        elif meta["tier"] == 2:
            tier2.append(entry)
        else:
            tier3.append(entry)

    return TEMPLATES.TemplateResponse(
        "eci/candidate_detail.html",
        {
            "request": request, "row": row,
            "tier1": tier1, "tier2": tier2, "tier3": tier3,
            "source_pdf_url": _source_pdf_url(row.get("source_pdf")),
        },
    )
