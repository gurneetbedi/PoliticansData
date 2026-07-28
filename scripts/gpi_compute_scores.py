"""Compute GPI scores: normalize indicator values → pillar scores → composite GPI.

Pipeline:
    1. For each (indicator, fiscal_year), compute national distribution stats
       (min, max, p5, p95) across all states with a raw_value.
    2. For each raw value, compute normalized_value in [0, 100]:
         higher_better: score = 100 * (v_clipped - p5) / (p95 - p5)
         lower_better:  score = 100 * (p95 - v_clipped) / (p95 - p5)
       (v_clipped is v winsorized at [p5, p95].)
    3. National rank per (indicator, year): 1 = best.
    4. For each (state, pillar, year), pillar_score = weighted mean of the
       indicator normalized_values available in that year, with weights
       re-normalized over available indicators (missing indicators don't drag
       the score to zero).
    5. For each (state, year), GPI = weighted mean of pillar scores (equal
       default weights across the 6 Phase-1 pillars).

Writes to gpi_indicator_values.normalized_value + national_rank, then to
gpi_pillar_scores and gpi_scores. Idempotent: re-running replaces existing
scores for the (state, year) it computed.

CAVEAT: Because Phase 1 currently ingests only Punjab data, the "national
distribution" for normalization is effectively a distribution of ONE. The
scoring math still runs, but normalized values will be flat 50 or 100. Real
signal appears once we ingest 5+ states. Until then use raw_value for judgment.

Usage:
    python scripts/gpi_compute_scores.py                   # score everything
    python scripts/gpi_compute_scores.py --year 2023       # score one year
    python scripts/gpi_compute_scores.py --state PB        # score one state
    python scripts/gpi_compute_scores.py --dry-run         # preview
"""
from __future__ import annotations
import argparse
import statistics
import sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.database import SessionLocal
from app import models
from app.gpi_models import (
    GpiPillar, GpiIndicator, GpiIndicatorValue,
    GpiPillarScore, GpiScore,
)
from app.models import State


def percentile(sorted_values, p):
    """Linear-interpolated percentile. p in [0, 100]."""
    if not sorted_values:
        return None
    if len(sorted_values) == 1:
        return sorted_values[0]
    k = (len(sorted_values) - 1) * (p / 100.0)
    lo = int(k)
    hi = min(lo + 1, len(sorted_values) - 1)
    frac = k - lo
    return sorted_values[lo] + (sorted_values[hi] - sorted_values[lo]) * frac


def normalize_indicator_year(session, indicator: GpiIndicator, year: int) -> int:
    """Normalize all values for one (indicator, year). Returns # rows updated."""
    values = session.query(GpiIndicatorValue).filter_by(
        indicator_id=indicator.id, fiscal_year=year
    ).filter(GpiIndicatorValue.raw_value.isnot(None)).all()
    if not values:
        return 0

    raw_sorted = sorted(v.raw_value for v in values)
    p5  = percentile(raw_sorted, 5)
    p95 = percentile(raw_sorted, 95)

    # Degenerate case: only one state, or all values identical.
    # Score everyone 50 (neutral) since there's no distribution to normalize against.
    if p95 == p5:
        for v in values:
            v.normalized_value = 50.0
            v.national_rank = 1
        return len(values)

    for v in values:
        vc = max(p5, min(p95, v.raw_value))  # winsorize
        if indicator.direction == "higher_better":
            score = 100.0 * (vc - p5) / (p95 - p5)
        else:  # lower_better
            score = 100.0 * (p95 - vc) / (p95 - p5)
        v.normalized_value = round(score, 2)

    # Rank: 1 = best. For higher_better, best = highest raw; for lower_better, best = lowest raw.
    reverse = (indicator.direction == "higher_better")
    ranked = sorted(values, key=lambda v: v.raw_value, reverse=reverse)
    for rank, v in enumerate(ranked, 1):
        v.national_rank = rank

    return len(values)


def compute_pillar_scores(session, state_id: int, year: int):
    """For one (state, year), compute the 6 pillar scores from indicator values."""
    written = 0
    pillars = session.query(GpiPillar).all()

    for pillar in pillars:
        indicators = session.query(GpiIndicator).filter_by(pillar_id=pillar.id).all()
        indicator_ids = [i.id for i in indicators]

        values = session.query(GpiIndicatorValue).filter(
            GpiIndicatorValue.state_id == state_id,
            GpiIndicatorValue.fiscal_year == year,
            GpiIndicatorValue.indicator_id.in_(indicator_ids),
            GpiIndicatorValue.normalized_value.isnot(None),
        ).all()

        used = len(values)
        total = len(indicators)

        if used == 0:
            # Delete any stale pillar score row so downstream GPI knows we have no data
            session.query(GpiPillarScore).filter_by(
                state_id=state_id, pillar_id=pillar.id, fiscal_year=year
            ).delete()
            continue

        # Weight indicators equally within pillar for Phase 1
        pillar_score = sum(v.normalized_value for v in values) / used
        coverage = 100.0 * used / total

        existing = session.query(GpiPillarScore).filter_by(
            state_id=state_id, pillar_id=pillar.id, fiscal_year=year
        ).one_or_none()

        if existing:
            existing.score = round(pillar_score, 2)
            existing.indicators_used = used
            existing.indicators_total = total
            existing.coverage_pct = round(coverage, 1)
            existing.computed_at = datetime.utcnow()
        else:
            session.add(GpiPillarScore(
                state_id=state_id,
                pillar_id=pillar.id,
                fiscal_year=year,
                score=round(pillar_score, 2),
                indicators_used=used,
                indicators_total=total,
                coverage_pct=round(coverage, 1),
            ))
        written += 1

    return written


MAX_CARRY_FORWARD_YEARS = 3


def compute_gpi(session, state_id: int, year: int):
    """For one (state, year), roll up pillar scores into composite GPI.

    Carry-forward policy: if a pillar has no fresh score for `year`, we look
    back up to MAX_CARRY_FORWARD_YEARS and use the most recent available.
    This prevents spurious YoY drops when a pillar's underlying data lags
    (e.g. SRS-IMR is ~2 years behind; Healthcare would otherwise disappear
    from FY24/FY25 GPIs and cause an artificial 10-point drop). We accept
    slightly-stale coverage over misleading fluctuations.

    Beyond MAX_CARRY_FORWARD_YEARS the pillar is dropped — we don't want to
    keep re-using a 5-year-old NFHS number as if it were current.
    """
    pillars = session.query(GpiPillar).all()

    # For each pillar, find its most recent score at-or-before `year`,
    # within the carry-forward window.
    min_year = year - MAX_CARRY_FORWARD_YEARS
    contributing = []  # list of (pillar, pillar_score, is_stale)
    for p in pillars:
        latest = session.query(GpiPillarScore).filter(
            GpiPillarScore.state_id == state_id,
            GpiPillarScore.pillar_id == p.id,
            GpiPillarScore.fiscal_year <= year,
            GpiPillarScore.fiscal_year >= min_year,
        ).order_by(GpiPillarScore.fiscal_year.desc()).first()
        if latest is None:
            continue
        contributing.append((p, latest, latest.fiscal_year < year))

    if not contributing:
        session.query(GpiScore).filter_by(
            state_id=state_id, fiscal_year=year
        ).delete()
        return False

    total_weight = sum(p.default_weight for p, _, _ in contributing)
    weighted_sum = sum(ps.score * p.default_weight for p, ps, _ in contributing)
    gpi_score = weighted_sum / total_weight

    existing = session.query(GpiScore).filter_by(
        state_id=state_id, fiscal_year=year
    ).one_or_none()

    if existing:
        existing.score = round(gpi_score, 2)
        existing.pillars_scored = len(contributing)
        existing.computed_at = datetime.utcnow()
    else:
        session.add(GpiScore(
            state_id=state_id,
            fiscal_year=year,
            score=round(gpi_score, 2),
            pillars_scored=len(contributing),
        ))
    return True


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                  formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--year", type=int, help="Score one fiscal year only")
    ap.add_argument("--state", help="Score one state (2-letter code, e.g. PB)")
    ap.add_argument("--dry-run", action="store_true", help="Preview, don't commit")
    args = ap.parse_args()

    session = SessionLocal()
    try:
        # Determine which (state, year) pairs to compute for
        state_ids = None
        if args.state:
            st = session.query(State).filter_by(code=args.state).one_or_none()
            if not st:
                raise SystemExit(f"Unknown state code: {args.state}")
            state_ids = [st.id]

        # Step 1: normalize all indicators for the years we're touching
        indicators = session.query(GpiIndicator).all()
        pairs = session.query(
            GpiIndicatorValue.indicator_id,
            GpiIndicatorValue.fiscal_year,
        ).filter(
            GpiIndicatorValue.raw_value.isnot(None)
        ).distinct().all()

        if args.year:
            pairs = [(iid, y) for iid, y in pairs if y == args.year]

        by_indicator = {i.id: i for i in indicators}
        normalized_count = 0
        for iid, year in pairs:
            ind = by_indicator[iid]
            normalized_count += normalize_indicator_year(session, ind, year)

        # Flush so pillar-score queries below see the normalized values.
        # (SessionLocal is autoflush=False by design.)
        session.flush()

        print(f"Normalized {normalized_count} indicator values.")

        # Step 2 + 3: pillar scores + composite GPI
        # Get (state, year) pairs that have any values
        sy_pairs = session.query(
            GpiIndicatorValue.state_id,
            GpiIndicatorValue.fiscal_year,
        ).filter(
            GpiIndicatorValue.raw_value.isnot(None)
        ).distinct().all()

        if args.year:
            sy_pairs = [(s, y) for s, y in sy_pairs if y == args.year]
        if state_ids:
            sy_pairs = [(s, y) for s, y in sy_pairs if s in state_ids]

        pillar_writes = 0
        gpi_writes = 0
        for state_id, year in sy_pairs:
            pillar_writes += compute_pillar_scores(session, state_id, year)
            # Flush pillar_scores so compute_gpi's query below sees them.
            session.flush()
            if compute_gpi(session, state_id, year):
                gpi_writes += 1

        print(f"Pillar-score rows written: {pillar_writes}")
        print(f"GPI-score rows written:    {gpi_writes}")

        if args.dry_run:
            print("\nDRY RUN — rolling back")
            session.rollback()
        else:
            session.commit()

        # Report
        print()
        print("═══════════════════════ Current GPI ═══════════════════════")
        rows = session.query(GpiScore, State).join(
            State, State.id == GpiScore.state_id
        ).order_by(State.code, GpiScore.fiscal_year).all()
        if not rows:
            print("  (no scores yet — ingest data via gpi_ingest_csv.py first)")
        for gs, st in rows:
            print(f"  {st.code}  FY{gs.fiscal_year}  score={gs.score:>5.1f}  "
                  f"pillars_scored={gs.pillars_scored}/6")
        print("═══════════════════════════════════════════════════════════")
    finally:
        session.close()


if __name__ == "__main__":
    main()
