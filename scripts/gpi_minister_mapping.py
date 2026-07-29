"""Canonical portfolio → GPI pillar mapping for state cabinet ministers.

Wikipedia (and state portals) label portfolios inconsistently across states:
  Punjab:    "Finance, Planning and Programme Implementation"
  Delhi:     "Finance and Planning"
  Kerala:    "Finance & Revenue"
  Karnataka: "Finance, Planning and Statistics"

We normalise the raw label into a `portfolio_key` and then map that key to
one of our GPI pillars (or None if it doesn't fit).

Match rules — case-insensitive keyword search on the raw label. Order matters:
we check specific portfolios (Home, Health, etc.) before general ones (General
Administration → governance).
"""
from __future__ import annotations

# Ordered list of (portfolio_key, pillar_code, keywords). First match wins.
# Keywords are checked as substrings against the lowercased raw label.
PORTFOLIO_RULES = [
    # ─── Public Finance ────────────────────────────────────────────────
    ("finance",       "public_finance",
        ["finance", "budget", "planning"]),
    ("revenue",       "public_finance",
        ["revenue", "commercial tax", "excise", "stamps", "registration"]),

    # ─── Healthcare ────────────────────────────────────────────────────
    ("health",        "healthcare",
        ["health", "medical education", "family welfare", "medical & family",
         "public health", "ayush"]),
    ("wcd",           "healthcare",
        ["women", "child development", "wcd"]),

    # ─── Education ─────────────────────────────────────────────────────
    ("education",     "education",
        ["school education", "higher education", "technical education",
         "education,", "educational", " education "]),
    # Bare "education" as a full word — catch cases like "Education" as the
    # entire portfolio string. Placed after the more-specific rules.
    ("education_gen", "education", ["education"]),

    # ─── Law & Order / Home ────────────────────────────────────────────
    ("home",          "law_and_order",
        ["home", "police", "prisons", "vigilance", "law and order",
         "law & order"]),
    ("law",           "law_and_order",
        ["law", "justice", "parliamentary affairs", "legislative affairs"]),

    # ─── Infrastructure ────────────────────────────────────────────────
    ("pwd",           "infrastructure",
        ["public works", "pwd", "roads and buildings", "roads & buildings",
         "land & building", "land and building"]),
    ("rural_dev",     "infrastructure",
        ["rural development", "panchayati raj", "panchayats"]),
    ("urban_dev",     "infrastructure",
        ["urban development", "urban affairs", "urban administration",
         "housing", "municipal", "town and country planning",
         "town & country planning"]),
    ("power",         "infrastructure",
        ["power", "energy", "renewable energy", "electricity"]),
    ("water",         "infrastructure",
        ["water resources", "jal shakti", "irrigation", "water supply",
         "drinking water", "water"]),
    ("transport",     "infrastructure",
        ["transport", "civil aviation", "shipping", "roads and transport"]),

    # ─── Economy ───────────────────────────────────────────────────────
    ("industry",      "economy",
        ["industry", "industries", "commerce", "msme", "small industries",
         "industrial development", "cottage"]),
    ("it_electronics", "economy",
        ["information technology", "electronics", "digital", " it ", " it,"]),
    ("agriculture",   "economy",
        ["agriculture", "farmers welfare", "horticulture",
         "animal husbandry", "cooperation", "fisheries"]),
    ("labour",        "economy",
        ["labour", "employment"]),
    ("food_supply",   "economy",
        ["food & supplies", "food and supplies", "food, civil supplies",
         "civil supplies", "consumer affairs"]),
    ("tourism",       "economy",
        ["tourism", "culture and tourism"]),

    # ─── Governance ────────────────────────────────────────────────────
    # General Admin catches Chief Secretary / DoPT / Personnel-type portfolios
    ("general_admin", "governance",
        ["general administration", "personnel", "administrative reforms",
         "dopt", "public grievances", "public grievance", "governance",
         "information and public relation", "information & public relation",
         "public relations", "services", "printing", "stationery"]),
]


def classify_portfolio(raw_label: str) -> tuple[str, str] | None:
    """Return (portfolio_key, pillar_code) for a raw Wikipedia label, or
    None if no rule matches (portfolio doesn't fit any GPI pillar)."""
    if not raw_label:
        return None
    label = raw_label.lower().strip()
    for key, pillar, keywords in PORTFOLIO_RULES:
        for kw in keywords:
            if kw in label:
                return (key, pillar)
    return None


# Pillar → display label for the UI chip
PILLAR_MINISTER_LABEL = {
    "economy":        "Industry / Economy Minister",
    "public_finance": "Finance Minister",
    "education":      "Education Minister",
    "healthcare":     "Health Minister",
    "infrastructure": "Infrastructure Minister",
    "law_and_order":  "Home Minister",
    "governance":     "General Admin Minister",
    "efficiency":     None,   # cross-cutting; usually CM
}
