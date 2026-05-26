"""
Mapping from Punjab Assembly Constituency (AC) name -> Lok Sabha
Parliamentary Constituency (PC) name, based on ECI delimitation (2008).

Each of Punjab's 13 LS PCs contains 9 ACs. Names are normalized
(uppercase, no SC/ST suffixes, no whitespace at ends). The map is
intentionally maintainable-by-hand; small corrections from users are
welcome and easy to verify against ECI's published delimitation order.

Lookup is via `ls_pc_for_ac(name)` which normalizes the input before lookup.
"""
import re

# Format: {LS_PC_NAME: [AC1, AC2, ...]}
LS_PC_TO_ACS: dict[str, list[str]] = {
    "GURDASPUR": [
        "SUJANPUR", "BHOA", "PATHANKOT", "GURDASPUR", "DINA NAGAR",
        "QADIAN", "BATALA", "FATEHGARH CHURIAN", "DERA BABA NANAK",
    ],
    "AMRITSAR": [
        "AJNALA", "RAJA SANSI", "MAJITHA", "JANDIALA",
        "AMRITSAR NORTH", "AMRITSAR WEST", "AMRITSAR CENTRAL",
        "AMRITSAR EAST", "AMRITSAR SOUTH",
    ],
    "KHADOOR SAHIB": [
        "ATTARI", "TARN TARAN", "KHEM KARAN", "PATTI", "KHADOOR SAHIB",
        "BABA BAKALA", "KAPURTHALA", "SULTANPUR LODHI", "ZIRA",
    ],
    "JALANDHAR": [
        "PHILLAUR", "NAKODAR", "SHAHKOT", "KARTARPUR",
        "JALANDHAR WEST", "JALANDHAR CENTRAL", "JALANDHAR NORTH",
        "JALANDHAR CANTT", "ADAMPUR",
    ],
    "HOSHIARPUR": [
        "MUKERIAN", "DASUYA", "URMAR", "SHAM CHAURASI",
        "HOSHIARPUR", "CHABBEWAL", "GARHSHANKAR", "BANGA", "PHAGWARA",
    ],
    "ANANDPUR SAHIB": [
        "BALACHAUR", "NAWAN SHAHR", "ANANDPUR SAHIB", "RUPNAGAR",
        "CHAMKAUR SAHIB", "KHARAR", "S.A.S. NAGAR", "BASSI PATHANA", "BANUR",
    ],
    "LUDHIANA": [
        "LUDHIANA EAST", "LUDHIANA SOUTH", "ATAM NAGAR", "LUDHIANA CENTRAL",
        "LUDHIANA WEST", "LUDHIANA NORTH", "GILL", "DAKHA", "JAGRAON",
    ],
    "FATEHGARH SAHIB": [
        "FATEHGARH SAHIB", "AMLOH", "KHANNA", "SAMRALA", "SAHNEWAL",
        "PAYAL", "RAIKOT", "BHADAUR", "DHURI",
    ],
    "FARIDKOT": [
        "NIHAL SINGH WALA", "BAGHA PURANA", "MOGA", "DHARAMKOT",
        "FARIDKOT", "KOTKAPURA", "JAITU", "RAMPURA PHUL", "FAZILKA",
    ],
    "FIROZPUR": [
        "FIROZPUR CITY", "FIROZPUR RURAL", "GURU HAR SAHAI", "JALALABAD",
        "MALOUT", "MUKTSAR", "GIDDERBAHA", "BALLUANA", "ABOHAR",
    ],
    "BATHINDA": [
        "LAMBI", "BHUCHO MANDI", "BATHINDA URBAN", "BATHINDA RURAL",
        "TALWANDI SABO", "MAUR", "MANSA", "SARDULGARH", "BUDHLADA",
    ],
    "SANGRUR": [
        "LEHRA", "DIRBA", "SUNAM", "BARNALA", "MEHAL KALAN",
        "MALERKOTLA", "AMARGARH", "SANGRUR", "DHURI",
    ],
    "PATIALA": [
        "NABHA", "PATIALA RURAL", "RAJPURA", "DERA BASSI", "GHANAUR",
        "SANOUR", "PATIALA", "SAMANA", "SHUTRANA",
    ],
}


def _norm(name: str) -> str:
    if not name:
        return ""
    s = re.sub(r"\(SC\)|\(ST\)", "", name).upper().strip()
    s = re.sub(r"\s+", " ", s)
    return s


# Pre-compute the reverse index: AC name -> LS PC name.
AC_TO_LS_PC: dict[str, str] = {}
for ls_pc, acs in LS_PC_TO_ACS.items():
    for ac in acs:
        AC_TO_LS_PC[_norm(ac)] = ls_pc


def ls_pc_for_ac(ac_name: str) -> str | None:
    """Return the Lok Sabha PC for a given assembly constituency name, or None."""
    return AC_TO_LS_PC.get(_norm(ac_name))
