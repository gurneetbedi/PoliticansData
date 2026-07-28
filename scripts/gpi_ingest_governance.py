"""Wire CAG audit-para data (from Phase 1 SFAR extraction) into Governance
pillar indicator values.

Phase 1's CAG SFAR extractor stored full extractions in `gpi_cag_extractions`
including audit-para counts and PAC recommendations. This script transforms
those into the 4 Governance-pillar indicators (G01-G04) and inserts them
into `gpi_indicator_values` so the scoring engine picks them up.

Mapping:
    G01  audit_paras_raised          → raw_value directly
    G02  audit_paras_over_5_yrs      → raw_value directly
    G03  pac_recommendations_pending → raw_value directly
    G04  money_value_observations_crore / gsdp_current_crore × 100

Fiscal-year mapping: audit_year "2022-23" → fiscal_year=2023.

Usage:
    python scripts/gpi_ingest_governance.py
    python scripts/gpi_ingest_governance.py --state Punjab --dry-run
"""
from __future__ import annotations
import argparse
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def audit_year_to_fiscal_year(audit_year: str) -> int | None:
    """'2022-23' → 2023 (ending calendar year, per our schema convention)."""
    if not audit_year or "-" not in audit_year:
        return None
    try:
        start = int(audit_year.split("-")[0])
    except ValueError:
        return None
    return start + 1


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                  formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--state", help="Only process this state (name or 2-letter code)")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    from app.database import SessionLocal
    from app import models
    from app.gpi_models import GpiCagExtraction, GpiIndicator, GpiIndicatorValue
    from app.models import State

    session = SessionLocal()

    # Identify our Governance indicators
    ind_by_code = {
        i.code: i for i in session.query(GpiIndicator).filter(
            GpiIndicator.code.in_(["G01", "G02", "G03", "G04"])
        ).all()
    }
    if not ind_by_code:
        raise SystemExit(
            "Governance indicators not seeded. Run: python scripts/gpi_seed.py"
        )

    # Get filter state if specified
    state_filter = None
    if args.state:
        st = session.query(State).filter(
            (State.name == args.state) | (State.code == args.state.upper())
        ).one_or_none()
        if not st:
            raise SystemExit(f"Unknown state: {args.state}")
        state_filter = st.id

    # Load CAG extractions
    q = session.query(GpiCagExtraction)
    if state_filter:
        q = q.filter(GpiCagExtraction.state_id == state_filter)
    all_extractions = q.all()

    # Dedupe by (state_id, audit_year). Some states have multiple SFAR
    # extractions for the same audit year (original + revised, or Gemini
    # misclassifications). Keep the extraction with the MOST populated
    # governance fields (audit paras + PAC + money value are our targets).
    def completeness(ex):
        fields = [ex.audit_paras_raised, ex.audit_paras_over_5_yrs,
                    ex.pac_recommendations_pending,
                    ex.money_value_observations_crore, ex.gsdp_current_crore]
        return sum(1 for v in fields if v is not None)

    dedupe: dict[tuple, "GpiCagExtraction"] = {}
    for ex in all_extractions:
        key = (ex.state_id, ex.audit_year)
        if key not in dedupe or completeness(ex) > completeness(dedupe[key]):
            dedupe[key] = ex
    extractions = list(dedupe.values())

    dropped = len(all_extractions) - len(extractions)
    print(f"CAG extractions total:     {len(all_extractions)}")
    print(f"After dedupe (per year):   {len(extractions)}")
    if dropped:
        print(f"Dropped duplicates:        {dropped}")
    print()

    counts = {
        "G01": {"inserted": 0, "updated": 0, "no_data": 0},
        "G02": {"inserted": 0, "updated": 0, "no_data": 0},
        "G03": {"inserted": 0, "updated": 0, "no_data": 0},
        "G04": {"inserted": 0, "updated": 0, "no_data": 0},
    }

    # Punjab sample tracker for a sanity-check printout at the end
    punjab_id = session.query(State.id).filter(State.code == "PB").scalar()
    punjab_samples = {}

    for ex in extractions:
        fy = audit_year_to_fiscal_year(ex.audit_year)
        if fy is None:
            continue

        # G01, G02, G03: direct integer counts
        #
        # Sanity floor — some SFARs return 0 for these when Gemini couldn't
        # locate the specific table (vs a real 0). Every state's SFAR raises
        # SOME audit paras and has SOME pending PAC actions, so 0 is almost
        # always an extraction miss. G02 (long-pending >5yr) is the exception:
        # a well-governed state can legitimately have 0.
        SANITY_MIN_NONZERO = {"G01": True, "G02": False, "G03": True}

        for code, source_val in [
            ("G01", ex.audit_paras_raised),
            ("G02", ex.audit_paras_over_5_yrs),
            ("G03", ex.pac_recommendations_pending),
        ]:
            if source_val is None:
                counts[code]["no_data"] += 1
                continue
            if SANITY_MIN_NONZERO.get(code) and source_val == 0:
                counts[code]["no_data"] += 1
                continue
            _upsert(session, ind_by_code[code].id, ex.state_id, fy,
                     float(source_val), ex, counts[code])
            if ex.state_id == punjab_id:
                punjab_samples.setdefault(code, {})[fy] = source_val

        # G04: ratio (money value crore / GSDP crore × 100)
        mv = ex.money_value_observations_crore
        gsdp = ex.gsdp_current_crore
        if mv is not None and gsdp not in (None, 0):
            ratio = round(100.0 * mv / gsdp, 4)
            _upsert(session, ind_by_code["G04"].id, ex.state_id, fy,
                     ratio, ex, counts["G04"])
            if ex.state_id == punjab_id:
                punjab_samples.setdefault("G04", {})[fy] = ratio
        else:
            counts["G04"]["no_data"] += 1

    if not args.dry_run:
        session.commit()
    else:
        session.rollback()

    print("═══════════════════════ Summary per indicator ══════════════════════")
    for code, c in counts.items():
        ind = ind_by_code[code]
        print(f"  {code} ({ind.name[:44]:<44s})  "
               f"ins={c['inserted']:>3}  upd={c['updated']:>3}  "
               f"no_data={c['no_data']:>3}")

    if punjab_samples:
        print()
        print("Punjab Governance values (chronological):")
        # Collect all years across G01-G04
        all_years = sorted({y for d in punjab_samples.values() for y in d.keys()})
        header = "  FY    " + " ".join(f"{c:>10s}" for c in ["G01", "G02", "G03", "G04"])
        print(header)
        for y in all_years:
            row = f"  FY{y}"
            for code in ["G01", "G02", "G03", "G04"]:
                v = punjab_samples.get(code, {}).get(y)
                row += f"  {v!s:>10s}"
            print(row)

    session.close()

    if args.dry_run:
        print("\n(dry run — no writes)")
    print()
    print("Next: python scripts/gpi_compute_scores.py")


def _upsert(session, ind_id, state_id, fy, val, cag_ex, ctr):
    """UPSERT one indicator value + bump counter."""
    from app.gpi_models import GpiIndicatorValue

    existing = session.query(GpiIndicatorValue).filter_by(
        indicator_id=ind_id, state_id=state_id, fiscal_year=fy,
    ).one_or_none()

    payload = {
        "raw_value":         val,
        "source_url":        cag_ex.source_url or "",
        "source_document":   f"CAG SFAR · audit_year {cag_ex.audit_year} · "
                                f"Report {cag_ex.report_no or '?'}",
        "extraction_method": "cag_extraction_wire",
        "staleness":         "current",
        "notes":             "Derived from gpi_cag_extractions",
        "extracted_at":      datetime.utcnow(),
    }

    if existing:
        for k, v in payload.items():
            setattr(existing, k, v)
        existing.normalized_value = None
        existing.national_rank = None
        ctr["updated"] += 1
    else:
        session.add(GpiIndicatorValue(
            indicator_id=ind_id, state_id=state_id, fiscal_year=fy,
            **payload,
        ))
        ctr["inserted"] += 1
    # Flush so subsequent same-key upserts in this transaction find the row
    # (avoids UNIQUE constraint failures when data has been de-duped upstream
    # but our own logic writes multiple rows to the same key).
    session.flush()


if __name__ == "__main__":
    main()
