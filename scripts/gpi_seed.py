"""Seed the GPI reference tables (pillars, sources, indicators) into lokvani.db.

Idempotent: safe to run multiple times. Uses upsert by `code` field so re-running
after a definition edit updates existing rows in place (never duplicates).

Also runs Base.metadata.create_all() first so the tables exist if the DB was
created before GPI models landed.

Usage:
    python scripts/gpi_seed.py             # seed all
    python scripts/gpi_seed.py --dry-run   # show plan without writing
    python scripts/gpi_seed.py --verify    # verify DB matches definitions
"""
from __future__ import annotations
import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.database import SessionLocal, engine, Base
from app import models  # ensures gpi_models is imported → tables registered
from app.gpi_models import GpiPillar, GpiSource, GpiIndicator
from scripts.gpi_definitions import PILLARS, SOURCES, INDICATORS, validate


def ensure_tables():
    """Create GPI tables if they don't already exist. Non-destructive."""
    Base.metadata.create_all(bind=engine)


def _upsert(session, model, key_field: str, defaults: dict):
    """Upsert one row: find by unique code, update if exists else insert.
    Returns the row object."""
    existing = session.query(model).filter(
        getattr(model, key_field) == defaults[key_field]
    ).one_or_none()
    if existing:
        changed = []
        for k, v in defaults.items():
            if getattr(existing, k) != v:
                setattr(existing, k, v)
                changed.append(k)
        return existing, "updated" if changed else "unchanged"
    row = model(**defaults)
    session.add(row)
    return row, "inserted"


def seed(dry_run: bool = False):
    counts = {"pillars": {"inserted": 0, "updated": 0, "unchanged": 0},
              "sources":  {"inserted": 0, "updated": 0, "unchanged": 0},
              "indicators": {"inserted": 0, "updated": 0, "unchanged": 0}}

    session = SessionLocal()
    try:
        # ── Pillars ────────────────────────────────────────────────────────
        pillar_by_code = {}
        for p in PILLARS:
            row, action = _upsert(session, GpiPillar, "code", {
                "code":            p["code"],
                "name":            p["name"],
                "description":     p["description"],
                "default_weight":  p["default_weight"],
                "display_order":   p["display_order"],
            })
            counts["pillars"][action] += 1
            pillar_by_code[p["code"]] = row

        if not dry_run:
            session.flush()   # assigns IDs to any freshly-inserted pillars

        # ── Sources ────────────────────────────────────────────────────────
        source_by_code = {}
        for s in SOURCES:
            row, action = _upsert(session, GpiSource, "code", {
                "code":            s["code"],
                "name":            s["name"],
                "publisher":       s.get("publisher"),
                "url":             s.get("url"),
                "format":          s.get("format"),
                "refresh_cadence": s.get("refresh_cadence"),
                "notes":           s.get("notes"),
            })
            counts["sources"][action] += 1
            source_by_code[s["code"]] = row

        if not dry_run:
            session.flush()

        # ── Indicators ─────────────────────────────────────────────────────
        for i in INDICATORS:
            pillar = pillar_by_code[i["pillar_code"]]
            source = source_by_code.get(i.get("source_code")) if i.get("source_code") else None
            row, action = _upsert(session, GpiIndicator, "code", {
                "code":            i["code"],
                "pillar_id":       pillar.id,
                "source_id":       source.id if source else None,
                "name":            i["name"],
                "description":     i.get("description"),
                "unit":            i.get("unit"),
                "direction":       i["direction"],
                "cadence":         i.get("cadence"),
                "default_weight":  i.get("default_weight", 1.0),
                "display_order":   i.get("order"),
                "notes":           i.get("notes"),
            })
            counts["indicators"][action] += 1

        if dry_run:
            print("DRY RUN — no writes performed")
            session.rollback()
        else:
            session.commit()
    finally:
        session.close()

    return counts


def verify():
    session = SessionLocal()
    try:
        n_pillars    = session.query(GpiPillar).count()
        n_sources    = session.query(GpiSource).count()
        n_indicators = session.query(GpiIndicator).count()
        exp = (len(PILLARS), len(SOURCES), len(INDICATORS))
        got = (n_pillars, n_sources, n_indicators)
        print(f"Expected: {exp[0]} pillars, {exp[1]} sources, {exp[2]} indicators")
        print(f"Got:      {got[0]} pillars, {got[1]} sources, {got[2]} indicators")
        ok = exp == got
        print("\n✓ Verified" if ok else "\n✗ Mismatch — re-run seed")
        return ok
    finally:
        session.close()


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                  formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dry-run", action="store_true", help="Preview without writing")
    ap.add_argument("--verify", action="store_true", help="Verify DB matches definitions")
    args = ap.parse_args()

    errs = validate()
    if errs:
        print("Validation errors in gpi_definitions.py — fix before seeding:")
        for e in errs:
            print(f"  ✗ {e}")
        sys.exit(1)

    ensure_tables()

    if args.verify:
        ok = verify()
        sys.exit(0 if ok else 1)

    counts = seed(dry_run=args.dry_run)
    print()
    print("═══════════════════════ GPI Seed ═══════════════════════")
    for table, cnts in counts.items():
        print(f"  {table:<12s}  ins={cnts['inserted']:>3d}  upd={cnts['updated']:>3d}  unchanged={cnts['unchanged']:>3d}")
    print("═════════════════════════════════════════════════════════")


if __name__ == "__main__":
    main()
