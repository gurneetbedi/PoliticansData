"""
State configuration — one entry per Indian state we cover.

Adding a new state means adding a new entry here. Everything downstream
(scraper, ingest target, browse filter, heatmap) reads from this registry.
"""
from dataclasses import dataclass, field


@dataclass
class StateConfig:
    """Configuration for one Indian state on myneta."""
    key: str                          # short slug, e.g. "punjab", "bihar"
    name: str                         # display name, e.g. "Punjab"
    code: str                         # 2-char ECI code, e.g. "PB", "BR"
    assembly_cycles: list[dict]       # [{year: 2022, slug: "punjab2022"}, ...]
    ls_pcs: set[str] = field(default_factory=set)   # set of LS PC names in this state


PUNJAB = StateConfig(
    key="punjab",
    name="Punjab",
    code="PB",
    assembly_cycles=[
        {"year": 2022, "slug": "punjab2022"},
        {"year": 2017, "slug": "punjab2017"},
        {"year": 2012, "slug": "pb2012"},
        {"year": 2007, "slug": "pb2007"},
    ],
    ls_pcs={
        "GURDASPUR", "AMRITSAR", "KHADOOR SAHIB", "KHADUR SAHIB",
        "JALANDHAR", "HOSHIARPUR", "ANANDPUR SAHIB", "LUDHIANA",
        "FATEHGARH SAHIB", "FARIDKOT", "FIROZPUR", "FEROZEPUR",
        "BATHINDA", "SANGRUR", "PATIALA",
    },
)

BIHAR = StateConfig(
    key="bihar",
    name="Bihar",
    code="BR",
    # myneta uses lowercase short names for Bihar cycles
    assembly_cycles=[
        {"year": 2020, "slug": "bihar2020"},
        {"year": 2015, "slug": "bihar2015"},
        {"year": 2010, "slug": "bihar2010"},
        {"year": 2005, "slug": "bihar2005"},
    ],
    ls_pcs={
        "BAGAHA", "VALMIKI NAGAR", "PASCHIM CHAMPARAN", "PURVI CHAMPARAN",
        "SHEOHAR", "SITAMARHI", "MADHUBANI", "JHANJHARPUR", "SUPAUL",
        "ARARIA", "KISHANGANJ", "KATIHAR", "PURNIA", "MADHEPURA",
        "DARBHANGA", "MUZAFFARPUR", "VAISHALI", "GOPALGANJ", "SIWAN",
        "MAHARAJGANJ", "SARAN", "HAJIPUR", "UJIARPUR", "SAMASTIPUR",
        "BEGUSARAI", "KHAGARIA", "BHAGALPUR", "BANKA", "MUNGER",
        "NALANDA", "PATNA SAHIB", "PATALIPUTRA", "ARRAH", "BUXAR",
        "SASARAM", "KARAKAT", "JAHANABAD", "AURANGABAD", "GAYA",
        "NAWADA", "JAMUI",
    },
)

GOA = StateConfig(
    key="goa",
    name="Goa",
    code="GA",
    # myneta uses these slugs for Goa assembly cycles
    assembly_cycles=[
        {"year": 2022, "slug": "Goa2022"},
        {"year": 2017, "slug": "goa2017"},
        {"year": 2012, "slug": "goa2012"},
        {"year": 2007, "slug": "goa2007"},
    ],
    ls_pcs={"NORTH GOA", "SOUTH GOA"},
)


ALL_STATES: dict[str, StateConfig] = {
    "punjab": PUNJAB,
    "bihar":  BIHAR,
    "goa":    GOA,
}


def get_state(key: str) -> StateConfig:
    """Lookup helper — raises KeyError for unknown state."""
    return ALL_STATES[key.lower()]
