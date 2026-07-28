"""Wire CAG SFAR revenue-side fields (from Phase 1 extraction) into the
Fiscal Efficiency pillar.

Mirrors gpi_ingest_governance.py — reads gpi_cag_extractions and populates
EF01 (Own Tax / GSDP) and EF02 (Revenue Receipts / GSDP) into indicator_values.

These fields were extracted by Gemini into gpi_cag_extractions but not yet
wired to any pillar. This script closes that loop.

Usage:
    python scripts/gpi_ingest_efficiency.py
    python scripts/gpi_ingest_efficiency.py --state Punjab --dry-run
"""
from __future__ import annotations
import argparse
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def audit_year_to_fiscal_year(audit_year: str) -> int | None:
    if not audit_year or "-" not in audit_year:
        return None
    try:
        return int(audit_year.split("-")[0]) + 1
    except ValueError:
        return None


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                  formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--state")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    from app.database import SessionLocal
    from app import models
    from app.gpi_models import GpiCagExtraction, GpiIndicator, GpiIndicatorValue
    from app.models import State

    session = SessionLocal()

    ind_by_code = {
        i.code: i for i in session.query(GpiIndicator).filter(
            GpiIndicator.code.in_(["EF01", "EF02"])
        ).all()
    }
    if not ind_by_code:
        raise SystemExit("Efficiency indicators not seeded — run gpi_seed.py")

    state_filter = None
    if args.state:
        st = session.query(State).filter(
            (State.name == args.state) | (State.code == args.state.upper())
        ).one_or_none()
        if not st:
            raise SystemExit(f"Unknown state: {args.state}")
        state_filter = st.id

    q = session.query(GpiCagExtraction)
    if state_filter:
        q = q.filter(GpiCagExtraction.state_id == state_filter)
    all_extractions = q.all()

    # Dedupe by (state_id, audit_year) — pick row with more non-null revenue fields
    def completeness(ex):
        return sum(1 for v in [ex.own_tax_revenue_pct_gsdp, ex.revenue_receipts_pct_gsdp]
                    if v is not None)

    dedupe = {}
    for ex in all_extractions:
        key = (ex.state_id, ex.audit_year)
        if key not in dedupe or completeness(ex) > completeness(dedupe[key]):
            dedupe[key] = ex
    extractions = list(dedupe.values())

    print(f"CAG extractions total:    {len(all_extractions)}")
    print(f"After dedupe (per year):  {len(extractions)}")
    print()

    counts = {"EF01": {"ins": 0, "upd": 0, "no_data": 0},
              "EF02": {"ins": 0, "upd": 0, "no_data": 0}}

    punjab_id = session.query(State.id).filter(State.code == "PB").scalar()
    punjab_samples = {}

    # Sanity floor — Own Tax below 0.5% GSDP is impossible for any Indian state
    # (Bihar's low is ~4%). Similarly Revenue Receipts < 5% is likely extraction miss.
    SANITY_MIN = {"EF01": 0.5, "EF02": 5.0}

    for ex in extractions:
        fy = audit_year_to_fiscal_year(ex.audit_year)
        if fy is None:
            continue

        for code, source_val in [
            ("EF01", ex.own_tax_revenue_pct_gsdp),
            ("EF02", ex.revenue_receipts_pct_gsdp),
        ]:
            if source_val is None or source_val < SANITY_MIN[code]:
                counts[code]["no_data"] += 1
                continue
            _upsert(session, ind_by_code[code].id, ex.state_id, fy,
                     float(source_val), ex, counts[code])
            if ex.state_id == punjab_id:
                punjab_samples.setdefault(code, {})[fy] = source_val

    if not args.dry_run:
        session.commit()
    else:
        session.rollback()

    print("═══════════════════════ Summary per indicator ══════════════════════")
    for code, c in counts.items():
        ind = ind_by_code[code]
        print(f"  {code} ({ind.name[:44]:<44s})  "
               f"ins={c['ins']:>3}  upd={c['upd']:>3}  no_data={c['no_data']:>3}")

    if punjab_samples:
        print()
        print("Punjab Efficiency values (chronological, % of GSDP):")
        all_years = sorted({y for d in punjab_samples.values() for y in d.keys()})
        print("  FY    " + " ".join(f"{c:>10s}" for c in ["EF01", "EF02"]))
        for y in all_years:
            row = f"  FY{y}"
            for code in ["EF01", "EF02"]:
                v = punjab_samples.get(code, {}).get(y)
                row += f"  {v if v is None else f'{v:.2f}%':>10s}"
            print(row)

    session.close()

    if args.dry_run:
        print("\n(dry run — no writes)")
    print()
    print("Next: python scripts/gpi_compute_scores.py")


def _upsert(session, ind_id, state_id, fy, val, cag_ex, ctr):
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
        ctr["upd"] += 1
    else:
        session.add(GpiIndicatorValue(
            indicator_id=ind_id, state_id=state_id, fiscal_year=fy,
            **payload,
        ))
        ctr["ins"] += 1
    session.flush()


if __name__ == "__main__":
    main()
