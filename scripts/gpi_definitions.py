"""GPI Phase 1 reference data — pillars, sources, indicators.

Single source of truth for what the GPI measures. `scripts/gpi_seed.py` reads
this file and upserts the rows into `lokvani.db`. The XLSX spec (docs/gpi/
GPI_Punjab_Phase1_Spec.xlsx) is the human-readable mirror of this file —
any change here should be reflected there and vice versa.

Structure:
    PILLARS   — 6 pillar dicts (code, name, description, weight, order)
    SOURCES   — 22 source dicts (code, name, publisher, url, format, cadence)
    INDICATORS — 36 indicator dicts (code, pillar_code, source_code, name,
                 description, unit, direction, cadence, order)
"""

PILLARS = [
    {"code": "economy",         "name": "Economy",
     "description": "Aggregate economic health — growth, employment, income.",
     "default_weight": 1/8, "display_order": 1},
    {"code": "public_finance",  "name": "Public Finance",
     "description": "Fiscal discipline, debt sustainability, spending quality.",
     "default_weight": 1/8, "display_order": 2},
    {"code": "education",       "name": "Education",
     "description": "Access, quality, and outcomes across schools.",
     "default_weight": 1/8, "display_order": 3},
    {"code": "healthcare",      "name": "Healthcare",
     "description": "Health outcomes and system capacity.",
     "default_weight": 1/8, "display_order": 4},
    {"code": "infrastructure",  "name": "Infrastructure",
     "description": "Physical and digital infrastructure delivery.",
     "default_weight": 1/8, "display_order": 5},
    {"code": "law_and_order",   "name": "Law & Order",
     "description": "Public safety, criminal justice performance, policing capacity.",
     "default_weight": 1/8, "display_order": 6},
    {"code": "governance",      "name": "Governance & Accountability",
     "description": "Audit compliance, PAC follow-through, transparency in public financial management.",
     "default_weight": 1/8, "display_order": 7},
    {"code": "efficiency",      "name": "Fiscal Efficiency",
     "description": "How well the state converts economic activity into fiscal capacity — "
                     "revenue collection efficiency, tax productivity.",
     "default_weight": 1/8, "display_order": 8},
]

SOURCES = [
    {"code": "S01", "name": "Handbook of Statistics on Indian States", "publisher": "RBI",
     "url": "https://www.rbi.org.in/scripts/AnnualPublications.aspx?head=Handbook%20of%20Statistics%20on%20Indian%20States",
     "format": "PDF + XLSX", "refresh_cadence": "Annual"},
    {"code": "S02", "name": "State Finances: A Study of Budgets", "publisher": "RBI",
     "url": "https://www.rbi.org.in/scripts/AnnualPublications.aspx?head=State+Finances+%3a+A+Study+of+Budgets",
     "format": "PDF", "refresh_cadence": "Annual"},
    {"code": "S03", "name": "Periodic Labour Force Survey (PLFS)", "publisher": "MOSPI",
     "url": "https://www.mospi.gov.in/publication/annual-report-periodic-labour-force-survey-plfs",
     "format": "PDF + Unit-level", "refresh_cadence": "Annual"},
    {"code": "S04", "name": "National Multidimensional Poverty Index", "publisher": "NITI Aayog",
     "url": "https://www.niti.gov.in/reports-multidimensional-poverty",
     "format": "PDF + XLSX", "refresh_cadence": "Sporadic (2015-16, 2019-21)"},
    {"code": "S05", "name": "Udyam Registration Portal", "publisher": "MSME Ministry",
     "url": "https://udyamregistration.gov.in/",
     "format": "Dashboard", "refresh_cadence": "Real-time"},
    {"code": "S06", "name": "Punjab State Budget", "publisher": "Punjab Finance Dept",
     "url": "https://finance.punjab.gov.in/budget",
     "format": "PDF", "refresh_cadence": "Annual"},
    {"code": "S07", "name": "UDISE+ Report Dashboard", "publisher": "MoE (GoI)",
     "url": "https://udiseplus.gov.in/",
     "format": "Dashboard + PDF", "refresh_cadence": "Annual (1yr lag)"},
    {"code": "S08", "name": "ASER (Annual Status of Education Report)", "publisher": "ASER Centre / Pratham",
     "url": "https://asercentre.org/",
     "format": "PDF", "refresh_cadence": "Sporadic (2018, 2022, 2024)"},
    {"code": "S09", "name": "SRS Bulletin (Sample Registration System)", "publisher": "Office of RGI",
     "url": "https://censusindia.gov.in/vital_statistics/SRS_Bulletins/",
     "format": "PDF", "refresh_cadence": "Annual"},
    {"code": "S10", "name": "HMIS (Health Management Information System)", "publisher": "MoHFW",
     "url": "https://hmis.nhp.gov.in/",
     "format": "Dashboard + XLSX", "refresh_cadence": "Monthly"},
    {"code": "S11", "name": "National Family Health Survey (NFHS-5)", "publisher": "IIPS / MoHFW",
     "url": "http://rchiips.org/nfhs/",
     "format": "PDF + Unit-level", "refresh_cadence": "Sporadic (~5 years)"},
    {"code": "S12", "name": "National Health Accounts", "publisher": "NHSRC",
     "url": "https://nhsrcindia.org/national-health-accounts-records",
     "format": "PDF", "refresh_cadence": "Annual (2yr lag)"},
    {"code": "S13", "name": "Rural Health Statistics", "publisher": "MoHFW",
     "url": "https://main.mohfw.gov.in/newshighlights/rural-health-statistics",
     "format": "PDF + XLSX", "refresh_cadence": "Annual"},
    {"code": "S14", "name": "Basic Road Statistics of India", "publisher": "MoRTH",
     "url": "https://morth.nic.in/road-transport-year-book",
     "format": "PDF", "refresh_cadence": "Annual (2yr lag)"},
    {"code": "S15", "name": "PSPCL Annual Report", "publisher": "Punjab State Power Corp",
     "url": "https://www.pspcl.in/",
     "format": "PDF", "refresh_cadence": "Annual"},
    {"code": "S16", "name": "JJM Dashboard", "publisher": "Jal Jeevan Mission (Jal Shakti)",
     "url": "https://ejalshakti.gov.in/JJM/JJMReports/",
     "format": "Dashboard", "refresh_cadence": "Real-time"},
    {"code": "S17", "name": "TRAI Performance Indicator Reports", "publisher": "TRAI",
     "url": "https://www.trai.gov.in/release-publication/reports/performance-indicators-reports",
     "format": "PDF", "refresh_cadence": "Quarterly"},
    {"code": "S18", "name": "Swachh Bharat Mission (Grameen) Dashboard", "publisher": "Jal Shakti",
     "url": "https://sbm.gov.in/sbmdashboard/",
     "format": "Dashboard", "refresh_cadence": "Real-time"},
    {"code": "S19", "name": "PMGSY OMMAS", "publisher": "Ministry of Rural Development",
     "url": "https://omms.nic.in/",
     "format": "Dashboard", "refresh_cadence": "Real-time"},
    {"code": "S20", "name": "Crime in India Report", "publisher": "NCRB (MHA)",
     "url": "https://www.ncrb.gov.in/crime-in-india.html",
     "format": "PDF + XLSX", "refresh_cadence": "Annual (2yr lag)"},
    {"code": "S21", "name": "Data on Police Organisations", "publisher": "BPRD (MHA)",
     "url": "https://bprd.nic.in/",
     "format": "PDF", "refresh_cadence": "Annual"},
    {"code": "S22", "name": "National Judicial Data Grid", "publisher": "eCourts (Supreme Court)",
     "url": "https://njdg.ecourts.gov.in/",
     "format": "Dashboard", "refresh_cadence": "Real-time"},
]

# Direction is either "higher_better" or "lower_better".
# `order` positions the indicator within its pillar for UI display.
INDICATORS = [
    # ── Economy ────────────────────────────────────────────────────────────
    {"code": "E01", "pillar_code": "economy", "source_code": "S01",
     "name": "GSDP growth rate (real)",
     "description": "Year-on-year real growth of Gross State Domestic Product.",
     "unit": "% annual", "direction": "higher_better",
     "cadence": "annual", "order": 1,
     "notes": "Base year 2011-12; watch for base revisions."},
    {"code": "E02", "pillar_code": "economy", "source_code": "S01",
     "name": "Per-capita GSDP",
     "description": "GSDP at current prices divided by population estimate.",
     "unit": "INR/capita", "direction": "higher_better",
     "cadence": "annual", "order": 2,
     "notes": "Population interpolated between census years."},
    {"code": "E03", "pillar_code": "economy", "source_code": "S03",
     "name": "Unemployment rate (PLFS)",
     "description": "Usual-status unemployment rate, all ages.",
     "unit": "%", "direction": "lower_better",
     "cadence": "annual", "order": 3,
     "notes": "PLFS 2017-18 onwards."},
    {"code": "E04", "pillar_code": "economy", "source_code": "S03",
     "name": "Labour Force Participation Rate",
     "description": "% of working-age population in labour force (usual status).",
     "unit": "%", "direction": "higher_better",
     "cadence": "annual", "order": 4,
     "notes": "Punjab historically low; gender-disaggregated data valuable."},
    {"code": "E05", "pillar_code": "economy", "source_code": "S04",
     "name": "Multidimensional Poverty Index headcount",
     "description": "% of population classified multidimensionally poor.",
     "unit": "%", "direction": "lower_better",
     "cadence": "sporadic", "order": 5,
     "notes": "Only 2 data points (2015-16, 2019-21); carry-forward w/ flag."},
    {"code": "E06", "pillar_code": "economy", "source_code": "S05",
     "name": "MSME registrations per 100k",
     "description": "Udyam registrations per 100k population, cumulative.",
     "unit": "count/100k", "direction": "higher_better",
     "cadence": "annual", "order": 6,
     "notes": "Udyam launched July 2020; UAM before."},

    # ── Public Finance ─────────────────────────────────────────────────────
    {"code": "F01", "pillar_code": "public_finance", "source_code": "S02",
     "name": "Fiscal deficit / GSDP",
     "description": "Gross fiscal deficit as % of GSDP.",
     "unit": "%", "direction": "lower_better",
     "cadence": "annual", "order": 1,
     "notes": "Punjab FRBM target 3%; actuals usually higher."},
    {"code": "F02", "pillar_code": "public_finance", "source_code": "S02",
     "name": "Outstanding debt / GSDP",
     "description": "Total outstanding liabilities as % of GSDP.",
     "unit": "%", "direction": "lower_better",
     "cadence": "annual", "order": 2,
     "notes": "Punjab ~48% FY23 vs 15th FC recommended 20%. Key pillar signal."},
    {"code": "F03", "pillar_code": "public_finance", "source_code": "S06",
     "name": "Own tax revenue growth",
     "description": "YoY growth in state's own tax revenue.",
     "unit": "% annual", "direction": "higher_better",
     "cadence": "annual", "order": 3,
     "notes": "GST era begins FY18 — pre-post comparability caveat."},
    {"code": "F04", "pillar_code": "public_finance", "source_code": "S02",
     "name": "Capital expenditure share",
     "description": "Capex as % of total expenditure.",
     "unit": "%", "direction": "higher_better",
     "cadence": "annual", "order": 4,
     "notes": "Higher capex = investment in future capacity."},
    {"code": "F05", "pillar_code": "public_finance", "source_code": "S02",
     "name": "Revenue deficit / GSDP",
     "description": "Revenue deficit as % of GSDP.",
     "unit": "%", "direction": "lower_better",
     "cadence": "annual", "order": 5,
     "notes": "Structural gap indicator."},
    {"code": "F06", "pillar_code": "public_finance", "source_code": "S02",
     "name": "Interest payments / revenue receipts",
     "description": "Debt-servicing burden as share of revenue receipts.",
     "unit": "%", "direction": "lower_better",
     "cadence": "annual", "order": 6,
     "notes": "Crowds out capex. Punjab historically 20%+."},

    # ── Education ──────────────────────────────────────────────────────────
    {"code": "ED01", "pillar_code": "education", "source_code": "S08",
     "name": "ASER Grade 5 reading proficiency",
     "description": "% Grade 5 rural children who can read Grade 2 text.",
     "unit": "%", "direction": "higher_better",
     "cadence": "sporadic", "order": 1,
     "notes": "Rural only; combine with NAS urban for full picture."},
    {"code": "ED02", "pillar_code": "education", "source_code": "S07",
     "name": "Gross Enrolment Ratio (secondary)",
     "description": "GER at secondary level (Grades 9-10).",
     "unit": "%", "direction": "higher_better",
     "cadence": "annual", "order": 2,
     "notes": "Punjab historically strong; ceiling effect possible."},
    {"code": "ED03", "pillar_code": "education", "source_code": "S07",
     "name": "Drop-out rate (secondary)",
     "description": "Cohort drop-out rate at secondary level.",
     "unit": "%", "direction": "lower_better",
     "cadence": "annual", "order": 3},
    {"code": "ED04", "pillar_code": "education", "source_code": "S07",
     "name": "Pupil-teacher ratio (secondary)",
     "description": "Students per teacher, secondary level.",
     "unit": "ratio", "direction": "lower_better",
     "cadence": "annual", "order": 4,
     "notes": "RTE norm: 30 (elementary), 35 (secondary)."},
    {"code": "ED05", "pillar_code": "education", "source_code": "S07",
     "name": "Teacher vacancy rate",
     "description": "% of sanctioned teacher posts vacant.",
     "unit": "%", "direction": "lower_better",
     "cadence": "annual", "order": 5,
     "notes": "Vacancy data sometimes only in state assembly reply."},
    {"code": "ED06", "pillar_code": "education", "source_code": "S07",
     "name": "Schools with all basic amenities",
     "description": "% of schools with electricity + water + toilets + boundary wall.",
     "unit": "%", "direction": "higher_better",
     "cadence": "annual", "order": 6,
     "notes": "Composite of 4-6 amenities per UDISE definition."},

    # ── Healthcare ─────────────────────────────────────────────────────────
    {"code": "H01", "pillar_code": "healthcare", "source_code": "S09",
     "name": "Infant Mortality Rate",
     "description": "Infant deaths per 1,000 live births.",
     "unit": "per 1000", "direction": "lower_better",
     "cadence": "annual", "order": 1,
     "notes": "SRS annual; NFHS periodic."},
    {"code": "H02", "pillar_code": "healthcare", "source_code": "S10",
     "name": "Institutional deliveries",
     "description": "% of births in health facilities.",
     "unit": "%", "direction": "higher_better",
     "cadence": "annual", "order": 2,
     "notes": "NFHS-5 baseline; HMIS annual."},
    {"code": "H03", "pillar_code": "healthcare", "source_code": "S11",
     "name": "Full immunization coverage (12-23 mo)",
     "description": "% children 12-23mo fully immunized (BCG+3DPT+3Polio+Measles).",
     "unit": "%", "direction": "higher_better",
     "cadence": "sporadic", "order": 3,
     "notes": "NFHS-4 (2015-16) & NFHS-5 (2019-21); HMIS interim proxy."},
    {"code": "H04", "pillar_code": "healthcare", "source_code": "S12",
     "name": "Out-of-pocket health expenditure share",
     "description": "OOP as % of total health expenditure.",
     "unit": "%", "direction": "lower_better",
     "cadence": "annual", "order": 4,
     "notes": "NHA released with 2-year lag."},
    {"code": "H05", "pillar_code": "healthcare", "source_code": "S13",
     "name": "Doctors per 10k",
     "description": "Registered allopathic doctors per 10,000 population.",
     "unit": "per 10k", "direction": "higher_better",
     "cadence": "annual", "order": 5,
     "notes": "MBBS registered with state medical council."},
    {"code": "H06", "pillar_code": "healthcare", "source_code": "S11",
     "name": "Anemia in women 15-49",
     "description": "% women 15-49 with anemia (Hb<12 g/dL).",
     "unit": "%", "direction": "lower_better",
     "cadence": "sporadic", "order": 6,
     "notes": "Punjab a major concern — NFHS-5 ~59%."},

    # ── Infrastructure ─────────────────────────────────────────────────────
    {"code": "I01", "pillar_code": "infrastructure", "source_code": "S14",
     "name": "Road density",
     "description": "Total road length per 100 km² geographical area.",
     "unit": "km/100km2", "direction": "higher_better",
     "cadence": "annual", "order": 1,
     "notes": "All roads aggregated: PMGSY + state + national."},
    {"code": "I02", "pillar_code": "infrastructure", "source_code": "S15",
     "name": "Household electrification rate",
     "description": "% households with electricity as primary lighting source.",
     "unit": "%", "direction": "higher_better",
     "cadence": "annual", "order": 2,
     "notes": "Punjab ~100% since 2018; add reliability (hours/day) later."},
    {"code": "I03", "pillar_code": "infrastructure", "source_code": "S16",
     "name": "JJM piped water coverage",
     "description": "% of rural households with functional tap water connection.",
     "unit": "%", "direction": "higher_better",
     "cadence": "real-time", "order": 3,
     "notes": "JJM launched Aug 2019."},
    {"code": "I04", "pillar_code": "infrastructure", "source_code": "S17",
     "name": "Broadband subscribers per 100",
     "description": "Wireless + wireline broadband subscribers per 100 pop.",
     "unit": "per 100", "direction": "higher_better",
     "cadence": "quarterly", "order": 4,
     "notes": "TRAI circle-level; Punjab circle covers state."},
    {"code": "I05", "pillar_code": "infrastructure", "source_code": "S18",
     "name": "Sanitation coverage (ODF+ villages)",
     "description": "% of villages certified ODF Plus.",
     "unit": "%", "direction": "higher_better",
     "cadence": "real-time", "order": 5,
     "notes": "SBM-G Phase 2 metric; baseline late 2020."},
    {"code": "I06", "pillar_code": "infrastructure", "source_code": "S19",
     "name": "PMGSY road completion rate",
     "description": "Sanctioned rural road length completed cumulatively.",
     "unit": "%", "direction": "higher_better",
     "cadence": "real-time", "order": 6,
     "notes": "State-level OMMAS reports."},

    # ── Law & Order ────────────────────────────────────────────────────────
    {"code": "LO01", "pillar_code": "law_and_order", "source_code": "S20",
     "name": "IPC crime rate",
     "description": "Total IPC crimes registered per 100,000 population.",
     "unit": "per 100k", "direction": "lower_better",
     "cadence": "annual", "order": 1,
     "notes": "NCRB 2-year lag. Punjab historically low IPC vs national avg."},
    {"code": "LO02", "pillar_code": "law_and_order", "source_code": "S20",
     "name": "Conviction rate (IPC)",
     "description": "% of IPC cases resulting in conviction out of trials completed.",
     "unit": "%", "direction": "higher_better",
     "cadence": "annual", "order": 2,
     "notes": "Reflects prosecution + judicial quality. Punjab ~40-50%."},
    {"code": "LO03", "pillar_code": "law_and_order", "source_code": "S20",
     "name": "Crime against women rate",
     "description": "Crimes against women per 100,000 female population.",
     "unit": "per 100k", "direction": "lower_better",
     "cadence": "annual", "order": 3,
     "notes": "Rape, DV, dowry deaths, kidnapping, cruelty by husband."},
    {"code": "LO04", "pillar_code": "law_and_order", "source_code": "S20",
     "name": "Cybercrime rate",
     "description": "Cybercrimes registered per 100,000 population.",
     "unit": "per 100k", "direction": "lower_better",
     "cadence": "annual", "order": 4,
     "notes": "IT Act + IPC-cyber sections."},
    {"code": "LO05", "pillar_code": "law_and_order", "source_code": "S21",
     "name": "Police strength per 100k",
     "description": "Actual civil police personnel per 100,000 population.",
     "unit": "per 100k", "direction": "higher_better",
     "cadence": "annual", "order": 5,
     "notes": "UN norm ~222/100k. Punjab reported ~275/100k (2022)."},
    {"code": "LO06", "pillar_code": "law_and_order", "source_code": "S20",
     "name": "IPC/BNS trial pendency %",
     "description": "% of IPC/BNS cases pending trial at end of year (of total cases for trial).",
     "unit": "%", "direction": "lower_better",
     "cadence": "annual", "order": 6,
     "notes": "NCRB CII 2024 Table 18A.2 col 22 (Pendency Percentage). "
                "Lower = faster criminal-court throughput. Higher = backlog."},

    # ── Governance & Accountability (Phase 2) ──────────────────────────────
    # Sourced from CAG State Finances Audit Reports (Phase 1 extraction is
    # already in gpi_cag_extractions). All four are "lower is better" —
    # fewer audit findings + faster PAC follow-through = healthier governance.
    {"code": "G01", "pillar_code": "governance", "source_code": "S02",
     "name": "Audit paras raised (annual count)",
     "description": "Total audit paragraphs raised in the CAG State Finances Audit Report for the year.",
     "unit": "count", "direction": "lower_better",
     "cadence": "annual", "order": 1,
     "notes": "Fewer findings = tighter fiscal discipline. Extracted per SFAR by Gemini."},
    {"code": "G02", "pillar_code": "governance", "source_code": "S02",
     "name": "Long-pending audit paras (>5 years)",
     "description": "Count of audit observations still unresolved for more than 5 years.",
     "unit": "count", "direction": "lower_better",
     "cadence": "annual", "order": 2,
     "notes": "Direct measure of executive follow-through on audit findings."},
    {"code": "G03", "pillar_code": "governance", "source_code": "S02",
     "name": "PAC recommendations pending",
     "description": "Count of Public Accounts Committee recommendations awaiting government action.",
     "unit": "count", "direction": "lower_better",
     "cadence": "annual", "order": 3,
     "notes": "Reflects legislative-oversight closure rate."},
    {"code": "G04", "pillar_code": "governance", "source_code": "S02",
     "name": "Audit money value / GSDP",
     "description": "Total money value of audit observations as % of GSDP.",
     "unit": "% of GSDP", "direction": "lower_better",
     "cadence": "annual", "order": 4,
     "notes": "money_value_observations_crore / gsdp × 100. Size-normalized "
                 "so small and large states are comparable."},

    # ── Fiscal Efficiency (Phase 2) ────────────────────────────────────────
    # How well the state converts its economy into revenue — distinct from
    # Public Finance's deficit/debt LEVELS. Both indicators sourced directly
    # from CAG SFAR extractions (fields already populated in gpi_cag_extractions).
    {"code": "EF01", "pillar_code": "efficiency", "source_code": "S02",
     "name": "Own Tax Revenue / GSDP",
     "description": "Percentage of state's GSDP collected as its own tax revenue "
                     "(excluding central transfers).",
     "unit": "% of GSDP", "direction": "higher_better",
     "cadence": "annual", "order": 1,
     "notes": "Signals indigenous tax-collection efficiency. Punjab ~7%, "
                 "Tamil Nadu ~7.5%, Bihar ~4%."},
    {"code": "EF02", "pillar_code": "efficiency", "source_code": "S02",
     "name": "Revenue Receipts / GSDP",
     "description": "Total revenue receipts (own tax + own non-tax + central transfers) "
                     "as percentage of GSDP.",
     "unit": "% of GSDP", "direction": "higher_better",
     "cadence": "annual", "order": 2,
     "notes": "Total fiscal capacity per unit of state economy."},

    # ── Efficiency (Phase 3) — derived from CAG Performance Audits ─────────
    # Aggregated across all PAs for a state × year by gpi_aggregate_pa_efficiency.py.
    # Each PA gets equal weight in the average (not project-count weighted) since
    # some PAs report "schemes audited" others report "projects" — not directly
    # comparable numerators.
    {"code": "EF03", "pillar_code": "efficiency", "source_code": "S02",
     "name": "Project Delay Rate",
     "description": "% of audited projects/schemes with time overrun (aggregate across PAs).",
     "unit": "%", "direction": "lower_better",
     "cadence": "annual", "order": 3,
     "notes": "Cross-PA aggregate. Higher = more chronic delivery slippage."},
    {"code": "EF04", "pillar_code": "efficiency", "source_code": "S02",
     "name": "Cost Overrun Rate",
     "description": "% of audited projects/schemes with cost overrun >10% (aggregate across PAs).",
     "unit": "%", "direction": "lower_better",
     "cadence": "annual", "order": 4,
     "notes": "Cross-PA aggregate. Higher = weaker cost control."},
    {"code": "EF05", "pillar_code": "efficiency", "source_code": "S02",
     "name": "Budget Utilization Gap",
     "description": "|100 - (actual expenditure / budgeted expenditure × 100)|. "
                     "0 = perfect utilization; higher = under- or over-spending.",
     "unit": "%", "direction": "lower_better",
     "cadence": "annual", "order": 5,
     "notes": "Absolute deviation from 100% utilization. Both under-spend (poor "
                 "implementation) and over-spend (poor planning) are bad."},
    {"code": "EF06", "pillar_code": "efficiency", "source_code": "S02",
     "name": "Physical Achievement Shortfall",
     "description": "100 - avg(physical achievement %) across audited schemes.",
     "unit": "%", "direction": "lower_better",
     "cadence": "annual", "order": 6,
     "notes": "How far from target the state's projects fell. 0 = met all targets."},
]


def validate():
    """Sanity checks — run before seeding to catch typos and orphans."""
    pillar_codes = {p["code"] for p in PILLARS}
    source_codes = {s["code"] for s in SOURCES}
    indicator_codes = set()
    errors = []

    for i, p in enumerate(PILLARS):
        for key in ("code", "name", "description", "default_weight", "display_order"):
            if key not in p:
                errors.append(f"Pillar #{i} missing '{key}'")

    weight_sum = sum(p["default_weight"] for p in PILLARS)
    if abs(weight_sum - 1.0) > 0.01:
        errors.append(f"Pillar default_weights sum to {weight_sum:.4f}, not 1.0")

    for i, s in enumerate(SOURCES):
        for key in ("code", "name", "publisher", "url"):
            if key not in s:
                errors.append(f"Source #{i} ({s.get('code','?')}) missing '{key}'")

    for ind in INDICATORS:
        if ind["code"] in indicator_codes:
            errors.append(f"Duplicate indicator code: {ind['code']}")
        indicator_codes.add(ind["code"])
        if ind["pillar_code"] not in pillar_codes:
            errors.append(f"Indicator {ind['code']} → unknown pillar '{ind['pillar_code']}'")
        if ind.get("source_code") and ind["source_code"] not in source_codes:
            errors.append(f"Indicator {ind['code']} → unknown source '{ind['source_code']}'")
        if ind["direction"] not in ("higher_better", "lower_better"):
            errors.append(f"Indicator {ind['code']} → bad direction '{ind['direction']}'")

    return errors


if __name__ == "__main__":
    errs = validate()
    print(f"Pillars:    {len(PILLARS)}")
    print(f"Sources:    {len(SOURCES)}")
    print(f"Indicators: {len(INDICATORS)}")
    if errs:
        print("\nValidation errors:")
        for e in errs:
            print(f"  ✗ {e}")
        raise SystemExit(1)
    print("\n✓ Validation passed.")
