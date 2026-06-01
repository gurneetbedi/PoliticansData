"""
SQLAlchemy models for PoliTrack.

Design notes:
- `myneta_candidate_id` is the stable ID from myneta.info; it persists across
  election cycles for re-contesting candidates and is our primary join key.
- `ElectionAppearance` is the central many-to-many: one row per (politician,
  election cycle) with all the affidavit data for that specific filing.
- Assets/liabilities/criminal_cases hang off ElectionAppearance because the
  declarations change every election.
"""
from datetime import datetime
from sqlalchemy import (
    Column, Integer, String, Text, Float, Boolean, DateTime, ForeignKey, Index, BigInteger
)
from sqlalchemy.orm import relationship
from app.database import Base


class State(Base):
    __tablename__ = "states"
    id = Column(Integer, primary_key=True)
    name = Column(String(64), unique=True, nullable=False)  # e.g. "Punjab"
    code = Column(String(8), unique=True)                   # e.g. "PB"

    constituencies = relationship("Constituency", back_populates="state")


class Party(Base):
    __tablename__ = "parties"
    id = Column(Integer, primary_key=True)
    # "short_name" is misleadingly named — myneta sometimes uses a 40-char descriptive
    # party label (e.g. "SAD (Amritsar)(Simranjit Singh Mann)") where a real short
    # code doesn't exist. Use unconstrained String so we never truncation-error.
    short_name = Column(String, unique=True, nullable=False)
    full_name = Column(String)


class Constituency(Base):
    __tablename__ = "constituencies"
    id = Column(Integer, primary_key=True)
    name = Column(String, nullable=False)
    state_id = Column(Integer, ForeignKey("states.id"), nullable=False)
    house = Column(String(16), nullable=False)  # "Assembly" | "LokSabha" | "RajyaSabha"
    reserved_for = Column(String(8))            # "SC" | "ST" | None

    state = relationship("State", back_populates="constituencies")

    __table_args__ = (
        Index("ix_constituency_state_house_name", "state_id", "house", "name", unique=True),
    )


class Election(Base):
    __tablename__ = "elections"
    id = Column(Integer, primary_key=True)
    year = Column(Integer, nullable=False)
    house = Column(String(16), nullable=False)  # "Assembly" | "LokSabha" | "RajyaSabha"
    state_id = Column(Integer, ForeignKey("states.id"))  # null for national LS/RS

    # myneta URL slug for this election cycle, e.g. "punjab2022", "pb2012", "LokSabha2024"
    myneta_slug = Column(String, unique=True, nullable=False)

    state = relationship("State")

    __table_args__ = (
        Index("ix_election_year_house_state", "year", "house", "state_id"),
    )


class Politician(Base):
    __tablename__ = "politicians"
    id = Column(Integer, primary_key=True)
    name = Column(String, nullable=False)
    slug = Column(String, unique=True, nullable=False)  # url-safe identifier
    myneta_candidate_id = Column(Integer, unique=True, index=True)  # stable across cycles
    photo_url = Column(String)
    dob = Column(String(32))     # store as text since myneta data is often partial
    gender = Column(String(16))

    # Optional richer profile fields populated by the per-candidate detail scraper
    age = Column(Integer)
    profession = Column(String)

    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    appearances = relationship("ElectionAppearance", back_populates="politician",
                                cascade="all, delete-orphan")

    @property
    def display_name(self) -> str:
        """Name with a safe fallback — never returns an empty string in the UI."""
        n = (self.name or "").strip()
        if n:
            return n
        if self.myneta_candidate_id:
            return f"Candidate #{self.myneta_candidate_id}"
        return f"Politician #{self.id}"


class ElectionAppearance(Base):
    """One row per (politician, election). Holds the affidavit snapshot."""
    __tablename__ = "election_appearances"
    id = Column(Integer, primary_key=True)
    politician_id = Column(Integer, ForeignKey("politicians.id"), nullable=False)
    election_id = Column(Integer, ForeignKey("elections.id"), nullable=False)
    constituency_id = Column(Integer, ForeignKey("constituencies.id"))
    party_id = Column(Integer, ForeignKey("parties.id"))

    age = Column(Integer)
    education = Column(String)        # "Graduate", "Post Graduate", "10th Pass", etc.
    profession = Column(String)
    won = Column(Boolean, default=False)
    votes_received = Column(Integer)
    vote_share_pct = Column(Float)

    # Aggregate financial figures (in INR — store as BigInteger to handle Rs 100Cr+)
    total_assets_inr = Column(BigInteger)
    total_liabilities_inr = Column(BigInteger)
    movable_assets_inr = Column(BigInteger)
    immovable_assets_inr = Column(BigInteger)

    # Quick-access counts (denormalized for fast list views)
    criminal_cases_count = Column(Integer, default=0)
    serious_cases_count = Column(Integer, default=0)

    source_url = Column(String)        # link back to myneta candidate page
    scraped_at = Column(DateTime, default=datetime.utcnow)

    politician = relationship("Politician", back_populates="appearances")
    election = relationship("Election")
    constituency = relationship("Constituency")
    party = relationship("Party")
    assets = relationship("Asset", back_populates="appearance", cascade="all, delete-orphan")
    liabilities = relationship("Liability", back_populates="appearance", cascade="all, delete-orphan")
    cases = relationship("CriminalCase", back_populates="appearance", cascade="all, delete-orphan")

    __table_args__ = (
        Index("ix_appearance_politician_election", "politician_id", "election_id", unique=True),
    )


class Asset(Base):
    __tablename__ = "assets"
    id = Column(Integer, primary_key=True)
    appearance_id = Column(Integer, ForeignKey("election_appearances.id"), nullable=False)
    category = Column(String(64))      # "movable" | "immovable"
    subcategory = Column(String)       # "Cash", "Bank Deposits", "Land", "Buildings", etc.
    description = Column(Text)
    value_inr = Column(BigInteger)

    appearance = relationship("ElectionAppearance", back_populates="assets")


class Liability(Base):
    __tablename__ = "liabilities"
    id = Column(Integer, primary_key=True)
    appearance_id = Column(Integer, ForeignKey("election_appearances.id"), nullable=False)
    creditor = Column(String)
    description = Column(Text)
    amount_inr = Column(BigInteger)

    appearance = relationship("ElectionAppearance", back_populates="liabilities")


class CriminalCase(Base):
    __tablename__ = "criminal_cases"
    id = Column(Integer, primary_key=True)
    appearance_id = Column(Integer, ForeignKey("election_appearances.id"), nullable=False)
    ipc_sections = Column(String)         # "IPC 420, 467, 471" — can be very long
    description = Column(Text)
    case_number = Column(String)
    court = Column(String)
    charges_framed = Column(Boolean, default=False)
    is_serious = Column(Boolean, default=False)
    status = Column(String(64))           # "pending" | "convicted" | "acquitted"

    appearance = relationship("ElectionAppearance", back_populates="cases")
