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
    # Free-text note shown in the Data Coverage banner if this state has
    # known gaps (e.g. "2008 cycle missing", "Detail enrichment ~70%").
    # Leave blank if coverage is complete.
    coverage_notes: str = ""
    # Geographic zone — used by the homepage's Constituency Deep-Dive panel
    # to aggregate states into North / South / East / West / Northeast cards.
    # One of: "North", "South", "East", "West", "Northeast", "Central".
    zone: str = ""


PUNJAB = StateConfig(
    key="punjab",
    name="Punjab",
    code="PB",
    zone="North",
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
    zone="East",
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
    zone="West",
    # myneta uses these slugs for Goa assembly cycles
    assembly_cycles=[
        {"year": 2022, "slug": "Goa2022"},
        {"year": 2017, "slug": "goa2017"},
        {"year": 2012, "slug": "goa2012"},
        {"year": 2007, "slug": "goa2007"},
    ],
    ls_pcs={"NORTH GOA", "SOUTH GOA"},
)

# Sikkim — 32 assembly seats, 1 Lok Sabha seat.
# Slugs are best-guess; verify with scripts/verify_state_slugs.py before
# committing to a full scrape (myneta's casing is inconsistent across years).
SIKKIM = StateConfig(
    key="sikkim",
    name="Sikkim",
    code="SK",
    zone="Northeast",
    assembly_cycles=[
        {"year": 2024, "slug": "Sikkim2024"},
        {"year": 2019, "slug": "sikkim2019"},
        {"year": 2014, "slug": "sikkim2014"},
        {"year": 2009, "slug": "sikkim2009"},
    ],
    ls_pcs={"SIKKIM"},
    coverage_notes="Per-affidavit detail enrichment partial.",
)

# Delhi (NCT) — 70 assembly seats, 7 Lok Sabha seats.
# Includes the recently-concluded Feb 2025 cycle.
DELHI = StateConfig(
    key="delhi",
    name="Delhi",
    code="DL",
    zone="North",
    assembly_cycles=[
        {"year": 2025, "slug": "Delhi2025"},
        {"year": 2020, "slug": "delhi2020"},
        {"year": 2015, "slug": "delhi2015"},
        {"year": 2013, "slug": "delhi2013"},
        {"year": 2008, "slug": "delhi2008"},
    ],
    ls_pcs={
        "CHANDNI CHOWK", "NORTH EAST DELHI", "EAST DELHI",
        "NEW DELHI", "NORTH WEST DELHI", "WEST DELHI", "SOUTH DELHI",
    },
    coverage_notes="2008 cycle not yet loaded; per-affidavit detail enrichment ~50%.",
)


# ---------------------------------------------------------------------------
# Small states / UTs — added in a single batch. Slugs are best-guess based on
# myneta's casing conventions (Title-case for the latest cycle, lowercase for
# older ones). Verify with `python scripts/verify_state_slugs.py` before
# committing to any multi-hour scrape.
# ---------------------------------------------------------------------------

PUDUCHERRY = StateConfig(
    key="puducherry", name="Puducherry", code="PY", zone="South",
    assembly_cycles=[
        {"year": 2021, "slug": "Puducherry2021"},
        {"year": 2016, "slug": "puducherry2016"},
        {"year": 2011, "slug": "puducherry2011"},
        {"year": 2006, "slug": "pondicherry2006"},
    ],
    ls_pcs={"PUDUCHERRY"},
)

MIZORAM = StateConfig(
    key="mizoram", name="Mizoram", code="MZ", zone="Northeast",
    assembly_cycles=[
        {"year": 2023, "slug": "Mizoram2023"},
        {"year": 2018, "slug": "mizoram2018"},
        {"year": 2013, "slug": "mizoram2013"},
        {"year": 2008, "slug": "mizoram2008"},
    ],
    ls_pcs={"MIZORAM"},
)

MANIPUR = StateConfig(
    key="manipur", name="Manipur", code="MN", zone="Northeast",
    assembly_cycles=[
        {"year": 2022, "slug": "Manipur2022"},
        {"year": 2017, "slug": "manipur2017"},
        {"year": 2012, "slug": "manipur2012"},
        {"year": 2007, "slug": "manipur2007"},
    ],
    ls_pcs={"INNER MANIPUR", "OUTER MANIPUR"},
)

MEGHALAYA = StateConfig(
    key="meghalaya", name="Meghalaya", code="ML", zone="Northeast",
    assembly_cycles=[
        {"year": 2023, "slug": "Meghalaya2023"},
        {"year": 2018, "slug": "meghalaya2018"},
        {"year": 2013, "slug": "meghalaya2013"},
        {"year": 2008, "slug": "meghalaya2008"},
    ],
    ls_pcs={"SHILLONG", "TURA"},
)

NAGALAND = StateConfig(
    key="nagaland", name="Nagaland", code="NL", zone="Northeast",
    assembly_cycles=[
        {"year": 2023, "slug": "Nagaland2023"},
        {"year": 2018, "slug": "nagaland2018"},
        {"year": 2013, "slug": "nagaland2013"},
        {"year": 2008, "slug": "nagaland2008"},
    ],
    ls_pcs={"NAGALAND"},
)

TRIPURA = StateConfig(
    key="tripura", name="Tripura", code="TR", zone="Northeast",
    assembly_cycles=[
        {"year": 2023, "slug": "Tripura2023"},
        {"year": 2018, "slug": "tripura2018"},
        {"year": 2013, "slug": "tripura2013"},
        {"year": 2008, "slug": "tripura2008"},
    ],
    ls_pcs={"TRIPURA WEST", "TRIPURA EAST"},
)

ARUNACHAL = StateConfig(
    key="arunachal", name="Arunachal Pradesh", code="AR", zone="Northeast",
    assembly_cycles=[
        {"year": 2024, "slug": "Arunachal2024"},
        {"year": 2019, "slug": "arunachal2019"},
        {"year": 2014, "slug": "arunachal2014"},
        {"year": 2009, "slug": "arunachal2009"},
    ],
    ls_pcs={"ARUNACHAL WEST", "ARUNACHAL EAST"},
)

HIMACHAL = StateConfig(
    key="himachal", name="Himachal Pradesh", code="HP", zone="North",
    assembly_cycles=[
        {"year": 2022, "slug": "HimachalPradesh2022"},
        {"year": 2017, "slug": "himachal2017"},
        {"year": 2012, "slug": "hp2012"},
        {"year": 2007, "slug": "hp2007"},
    ],
    ls_pcs={"KANGRA", "MANDI", "HAMIRPUR", "SHIMLA"},
)

UTTARAKHAND = StateConfig(
    key="uttarakhand", name="Uttarakhand", code="UK", zone="Central",
    assembly_cycles=[
        {"year": 2022, "slug": "Uttarakhand2022"},
        {"year": 2017, "slug": "uttarakhand2017"},
        {"year": 2012, "slug": "uttarakhand2012"},
        {"year": 2007, "slug": "uttarakhand2007"},
    ],
    ls_pcs={"TEHRI GARHWAL", "GARHWAL", "ALMORA", "NAINITAL-UDHAMSINGH NAGAR", "HARDWAR"},
)

# Next-smallest tier — 81-90 seat states. All with very recent (2023-24) cycles.
JHARKHAND = StateConfig(
    key="jharkhand", name="Jharkhand", code="JH", zone="East",
    assembly_cycles=[
        {"year": 2024, "slug": "Jharkhand2024"},
        {"year": 2019, "slug": "jharkhand2019"},
        {"year": 2014, "slug": "jharkhand2014"},
        {"year": 2009, "slug": "jharkhand2009"},
        {"year": 2005, "slug": "jharkhand2005"},
    ],
    ls_pcs={"RAJMAHAL", "DUMKA", "GODDA", "CHATRA", "KODARMA",
            "GIRIDIH", "DHANBAD", "RANCHI", "JAMSHEDPUR", "SINGHBHUM",
            "KHUNTI", "LOHARDAGA", "PALAMU", "HAZARIBAGH"},
)

HARYANA = StateConfig(
    key="haryana", name="Haryana", code="HR", zone="North",
    assembly_cycles=[
        {"year": 2024, "slug": "Haryana2024"},
        {"year": 2019, "slug": "haryana2019"},
        {"year": 2014, "slug": "haryana2014"},
        {"year": 2009, "slug": "haryana2009"},
        {"year": 2005, "slug": "haryana2005"},
    ],
    ls_pcs={"AMBALA", "KURUKSHETRA", "SIRSA", "HISAR", "KARNAL",
            "SONIPAT", "ROHTAK", "BHIWANI-MAHENDRAGARH", "GURGAON", "FARIDABAD"},
)

CHHATTISGARH = StateConfig(
    key="chhattisgarh", name="Chhattisgarh", code="CG", zone="Central",
    assembly_cycles=[
        {"year": 2023, "slug": "Chhattisgarh2023"},
        {"year": 2018, "slug": "chhattisgarh2018"},
        {"year": 2013, "slug": "chhattisgarh2013"},
        {"year": 2008, "slug": "chhattisgarh2008"},
        {"year": 2003, "slug": "chhattisgarh2003"},
    ],
    ls_pcs={"SARGUJA", "RAIGARH", "JANJGIR-CHAMPA", "KORBA", "BILASPUR",
            "RAJNANDGAON", "DURG", "RAIPUR", "MAHASAMUND", "BASTAR", "KANKER"},
)


# ---------------------------------------------------------------------------
# Next batch: 90-126 seat states across three zones to balance the panel.
# - J&K (90, North): first assembly post-Art 370 reorganization in 2024
# - Telangana (119, South): formed 2014; first proper South-zone state
# - Assam (126, Northeast): the populous NE state
# ---------------------------------------------------------------------------

JK = StateConfig(
    key="jk", name="Jammu and Kashmir", code="JK", zone="North",
    assembly_cycles=[
        # 2024 was the first cycle after the Aug 2019 reorganization into a UT.
        # Earlier cycles (2014, 2008, 2002) were the J&K state including Ladakh,
        # so historical slugs follow the pre-reorganization naming on myneta.
        {"year": 2024, "slug": "JK2024"},
        {"year": 2014, "slug": "jk2014"},
        {"year": 2008, "slug": "jk2008"},
        {"year": 2002, "slug": "jk2002"},
    ],
    ls_pcs={"BARAMULLA", "SRINAGAR", "ANANTNAG", "UDHAMPUR", "JAMMU"},
)

TELANGANA = StateConfig(
    key="telangana", name="Telangana", code="TG", zone="South",
    assembly_cycles=[
        {"year": 2023, "slug": "Telangana2023"},
        {"year": 2018, "slug": "telangana2018"},
        # First-ever Telangana election after bifurcation from AP.
        {"year": 2014, "slug": "telangana2014"},
    ],
    ls_pcs={
        "ADILABAD", "PEDDAPALLI", "KARIMNAGAR", "NIZAMABAD", "ZAHIRABAD",
        "MEDAK", "MALKAJGIRI", "SECUNDERABAD", "HYDERABAD", "CHELVELLA",
        "MAHBUBNAGAR", "NAGARKURNOOL", "NALGONDA", "BHONGIR", "WARANGAL",
        "MAHABUBABAD", "KHAMMAM",
    },
)

ASSAM = StateConfig(
    key="assam", name="Assam", code="AS", zone="Northeast",
    assembly_cycles=[
        {"year": 2021, "slug": "Assam2021"},
        {"year": 2016, "slug": "assam2016"},
        {"year": 2011, "slug": "assam2011"},
        {"year": 2006, "slug": "assam2006"},
    ],
    ls_pcs={
        "KARIMGANJ", "SILCHAR", "AUTONOMOUS DISTRICT", "DHUBRI", "KOKRAJHAR",
        "BARPETA", "GAUHATI", "MANGALDOI", "TEZPUR", "NOWGONG", "KALIABOR",
        "JORHAT", "DIBRUGARH", "LAKHIMPUR",
    },
)


ALL_STATES: dict[str, StateConfig] = {
    "punjab":       PUNJAB,
    "bihar":        BIHAR,
    "goa":          GOA,
    "sikkim":       SIKKIM,
    "delhi":        DELHI,
    "puducherry":   PUDUCHERRY,
    "mizoram":      MIZORAM,
    "manipur":      MANIPUR,
    "meghalaya":    MEGHALAYA,
    "nagaland":     NAGALAND,
    "tripura":      TRIPURA,
    "arunachal":    ARUNACHAL,
    "himachal":     HIMACHAL,
    "uttarakhand":  UTTARAKHAND,
    "jharkhand":    JHARKHAND,
    "haryana":      HARYANA,
    "chhattisgarh": CHHATTISGARH,
    "jk":           JK,
    "telangana":    TELANGANA,
    "assam":        ASSAM,
}


def get_state(key: str) -> StateConfig:
    """Lookup helper — raises KeyError for unknown state."""
    return ALL_STATES[key.lower()]
