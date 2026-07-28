"""Aggregate per-PA extractions (gpi_cag_pa_extractions) into Efficiency
pillar indicator values (EF03-EF06).

Each PA covers a specific topic — a state's efficiency signal comes from
combining multiple PAs. For each state × fiscal_year, we take:

    EF03 Project Delay Rate:
        SUM(projects_time_overrun) / SUM(total_projects_audited) × 100

    EF04 Cost Overrun Rate:
        SUM(projects_cost_overrun_over_10pct) / SUM(total_projects_audited) × 100

    EF05 Budget Utilization Gap:
        |100 - SUM(actual_expenditure) / SUM(budgeted) × 100|

    EF06 Physical Achievement Shortfall:
        100 - MEAN(physical_achievement_pct across PAs)

Fiscal_year comes from each PA's audit_period_end (per PA extractor's
canonical mapping). Multi-year PAs (e.g., 2017-22 audit) map to their end
year only — we don't split across intermediate years.

Usage:
    python scripts/gpi_aggregate_pa_efficiency.py
    python scripts/gpi_aggregate_pa_efficiency.py --state Punjab --dry-run
"""
from __future__ import annotations
import argparse
import sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                  formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--state")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    from app.database import SessionLocal
    from app import models
    from app.gpi_models import (
        GpiCagPaExtraction, GpiIndicator, GpiIndicatorValue,
    )
    from app.models import State

    session = SessionLocal()

    ind_by_code = {
        i.code: i for i in session.query(GpiIndicator).filter(
            GpiIndicator.code.in_(["EF03", "EF04", "EF05", "EF06"])
        ).all()
    }
    if len(ind_by_code) != 4:
        raise SystemExit("Efficiency indicators EF03-EF06 not seeded — run gpi_seed.py")

    state_filter = None
    if args.state:
        st = session.query(State).filter(
            (State.name == args.state) | (State.code == args.state.upper())
        ).one_or_none()
        if not st:
            raise SystemExit(f"Unknown state: {args.state}")
        state_filter = st.id

    # Load all PA extractions
    q = session.query(GpiCagPaExtraction)
    if state_filter:
        q = q.filter(GpiCagPaExtraction.state_id == state_filter)
    pas = q.all()

    print(f"PA extractions loaded: {len(pas)}")

    # Group by (state_id, fiscal_year)
    groups = defaultdict(list)
    for pa in pas:
        if not pa.fiscal_year:
            continue
        groups[(pa.state_id, pa.fiscal_year)].append(pa)

    print(f"Distinct (state, fiscal_year) groups: {len(groups)}")

    # For each group, compute the 4 indicator values
    counts = {"EF03": 0, "EF04": 0, "EF05": 0, "EF06": 0}
    punjab_id = session.query(State.id).filter(State.code == "PB").scalar()
    punjab_out = {}

    for (state_id, fy), group in groups.items():
        # Sum-based rates (weighted by project count implicitly)
        tot = sum(p.total_projects_audited or 0 for p in group)
        delayed = sum(p.projects_time_overrun or 0 for p in group)
        overrun = sum(p.projects_cost_overrun_over_10pct or 0 for p in group)

        ef03 = round(100.0 * delayed / tot, 2) if tot > 0 else None
        ef04 = round(100.0 * overrun / tot, 2) if tot > 0 else None

        # Budget utilization gap
        tot_bud = sum(p.total_budgeted_crore or 0 for p in group)
        tot_act = sum(p.total_actual_expenditure_crore or 0 for p in group)
        if tot_bud > 0:
            util = tot_act / tot_bud * 100
            ef05 = round(abs(100.0 - util), 2)
        else:
            ef05 = None

        # Physical achievement shortfall = 100 - avg
        phys_vals = [p.physical_achievement_pct for p in group
                      if p.physical_achievement_pct is not None]
        if phys_vals:
            avg_phys = sum(phys_vals) / len(phys_vals)
            ef06 = round(100.0 - avg_phys, 2)
        else:
            ef06 = None

        # Upsert per indicator
        for code, val in [("EF03", ef03), ("EF04", ef04),
                            ("EF05", ef05), ("EF06", ef06)]:
            if val is None:
                continue
            _upsert(session, ind_by_code[code].id, state_id, fy, val, group)
            counts[code] += 1
            if state_id == punjab_id:
                punjab_out.setdefault(code, {})[fy] = val

    if not args.dry_run:
        session.commit()
    else:
        session.rollback()

    print()
    print("═══════════════════════ Summary per indicator ══════════════════════")
    for code, n in counts.items():
        ind = ind_by_code[code]
        print(f"  {code} ({ind.name[:36]:<36s})  writes={n}")

    if punjab_out:
        print()
        print("Punjab Efficiency (project-audit derived) values:")
        all_years = sorted({y for d in punjab_out.values() for y in d})
        header = "  FY    " + " ".join(f"{c:>10s}" for c in ["EF03", "EF04", "EF05", "EF06"])
        print(header)
        for y in all_years:
            row = f"  FY{y}"
            for code in ["EF03", "EF04", "EF05", "EF06"]:
                v = punjab_out.get(code, {}).get(y)
                row += f"  {v if v is None else f'{v:.1f}%':>10s}"
            print(row)

    session.close()
    if args.dry_run:
        print("\n(dry run — no writes)")
    print()
    print("Next: python scripts/gpi_compute_scores.py")


def _upsert(session, ind_id, state_id, fy, val, source_pas):
    from app.gpi_models import GpiIndicatorValue

    existing = session.query(GpiIndicatorValue).filter_by(
        indicator_id=ind_id, state_id=state_id, fiscal_year=fy,
    ).one_or_none()

    topics = ", ".join(sorted({p.report_topic for p in source_pas if p.report_topic})[:3])
    src_docs = f"CAG PA aggregate · {len(source_pas)} PAs · topics: {topics}"

    payload = {
        "raw_value":         val,
        "source_url":        "",
        "source_document":   src_docs,
        "extraction_method": "cag_pa_aggregate",
        "staleness":         "current",
        "notes":             f"Aggregated across {len(source_pas)} Performance Audit(s)",
        "extracted_at":      datetime.utcnow(),
    }

    if existing:
        for k, v in payload.items():
            setattr(existing, k, v)
        existing.normalized_value = None
        existing.national_rank = None
    else:
        session.add(GpiIndicatorValue(
            indicator_id=ind_id, state_id=state_id, fiscal_year=fy,
            **payload,
        ))
    session.flush()


if __name__ == "__main__":
    main()
