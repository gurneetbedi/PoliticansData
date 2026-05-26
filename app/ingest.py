"""
Ingest scraped myneta data into the database.

Run with:
  python -m app.ingest punjab       # Punjab assembly (MLAs)
  python -m app.ingest punjab_ls    # Punjab Lok Sabha MPs

This is idempotent — re-running merges new data and updates existing records
based on (politician.myneta_candidate_id, election.id).
"""
import logging
import sys
from slugify import slugify

from app.database import Base, SessionLocal, engine
from app.models import (
    State, Party, Constituency, Election, Politician, ElectionAppearance
)
from app.scrapers.punjab import (
    scrape_all_punjab, scrape_candidate_detail, PUNJAB_CYCLES, WinnerRow,
)
from app.scrapers.punjab_ls import scrape_all_punjab_ls, LS_CYCLES, LSWinnerRow
from app.models import Asset, CriminalCase

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)


def get_or_create(session, model, defaults=None, **kwargs):
    instance = session.query(model).filter_by(**kwargs).first()
    if instance:
        return instance, False
    instance = model(**{**kwargs, **(defaults or {})})
    session.add(instance)
    session.flush()
    return instance, True


def ingest_punjab():
    """Scrape and ingest all four Punjab assembly election cycles."""
    Base.metadata.create_all(bind=engine)
    session = SessionLocal()

    try:
        # Ensure the Punjab state row exists
        state, _ = get_or_create(session, State, name="Punjab", defaults={"code": "PB"})

        # Pre-create the election rows
        cycle_to_election = {}
        for cycle in PUNJAB_CYCLES:
            election, _ = get_or_create(
                session, Election,
                year=cycle["year"], house="Assembly", state_id=state.id,
                defaults={"myneta_slug": cycle["slug"]},
            )
            cycle_to_election[cycle["year"]] = election

        scraped = scrape_all_punjab()

        total_appearances = 0
        for year, winners in scraped.items():
            election = cycle_to_election[year]
            log.info("Ingesting %d winners for Punjab %d", len(winners), year)

            for row in winners:
                _ingest_one(session, row, state, election)
                total_appearances += 1

        session.commit()
        log.info("Done. Total ElectionAppearances ingested: %d", total_appearances)

    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def _ingest_one(session, row: WinnerRow, state, election):
    # Party
    party, _ = get_or_create(session, Party, short_name=row.party)

    # Constituency (unique per state+house+name)
    constituency, _ = get_or_create(
        session, Constituency,
        name=row.constituency, state_id=state.id, house="Assembly",
    )

    # Politician — keyed on myneta_candidate_id so re-contesters merge
    politician = session.query(Politician).filter_by(
        myneta_candidate_id=row.candidate_id
    ).first()
    if not politician:
        # Generate a unique slug
        base_slug = slugify(row.name)[:200]
        slug = base_slug
        n = 1
        while session.query(Politician).filter_by(slug=slug).first():
            n += 1
            slug = f"{base_slug}-{n}"
        politician = Politician(
            name=row.name,
            slug=slug,
            myneta_candidate_id=row.candidate_id,
        )
        session.add(politician)
        session.flush()

    # ElectionAppearance — one per (politician, election)
    appearance = session.query(ElectionAppearance).filter_by(
        politician_id=politician.id, election_id=election.id
    ).first()
    if not appearance:
        appearance = ElectionAppearance(
            politician_id=politician.id,
            election_id=election.id,
        )
        session.add(appearance)

    appearance.constituency_id = constituency.id
    appearance.party_id = party.id
    appearance.education = row.education
    appearance.total_assets_inr = row.total_assets_inr
    appearance.total_liabilities_inr = row.total_liabilities_inr
    appearance.criminal_cases_count = row.criminal_cases
    appearance.won = True   # winners list only
    appearance.source_url = row.detail_url


def ingest_punjab_ls():
    """Scrape and ingest Punjab Lok Sabha MPs across all available LS cycles."""
    Base.metadata.create_all(bind=engine)
    session = SessionLocal()
    try:
        state, _ = get_or_create(session, State, name="Punjab", defaults={"code": "PB"})

        cycle_to_election = {}
        for cycle in LS_CYCLES:
            election, _ = get_or_create(
                session, Election,
                year=cycle["year"], house="LokSabha", state_id=state.id,
                defaults={"myneta_slug": cycle["slug"]},
            )
            cycle_to_election[cycle["year"]] = election

        scraped = scrape_all_punjab_ls()

        total = 0
        for year, winners in scraped.items():
            election = cycle_to_election[year]
            log.info("Ingesting %d Punjab LS winners for %d", len(winners), year)
            for row in winners:
                _ingest_ls_one(session, row, state, election)
                total += 1

        session.commit()
        log.info("Done. Total LS ElectionAppearances ingested: %d", total)
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def _ingest_ls_one(session, row: LSWinnerRow, state, election):
    party, _ = get_or_create(session, Party, short_name=row.party)
    constituency, _ = get_or_create(
        session, Constituency,
        name=row.constituency, state_id=state.id, house="LokSabha",
    )

    politician = session.query(Politician).filter_by(
        myneta_candidate_id=row.candidate_id
    ).first()
    if not politician:
        base_slug = slugify(row.name)[:200]
        slug = base_slug
        n = 1
        while session.query(Politician).filter_by(slug=slug).first():
            n += 1
            slug = f"{base_slug}-{n}"
        politician = Politician(
            name=row.name, slug=slug, myneta_candidate_id=row.candidate_id,
        )
        session.add(politician)
        session.flush()

    appearance = session.query(ElectionAppearance).filter_by(
        politician_id=politician.id, election_id=election.id
    ).first()
    if not appearance:
        appearance = ElectionAppearance(
            politician_id=politician.id, election_id=election.id,
        )
        session.add(appearance)

    appearance.constituency_id = constituency.id
    appearance.party_id = party.id
    appearance.education = row.education
    appearance.total_assets_inr = row.total_assets_inr
    appearance.total_liabilities_inr = row.total_liabilities_inr
    appearance.criminal_cases_count = row.criminal_cases
    appearance.won = True
    appearance.source_url = row.detail_url


def ingest_punjab_detail():
    """
    Enrich each politician with details from their candidate page on myneta:
    age, profession, individual cases, asset breakdown by sub-category.

    This is the slow scrape — one fetch per politician per cycle they appeared
    in. With ~300 politicians × 2s rate limit ≈ 10 minutes for Punjab.
    Idempotent — re-running updates existing rows.
    """
    Base.metadata.create_all(bind=engine)
    session = SessionLocal()

    # Defensive cap. SQLite INTEGER is signed 64-bit (max ~9.2e18) but Python
    # ints are unbounded; any rupee value beyond ₹10,000 Cr is almost certainly
    # a parser mishap (survey/plot/account number), so we clamp it to None.
    SAFE_CAP = 10_000_000_000_000  # ₹10,000 Cr

    def safe_inr(v):
        if v is None:
            return None
        try:
            v = int(v)
        except (TypeError, ValueError):
            return None
        return v if 0 <= v <= SAFE_CAP else None

    try:
        appearances = session.query(ElectionAppearance).all()
        log.info("Enriching detail for %d election appearances", len(appearances))

        skipped_oversize = 0
        for i, app in enumerate(appearances, 1):
            if not app.source_url:
                continue
            try:
                detail = scrape_candidate_detail(app.source_url, app.politician.myneta_candidate_id or 0)
            except Exception as e:
                log.warning("Detail scrape failed for %s: %s", app.source_url, e)
                continue

            # Update politician-level fields (age, profession)
            if detail.age and not app.politician.age:
                app.politician.age = detail.age
            if detail.profession and not app.politician.profession:
                app.politician.profession = detail.profession

            # Update appearance-level fields — clamp every numeric write
            mov = safe_inr(detail.movable_total_inr)
            if mov:
                app.movable_assets_inr = mov
            imm = safe_inr(detail.immovable_total_inr)
            if imm:
                app.immovable_assets_inr = imm
            if detail.serious_cases:
                app.serious_cases_count = detail.serious_cases

            # Replace asset/case rows for this appearance with the freshly scraped set
            if detail.assets:
                session.query(Asset).filter_by(appearance_id=app.id).delete()
                for a in detail.assets:
                    v = safe_inr(a.get("value_inr"))
                    if v is None or v == 0:
                        skipped_oversize += 1
                        continue
                    session.add(Asset(
                        appearance_id=app.id,
                        category=a["category"],
                        subcategory=a["subcategory"],
                        value_inr=v,
                    ))

            if detail.cases:
                session.query(CriminalCase).filter_by(appearance_id=app.id).delete()
                for c in detail.cases:
                    session.add(CriminalCase(
                        appearance_id=app.id,
                        ipc_sections=c.get("ipc_sections", ""),
                        description=c.get("description", ""),
                        status=c.get("status", "pending"),
                    ))

            # Commit per-batch with rollback safety so one bad row can't lose 25 good ones
            if i % 25 == 0:
                try:
                    session.commit()
                    log.info("  ...%d/%d processed", i, len(appearances))
                except Exception as e:
                    session.rollback()
                    log.warning("  batch %d-%d failed (%s) — rolled back and continuing", i-25, i, e)

        try:
            session.commit()
        except Exception as e:
            session.rollback()
            log.warning("Final commit failed: %s", e)
        log.info("Done. Detail enrichment complete. Skipped %d oversized asset rows.", skipped_oversize)
    finally:
        session.close()


def main():
    if len(sys.argv) < 2:
        print("Usage:")
        print("  python -m app.ingest punjab          # Punjab MLAs (winners list)")
        print("  python -m app.ingest punjab_ls       # Punjab Lok Sabha MPs")
        print("  python -m app.ingest punjab_detail   # Enrich each politician's profile")
        sys.exit(1)
    target = sys.argv[1]
    if target == "punjab":
        ingest_punjab()
    elif target == "punjab_ls":
        ingest_punjab_ls()
    elif target == "punjab_detail":
        ingest_punjab_detail()
    else:
        print(f"Unknown target: {target}")
        sys.exit(1)


if __name__ == "__main__":
    main()
