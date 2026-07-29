"""SQLAlchemy models for the Government Performance Index (GPI).

Design principles:
  - Reference tables (Pillar, Source, Indicator) hold the METHODOLOGY definitions
    seeded once from scripts/gpi_definitions.py.
  - Fact tables (IndicatorValue, PillarScore, GpiScore) hold state × year data
    written by ingesters + the scoring engine.
  - Every value carries source URL + retrieval timestamp for auditability.
  - Multi-state ready from day one — Punjab pilot uses state_id=23, scales to
    all 36 states/UTs later without schema change.

The Government Performance Index (GPI) is a state-year composite score in [0, 100]
computed as a weighted average of 6 pillar scores; each pillar is a weighted
average of 6 min-max normalized indicators drawn from public sources (RBI, MOSPI,
UDISE+, NFHS, HMIS, NCRB, and others documented in the GPI Phase 1 spec).
"""
from datetime import datetime
from sqlalchemy import (
    Column, Integer, String, Text, Float, Boolean, DateTime, Date,
    ForeignKey, Index, UniqueConstraint,
)
from sqlalchemy.orm import relationship
from app.database import Base


class GpiPillar(Base):
    """The 6 (later 14) top-level pillars of the GPI.

    Weight defaults sum to 1.0 across pillars. User can re-weight in UI without
    changing this table.
    """
    __tablename__ = "gpi_pillars"
    id = Column(Integer, primary_key=True)
    code = Column(String(32), unique=True, nullable=False)   # "economy", "law_and_order"
    name = Column(String(64), nullable=False)
    description = Column(Text)
    default_weight = Column(Float, nullable=False)           # e.g. 0.1667 (1/6)
    display_order = Column(Integer, nullable=False)

    indicators = relationship("GpiIndicator", back_populates="pillar")


class GpiSource(Base):
    """Canonical data-source registry (RBI, NCRB, MOSPI, ...)."""
    __tablename__ = "gpi_sources"
    id = Column(Integer, primary_key=True)
    code = Column(String(16), unique=True, nullable=False)   # "S01", "S02", ...
    name = Column(String, nullable=False)
    publisher = Column(String)
    url = Column(String)                                     # canonical landing URL
    format = Column(String(32))                              # "PDF", "XLSX", "Dashboard"
    refresh_cadence = Column(String(128))                    # "Annual", "Monthly", "Real-time",
                                                              # or longer notes like "Sporadic (2018, 2019; covers 25 states + 2 UTs)"
    notes = Column(Text)

    indicators = relationship("GpiIndicator", back_populates="source")


class GpiIndicator(Base):
    """A specific measurable, tied to one pillar and one primary data source."""
    __tablename__ = "gpi_indicators"
    id = Column(Integer, primary_key=True)
    pillar_id = Column(Integer, ForeignKey("gpi_pillars.id"), nullable=False)
    source_id = Column(Integer, ForeignKey("gpi_sources.id"))
    code = Column(String(16), unique=True, nullable=False)   # "E01", "F01", "LO01"
    name = Column(String, nullable=False)
    description = Column(Text)
    unit = Column(String(32))                                # "%", "per 100k", "₹/capita"
    # "higher_better" means larger raw values map to higher normalized scores;
    # "lower_better" means the opposite (e.g. IMR, crime rate, fiscal deficit).
    direction = Column(String(16), nullable=False)
    cadence = Column(String(32))                             # "annual", "sporadic", ...
    default_weight = Column(Float, default=1.0)              # relative weight within pillar
    display_order = Column(Integer)
    notes = Column(Text)

    pillar = relationship("GpiPillar", back_populates="indicators")
    source = relationship("GpiSource", back_populates="indicators")
    values = relationship("GpiIndicatorValue", back_populates="indicator",
                           cascade="all, delete-orphan")


class GpiIndicatorValue(Base):
    """The raw fact table. One row per (indicator, state, fiscal_year).

    `raw_value` is the measurement in the indicator's native unit.
    `normalized_value` is 0-100 (populated by scripts/gpi_compute_scores.py).
    Provenance fields (source_url, source_document, extracted_at) let us cite
    every score back to its origin — a hard requirement for the site's
    "click any score → see where it came from" promise.
    """
    __tablename__ = "gpi_indicator_values"
    id = Column(Integer, primary_key=True)
    indicator_id = Column(Integer, ForeignKey("gpi_indicators.id"), nullable=False)
    state_id     = Column(Integer, ForeignKey("states.id"), nullable=False)
    # 2018 = FY18 = 2017-18 (India's fiscal year runs Apr-Mar; we anchor to
    # ending calendar year, so FY 2017-18 → 2018 here).
    fiscal_year  = Column(Integer, nullable=False)

    raw_value        = Column(Float)
    normalized_value = Column(Float)                         # 0-100
    national_rank    = Column(Integer)                       # rank among states w/ same-year data

    # Provenance
    source_url        = Column(String)                       # exact URL (may differ from canonical)
    source_document   = Column(String)                       # "Handbook 2024, Table 1.3"
    extracted_at      = Column(DateTime, default=datetime.utcnow)
    extraction_method = Column(String(32))                   # "manual", "scraped", "llm_extracted"

    # "current" = fresh data point for this year
    # "carried_forward" = last known value re-used because current year isn't available
    # "interpolated" = linearly interpolated between two known observations
    staleness = Column(String(32), default="current")
    notes     = Column(Text)

    indicator = relationship("GpiIndicator", back_populates="values")

    __table_args__ = (
        UniqueConstraint("indicator_id", "state_id", "fiscal_year",
                          name="uq_indicator_state_year"),
        Index("ix_indicator_value_state_year",  "state_id", "fiscal_year"),
    )


class GpiPillarScore(Base):
    """Aggregated pillar score per (state, year).

    Written by scripts/gpi_compute_scores.py — recomputed idempotently from
    the underlying indicator values. `coverage_pct` surfaces to the UI so users
    can tell when a pillar score is based on thin data.
    """
    __tablename__ = "gpi_pillar_scores"
    id = Column(Integer, primary_key=True)
    state_id    = Column(Integer, ForeignKey("states.id"), nullable=False)
    pillar_id   = Column(Integer, ForeignKey("gpi_pillars.id"), nullable=False)
    fiscal_year = Column(Integer, nullable=False)

    score            = Column(Float, nullable=False)         # 0-100
    indicators_used  = Column(Integer, nullable=False)
    indicators_total = Column(Integer, nullable=False)
    coverage_pct     = Column(Float)                         # indicators_used / total * 100
    computed_at      = Column(DateTime, default=datetime.utcnow)

    __table_args__ = (
        UniqueConstraint("state_id", "pillar_id", "fiscal_year",
                          name="uq_pillar_state_year"),
    )


class GpiCagExtraction(Base):
    """Raw fields extracted from a CAG audit report (SFAR / Revenue / etc.).

    Distinct from `gpi_indicator_values` because a single SFAR yields many
    fields at once, and we want to preserve the audit-para counts + audit-
    verified fiscal ratios for Phase 2 Governance pillar without polluting
    the main indicator-values table with sparse pseudo-indicators.

    Each row = one SFAR PDF's structured extract.
    """
    __tablename__ = "gpi_cag_extractions"
    id = Column(Integer, primary_key=True)
    state_id       = Column(Integer, ForeignKey("states.id"), nullable=False)
    audit_year     = Column(String(16), nullable=False)   # "2017-18"
    report_no      = Column(String(32))                    # "1/2025"
    publication_year = Column(Integer)                     # year the report was tabled

    # Fiscal indicators (as % of GSDP unless noted)
    fiscal_deficit_pct_gsdp     = Column(Float)  # cross-validates F01
    revenue_deficit_pct_gsdp    = Column(Float)  # cross-validates F05
    primary_deficit_pct_gsdp    = Column(Float)
    debt_pct_gsdp               = Column(Float)  # F02 numerator ratio
    interest_pct_revenue_receipts = Column(Float)  # F06
    capital_outlay_pct_gsdp     = Column(Float)
    revenue_receipts_pct_gsdp   = Column(Float)
    own_tax_revenue_pct_gsdp    = Column(Float)

    # Absolute figures (₹ Crore)
    gsdp_current_crore          = Column(Float)
    outstanding_debt_crore      = Column(Float)

    # Governance / audit metadata — feeds Phase 2 Governance pillar
    audit_paras_raised          = Column(Integer)
    audit_paras_over_5_yrs      = Column(Integer)
    pac_recommendations_pending = Column(Integer)
    money_value_observations_crore = Column(Float)

    # Provenance
    source_pdf                  = Column(String)  # relative path within data/cag/pdfs/
    source_url                  = Column(String)
    extraction_confidence       = Column(String(16))  # "high" | "medium" | "low"
    extraction_notes            = Column(Text)
    extracted_at                = Column(DateTime, default=datetime.utcnow)
    gemini_raw_response         = Column(Text)   # full JSON for auditability

    __table_args__ = (
        UniqueConstraint("state_id", "audit_year", "report_no",
                          name="uq_cag_extract_state_year_report"),
    )


class GpiCagPaExtraction(Base):
    """Per-Performance-Audit extraction from CAG reports.

    Different from GpiCagExtraction (which is for State Finances Audit Reports).
    PAs are TOPIC-SPECIFIC deep-dives on particular schemes/departments — one
    row per PA report. Multiple PAs per state × year get aggregated by
    gpi_aggregate_pa_efficiency.py into the Efficiency pillar indicators.
    """
    __tablename__ = "gpi_cag_pa_extractions"
    id                              = Column(Integer, primary_key=True)
    state_id                        = Column(Integer, ForeignKey("states.id"), nullable=False)

    # Audit period (PAs typically cover a range: "2017-22")
    audit_period_start              = Column(Integer)   # e.g. 2017
    audit_period_end                = Column(Integer)   # e.g. 2022
    fiscal_year                     = Column(Integer)   # audit_period_end (schema-canonical)

    # Report identity
    report_topic                    = Column(String)    # e.g. "Public Health Infrastructure"
    report_no                       = Column(String(64))   # "Report No. 4 of 2024"

    # Cross-topic metrics — every PA reports these somehow
    total_projects_audited          = Column(Integer)
    projects_time_overrun           = Column(Integer)
    projects_cost_overrun_over_10pct = Column(Integer)
    avg_time_overrun_months         = Column(Float)
    avg_cost_overrun_pct            = Column(Float)

    # Financial delivery
    total_budgeted_crore            = Column(Float)
    total_actual_expenditure_crore  = Column(Float)
    financial_utilization_pct       = Column(Float)

    # Outcomes
    physical_achievement_pct        = Column(Float)     # vs targets
    observations_summary            = Column(Text)

    # Provenance
    source_pdf                      = Column(String)
    source_url                      = Column(String)
    extraction_confidence           = Column(String(16))
    extracted_at                    = Column(DateTime, default=datetime.utcnow)
    gemini_raw_response             = Column(Text)

    __table_args__ = (
        UniqueConstraint("state_id", "report_no",
                          name="uq_cag_pa_state_report"),
    )


class StateMinister(Base):
    """State cabinet ministers by portfolio, with change tracking.

    Design: each row is one (state, portfolio, minister) assignment. When a
    minister changes hands, we DON'T delete the old row — we set its end_date
    and insert a new row for the incoming minister. This preserves history
    so we can show "Punjab Finance Minister since 2022 · previously X".

    `portfolio_key` is our canonical mapping ("finance", "health", etc.).
    `portfolio_display` is the raw label from the source ("Finance & Planning
    and Programme Implementation"). We show display but query on key.

    `pillar_code` maps the portfolio to one of our GPI pillars — used to
    render the minister chip on the correct pillar section on /gpi.
    """
    __tablename__ = "state_ministers"
    id = Column(Integer, primary_key=True)
    state_id           = Column(Integer, ForeignKey("states.id"), nullable=False)

    # Portfolio identity
    portfolio_key      = Column(String(32), nullable=False)  # "finance", "health", "home"
    portfolio_display  = Column(String(256), nullable=False) # raw label from source
    pillar_code        = Column(String(32))                  # GPI pillar this maps to

    # Minister identity
    minister_name      = Column(String(128), nullable=False)
    party              = Column(String(64))
    is_cm              = Column(Boolean, default=False)      # Chief Minister also holding this portfolio?

    # Tenure — end_date NULL means "currently holding"
    sworn_in_date      = Column(Date)
    end_date           = Column(Date)                        # null = current

    # Provenance
    source_url         = Column(String(512))
    source_type        = Column(String(32), default="wikipedia")  # "wikipedia" | "state_portal" | "pib"
    scraped_at         = Column(DateTime, default=datetime.utcnow)
    notes              = Column(Text)


class GpiScore(Base):
    """The composite Government Performance Index per (state, year).

    GPI = weighted average of pillar scores (default weights are equal across
    the 6 Phase-1 pillars). `pillars_scored` tells us how many of the 6 had
    enough data to contribute — a score built on 3 pillars is less trustworthy
    than one built on 6.
    """
    __tablename__ = "gpi_scores"
    id = Column(Integer, primary_key=True)
    state_id    = Column(Integer, ForeignKey("states.id"), nullable=False)
    fiscal_year = Column(Integer, nullable=False)

    score          = Column(Float, nullable=False)           # 0-100
    national_rank  = Column(Integer)                         # 1 = best (populated once national data lands)
    pillars_scored = Column(Integer)                         # of 6 (or 14 in future)
    computed_at    = Column(DateTime, default=datetime.utcnow)

    __table_args__ = (
        UniqueConstraint("state_id", "fiscal_year", name="uq_gpi_state_year"),
    )
