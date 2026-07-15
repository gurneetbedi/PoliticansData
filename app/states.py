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
        {"year": 2025, "slug": "Bihar2025"},
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
    key="uttarakhand", name="Uttarakhand", code="UK", zone="North",
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
    key="chhattisgarh", name="Chhattisgarh", code="CG", zone="East",
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
        {"year": 2026, "slug": "Assam2026"},
        {"year": 2021, "slug": "assam2021"},
        {"year": 2016, "slug": "assam2016"},
        {"year": 2011, "slug": "assam2011"},
    ],
    ls_pcs={
        "KARIMGANJ", "SILCHAR", "AUTONOMOUS DISTRICT", "DHUBRI", "KOKRAJHAR",
        "BARPETA", "GAUHATI", "MANGALDOI", "TEZPUR", "NOWGONG", "KALIABOR",
        "JORHAT", "DIBRUGARH", "LAKHIMPUR",
    },
)

KERALA = StateConfig(
    key="kerala", name="Kerala", code="KL", zone="South",
    assembly_cycles=[
        {"year": 2026, "slug": "Kerala2026"},
        {"year": 2021, "slug": "kerala2021"},
        {"year": 2016, "slug": "kerala2016"},
        {"year": 2011, "slug": "kerala2011"},
    ],
    ls_pcs = {
    "KASARAGOD", "KANNUR", "VADAKARA", "WAYANAD", "KOZHIKODE",
    "MALAPPURAM", "PONNANI", "PALAKKAD", "ALATHUR", "THRISSUR",
    "CHALAKUDY", "ERNAKULAM", "IDUKKI", "KOTTAYAM", "ALAPPUZHA",
    "MAVELIKKARA", "PATHANAMTHITTA", "KOLLAM", "ATTINGAL", "THIRUVANANTHAPURAM",
    },
)


GUJARAT = StateConfig(
    key="gujarat", name="Gujarat", code="GJ", zone="West",
    assembly_cycles=[
        {"year": 2022, "slug": "gujarat2022"},
        {"year": 2017, "slug": "gujarat2017"},
        {"year": 2012, "slug": "gujarat2012"},
        {"year": 2007, "slug": "gujarat2007"},
    ],
    ls_pcs = {
        "KACHCHH", "BANASKANTHA", "PATAN", "MAHESANA", "SABARKANTHA", "GANDHINAGAR", "AHMEDABAD EAST", "AHMEDABAD WEST", "SURENDRANAGAR",
        "RAJKOT", "PORBANDAR", "JAMNAGAR", "JUNAGADH", "AMRELI", "BHAVNAGAR", "ANAND", "KHEDA", "PANCHMAHAL", "DAHOD",
        "VADODARA", "CHHOTA UDAIPUR", "BHARUCH", "BARDOLI", "SURAT", "NAVSARI", "VALSAD",
        },
)



TAMIL_NADU = StateConfig(
    key="tamil_nadu", name="Tamil Nadu", code="TN", zone="South",
    assembly_cycles=[
        {"year": 2026, "slug": "tamilnadu2026"},
        {"year": 2021, "slug": "tamilnadu2021"},
        {"year": 2016, "slug": "tamilnadu2016"},
        {"year": 2011, "slug": "tamilnadu2011"},
    ],
    ls_pcs={
        "THIRUVALLUR", "CHENNAI NORTH", "CHENNAI SOUTH", "CHENNAI CENTRAL",
        "SRIPERUMBUDUR", "KANCHEEPURAM", "ARAKKONAM", "VELLORE",
        "KRISHNAGIRI", "DHARMAPURI", "TIRUVANNAMALAI", "ARANI",
        "VILUPPURAM", "KALLAKURICHI", "SALEM", "NAMAKKAL",
        "ERODE", "TIRUPPUR", "NILGIRIS", "COIMBATORE",
        "POLLACHI", "DINDIGUL", "KARUR", "TIRUCHIRAPPALLI",
        "PERAMBALUR", "CUDDALORE", "CHIDAMBARAM", "MAYILADUTHURAI",
        "NAGAPATTINAM", "THANJAVUR", "SIVAGANGA", "MADURAI",
        "THENI", "VIRUDHUNAGAR", "RAMANATHAPURAM", "THOOTHUKKUDI",
        "TENKASI", "TIRUNELVELI", "KANNIYAKUMARI",
    },
)



WEST_BENGAL = StateConfig(
    key="west_bengal", name="West Bengal", code="WB", zone="East",
    assembly_cycles=[
        {"year": 2026, "slug": "westbengal2026"},
        {"year": 2021, "slug": "westbengal2021"},
        {"year": 2016, "slug": "westbengal2016"},
        {"year": 2011, "slug": "westbengal2011"},
    ],
    ls_pcs={
        "COOCH BEHAR", "ALIPURDUARS", "JALPAIGURI", "DARJEELING",
        "RAIGANJ", "BALURGHAT", "MALDAHA UTTAR", "MALDAHA DAKSHIN",
        "JANGIPUR", "MURSHIDABAD", "BAHARAMPUR", "KRISHNANAGAR",
        "RANAGHAT", "BANGAON", "BARRACKPORE", "DUM DUM",
        "BARASAT", "BASIRHAT", "JAYNAGAR", "MATHURAPUR",
        "DIAMOND HARBOUR", "JADAVPUR", "KOLKATA DAKSHIN", "KOLKATA UTTAR",
        "HOWRAH", "ULUBERIA", "SREERAMPUR", "HOOGHLY",
        "ARAMBAGH", "TAMLUK", "KANTHI", "GHATAL",
        "JHARGRAM", "MEDINIPUR", "PURULIA", "BANKURA",
        "BISHNUPUR", "BARDHAMAN PURBA", "BARDHAMAN-DURGAPUR",
        "ASANSOL", "BOLPUR", "BIRBHUM",
    },
)


ODISHA = StateConfig(
    key="odisha", name="Odisha", code="OD", zone="East",
    assembly_cycles=[
        {"year": 2024, "slug": "odisha2024"},
        {"year": 2019, "slug": "odisha2019"},
        {"year": 2014, "slug": "odisha2014"},
        {"year": 2009, "slug": "odisha2009"},
    ],
    ls_pcs={
        "BARGARH", "SUNDARGARH", "SAMBALPUR", "KEONJHAR",
        "MAYURBHANJ", "BALASORE", "BHADRAK", "JAJPUR",
        "DHENKANAL", "BOLANGIR", "KALAHANDI", "NABARANGPUR",
        "KANDHAMAL", "CUTTACK", "KENDRAPARA", "JAGATSINGHPUR",
        "PURI", "BHUBANESWAR", "ASKA", "BERHAMPUR",
        "KORAPUT",
    },
)


ANDHRA_PRADESH = StateConfig(
    key="andhra_pradesh", name="Andhra Pradesh", code="AP", zone="South",
    assembly_cycles=[
        {"year": 2024, "slug": "andhrapradesh2024"},
        {"year": 2019, "slug": "andhrapradesh2019"},
        {"year": 2014, "slug": "andhrapradesh2014"},
    ],
    ls_pcs={
        "ARAKU", "SRIKAKULAM", "VIZIANAGARAM", "VISAKHAPATNAM",
        "ANAKAPALLI", "KAKINADA", "AMALAPURAM", "RAJAHMUNDRY",
        "NARASAPURAM", "ELURU", "MACHILIPATNAM", "VIJAYAWADA",
        "GUNTUR", "NARASARAOPET", "BAPATLA", "ONGOLE",
        "NANDYAL", "KURNOOL", "ANANTAPUR", "HINDUPUR",
        "KADAPA", "NELLORE", "TIRUPATI", "RAJAMPET",
        "CHITTOOR",
    },
)


RAJASTHAN = StateConfig(
    key="rajasthan", name="Rajasthan", code="RJ", zone="North",
    assembly_cycles=[
        {"year": 2023, "slug": "rajasthan2023"},
        {"year": 2018, "slug": "rajasthan2018"},
        {"year": 2013, "slug": "rajasthan2013"},
        {"year": 2008, "slug": "rajasthan2008"},
    ],
    ls_pcs={
        "GANGANAGAR", "BIKANER", "CHURU", "JHUNJHUNU",
        "SIKAR", "JAIPUR RURAL", "JAIPUR", "ALWAR",
        "BHARATPUR", "KARAULI-DHOLPUR", "DAUSA", "TONK-SAWAI MADHOPUR",
        "AJMER", "NAGAUR", "PALI", "JODHPUR",
        "BARMER", "JALORE", "UDAIPUR", "BANSWARA",
        "CHITTORGARH", "RAJSAMAND", "BHILWARA", "KOTA",
        "JHALAWAR-BARAN",
    },
)


MADHYA_PRADESH = StateConfig(
    key="madhya-pradesh", name="Madhya Pradesh", code="MP", zone="Central",
    assembly_cycles=[
        {"year": 2023, "slug": "madhyapradesh2023"},
        {"year": 2018, "slug": "madhyapradesh2018"},
        {"year": 2013, "slug": "madhyapradesh2013"},
        {"year": 2008, "slug": "madhyapradesh2008"},
    ],
    ls_pcs={
        "MORENA", "BHIND", "GWALIOR", "GUNA",
        "SAGAR", "TIKAMGARH", "DAMOH", "KHAJURAHO",
        "SATNA", "REWA", "SIDHI", "SHAHDOL",
        "JABALPUR", "MANDLA", "BALAGHAT", "CHHINDWARA",
        "HOSHANGABAD", "VIDISHA", "BHOPAL", "RAJGARH",
        "DEWAS", "UJJAIN", "MANDSAUR", "RATLAM",
        "DHAR", "INDORE", "KHARGONE", "KHANDWA",
        "BETUL",
    },
)


KARNATAKA = StateConfig(
    key="karnataka", name="Karnataka", code="KA", zone="South",
    assembly_cycles=[
        {"year": 2023, "slug": "karnataka2023"},
        {"year": 2018, "slug": "karnataka2018"},
        {"year": 2013, "slug": "karnataka2013"},
        {"year": 2008, "slug": "karnataka2008"},
    ],
    ls_pcs={
        "CHIKKODI", "BELGAUM", "BAGALKOT", "BIJAPUR",
        "KALABURAGI", "RAICHUR", "BIDAR", "KOPPAL",
        "BELLARY", "HAVERI", "DHARWAD", "UTTARA KANNADA",
        "DAVANAGERE", "SHIMOGA", "UDUPI CHIKMAGALUR", "HASSAN",
        "DAKSHINA KANNADA", "CHITRADURGA", "TUMKUR", "MANDYA",
        "MYSORE", "CHAMARAJANAGAR", "BANGALORE RURAL", "BANGALORE NORTH",
        "BANGALORE CENTRAL", "BANGALORE SOUTH", "CHIKKBALLAPUR", "KOLAR",
    },
)

# Maharashtra — 288 assembly seats, 48 Lok Sabha seats.
# 2024 cycle: Nov 2024 (Mahayuti landslide).
MAHARASHTRA = StateConfig(
    key="maharashtra", name="Maharashtra", code="MH", zone="West",
    assembly_cycles=[
        {"year": 2024, "slug": "Maharashtra2024"},
        {"year": 2019, "slug": "maharashtra2019"},
        {"year": 2014, "slug": "maharashtra2014"},
        {"year": 2009, "slug": "maharashtra2009"},
    ],
    ls_pcs={
        "NANDURBAR", "DHULE", "JALGAON", "RAVER", "BULDHANA",
        "AKOLA", "AMRAVATI", "WARDHA", "RAMTEK", "NAGPUR",
        "BHANDARA-GONDIYA", "GADCHIROLI-CHIMUR", "CHANDRAPUR", "YAVATMAL-WASHIM",
        "HINGOLI", "NANDED", "PARBHANI", "JALNA", "AURANGABAD",
        "DINDORI", "NASHIK", "PALGHAR", "BHIWANDI", "KALYAN",
        "THANE", "MUMBAI NORTH", "MUMBAI NORTH WEST", "MUMBAI NORTH EAST",
        "MUMBAI NORTH CENTRAL", "MUMBAI SOUTH CENTRAL", "MUMBAI SOUTH",
        "RAIGAD", "MAVAL", "PUNE", "BARAMATI", "SHIRUR",
        "AHMEDNAGAR", "SHIRDI", "BEED", "OSMANABAD", "LATUR",
        "SOLAPUR", "MADHA", "SANGLI", "SATARA", "RATNAGIRI-SINDHUDURG",
        "KOLHAPUR", "HATKANANGLE",
    },
)

# Uttar Pradesh — 403 assembly seats, 80 Lok Sabha seats (largest state).
# 2022 cycle: BJP re-elected under Yogi Adityanath.
UTTAR_PRADESH = StateConfig(
    key="uttarpradesh", name="Uttar Pradesh", code="UP", zone="North",
    assembly_cycles=[
        {"year": 2022, "slug": "UttarPradesh2022"},
        {"year": 2017, "slug": "uttarpradesh2017"},
        {"year": 2012, "slug": "uttarpradesh2012"},
        {"year": 2007, "slug": "uttarpradesh2007"},
    ],
    # 80 Lok Sabha PCs — kept partial for brevity; extend as needed.
    ls_pcs={
        "SAHARANPUR", "KAIRANA", "MUZAFFARNAGAR", "BIJNOR", "NAGINA",
        "MORADABAD", "RAMPUR", "SAMBHAL", "AMROHA", "MEERUT",
        "BAGHPAT", "GHAZIABAD", "GAUTAM BUDDHA NAGAR", "BULANDSHAHR",
        "ALIGARH", "HATHRAS", "MATHURA", "AGRA", "FATEHPUR SIKRI",
        "FIROZABAD", "MAINPURI", "ETAWAH", "KANNAUJ", "KANPUR",
        "AKBARPUR", "JALAUN", "JHANSI", "HAMIRPUR", "BANDA",
        "FATEHPUR", "KAUSHAMBI", "PHULPUR", "ALLAHABAD", "BARABANKI",
        "FAIZABAD", "AMBEDKAR NAGAR", "BAHRAICH", "KAISERGANJ",
        "SHRAWASTI", "GONDA", "DOMARIYAGANJ", "BASTI", "SANT KABIR NAGAR",
        "MAHARAJGANJ", "GORAKHPUR", "KUSHI NAGAR", "DEORIA", "BANSGAON",
        "LALGANJ", "AZAMGARH", "GHOSI", "SALEMPUR", "BALLIA",
        "JAUNPUR", "MACHHLISHAHR", "GHAZIPUR", "CHANDAULI", "VARANASI",
        "BHADOHI", "MIRZAPUR", "ROBERTSGANJ", "PILIBHIT", "SHAHJAHANPUR",
        "KHERI", "DHAURAHRA", "SITAPUR", "HARDOI", "MISRIKH",
        "UNNAO", "MOHANLALGANJ", "LUCKNOW", "RAE BARELI", "AMETHI",
        "SULTANPUR", "PRATAPGARH", "FARRUKHABAD", "ETAH", "BADAUN",
        "AONLA", "BAREILLY",
    },
)
# Active states — must have ECI affidavit data loaded for the listed
# cycles before being added here. The other StateConfig objects above
# are preserved in source so we can re-enable them as we backfill ECI
# data state-by-state. Adding a state back is a one-line edit in this dict.
ALL_STATES: dict[str, StateConfig] = {
    "delhi":        DELHI,        # 2020 + 2025 cycles (current)
    "punjab":       PUNJAB,       # 2022 cycle (current — next Feb 2027)
    "puducherry":   PUDUCHERRY,   # 2021 cycle (current — next 2026)
    "goa":          GOA,          # 2022 cycle (current — next 2027)
    "mizoram":      MIZORAM,      # 2023 cycle (current — next 2028)
    "nagaland":     NAGALAND,     # 2023 cycle (current — next 2028)
    "himachal":     HIMACHAL,     # 2022 cycle (current — next 2027)
    "arunachal":    ARUNACHAL,    # 2024 cycle (current — next 2029)
    "sikkim":       SIKKIM,       # 2019 + 2024 cycles (current — cross-cycle potential)
    "haryana":      HARYANA,      # 2019 + 2024 cycles (current — cross-cycle potential)
    "manipur":      MANIPUR,      # 2022 cycle — Tier 1 (smallest-to-biggest queue)
    "tripura":      TRIPURA,      # 2023 cycle — Tier 1
    "meghalaya":    MEGHALAYA,    # 2023 cycle — Tier 1
    "uttarakhand":  UTTARAKHAND,  # 2022 cycle — Tier 2 (hill state)
    "jharkhand":    JHARKHAND,    # 2024 cycle — Tier 2
    "jk":           JK,           # 2024 cycle — historic first post-370 election
    "chhattisgarh": CHHATTISGARH, # 2023 cycle — Tier 2
    "telangana":    TELANGANA,    # 2023 cycle — Tier 2
    "assam":        ASSAM,        # 2026 cycle — Tier 2 (Northeast)
    "kerala":       KERALA,       # 2026 cycle — Tier 2 (South)
    "gujarat":      GUJARAT,      # 2022 cycle — Tier 2 (West)
    "tamil_nadu":   TAMIL_NADU,   # 2026 cycle — Tier 2 (South)
    "westbengal":   WEST_BENGAL,   # 2026 cycle — Tier 2 (South)
    "odisha":       ODISHA,       # 2024 cycle — Tier 2 (East)
    "andhra_pradesh": ANDHRA_PRADESH, # 2024 cycle — Tier 2 (South)
    "rajasthan":    RAJASTHAN,    # 2023 cycle — Tier 2 (North)
    "madhya-pradesh": MADHYA_PRADESH, # 2023 cycle — Tier 2 (Central)
    "karnataka":     KARNATAKA,     # 2023 cycle — Tier 2 (South)
    "bihar":         BIHAR,         # 2025 cycle — Tier 3 (East, 243 seats)
    "maharashtra":   MAHARASHTRA,   # 2024 cycle — Tier 3 (West, 288 seats)
    "uttarpradesh":  UTTAR_PRADESH, # 2022 cycle — Tier 3 (North, 403 seats — largest)
}

# States we ingested but hid pending latest-cycle data. Empty right now —
# move a state here when its loaded cycle is no longer the most recent one
# available on the ECI portal.
_HIDDEN_STATES: dict[str, StateConfig] = {}

# Internal-only — used by scripts/migrate scaffolding. Not surfaced in the UI.
_ALL_STATES_HISTORICAL: dict[str, StateConfig] = {
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
    "maharashtra":  MAHARASHTRA,
    "uttarpradesh": UTTAR_PRADESH,
}


def get_state(key: str) -> StateConfig:
    """Lookup helper — raises KeyError for unknown state."""
    return ALL_STATES[key.lower()]


# ──────────────────────────────────────────────────────────────────────────
# Data-driven visibility
# ──────────────────────────────────────────────────────────────────────────
# A state is "visible" if the DB has at least one flagged winner (won=1)
# for its latest declared assembly cycle. States registered in ALL_STATES
# but not yet loaded (winners = 0) are auto-hidden. This means we can
# register a state's config the moment we have its Statistical Report,
# and it stays hidden until the affidavit pipeline lands winners in the
# DB — no manual toggling here required.
#
# Cached at module import (site restart re-computes). The query is
# lightweight (one aggregation on election_appearances).

_VISIBLE_STATES_CACHE: dict[str, StateConfig] | None = None


def _compute_visible_states() -> dict[str, StateConfig]:
    """Return ALL_STATES minus any state with 0 loaded winners for its
    latest cycle. Fails soft: if the DB isn't reachable (e.g. during a
    fresh setup before migrations), returns ALL_STATES unchanged.
    """
    import sqlite3
    from pathlib import Path

    db_path = Path(__file__).resolve().parent.parent / "lokvani.db"
    if not db_path.exists():
        return dict(ALL_STATES)

    try:
        con = sqlite3.connect(str(db_path))
        cur = con.cursor()
        loaded_states = set()
        for key, cfg in ALL_STATES.items():
            if not cfg.assembly_cycles:
                continue
            latest_year = cfg.assembly_cycles[0]["year"]
            wins = cur.execute("""
                SELECT COUNT(*) FROM election_appearances ea
                JOIN elections e ON ea.election_id = e.id
                JOIN states s    ON e.state_id     = s.id
                WHERE s.name = ? AND e.year = ? AND ea.won = 1
            """, (cfg.name, latest_year)).fetchone()[0]
            if wins > 0:
                loaded_states.add(key)
        con.close()
        return {k: v for k, v in ALL_STATES.items() if k in loaded_states}
    except Exception:
        # If anything goes wrong (schema missing, DB locked, etc.) fall
        # back to showing everything — better a crowded map than a blank
        # one during setup.
        return dict(ALL_STATES)


def visible_states() -> dict[str, StateConfig]:
    """States that should be surfaced in the UI (map, dropdown, listings).

    Auto-derived from the DB: only states with loaded winners appear.
    Callers should prefer this over `ALL_STATES` for any user-facing render.
    Result is cached on first call — restart the server to pick up newly
    loaded states.
    """
    global _VISIBLE_STATES_CACHE
    if _VISIBLE_STATES_CACHE is None:
        _VISIBLE_STATES_CACHE = _compute_visible_states()
    return _VISIBLE_STATES_CACHE


def clear_visibility_cache() -> None:
    """Force `visible_states()` to re-query on next call. Useful during
    development after running a fresh load, or if you add an admin
    'refresh visibility' button later."""
    global _VISIBLE_STATES_CACHE
    _VISIBLE_STATES_CACHE = None
