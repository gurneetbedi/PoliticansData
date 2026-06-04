"""
Ingest scraped myneta data into the database.

Run with:
  python -m app.ingest punjab       # Punjab assembly (MLAs)
  python -m app.ingest punjab_ls    # Punjab Lok Sabha MPs

This is idempotent — re-running merges new data and updates existing records
based on (politician.myneta_candidate_id, election.id).

IMPORTANT: ingest writes thousands of rows interspersed with 2-second sleeps
for politeness with myneta. Against a remote Postgres (e.g. Neon free tier)
the connection gets dropped during the long fetch gaps. Always scrape into
local SQLite, then push to Neon with scripts/sqlite_to_postgres.py once
done. To override the guard (rarely needed), set ALLOW_REMOTE_INGEST=1.
"""
import logging
import os
import sys
from slugify import slugify
# from sqlalchemy.orm import joinedload


# ----- Safety guard ----------------------------------------------------------
# If the user has DATABASE_URL pointing at a remote Postgres host, refuse to
# run unless they explicitly opt in. This prevents the foot-gun where a
# previously-exported DATABASE_URL from a loader/migration session leaks into
# a subsequent multi-hour scrape and tries to hammer the production DB.
_DB_URL_NOW = os.getenv("DATABASE_URL", "")
if (
    _DB_URL_NOW.startswith(("postgres://", "postgresql://"))
    and os.getenv("ALLOW_REMOTE_INGEST") != "1"
):
    print(
        "\nERROR: DATABASE_URL is pointing at a remote Postgres database:\n"
        f"  {_DB_URL_NOW.split('@')[-1]}\n"
        "\nIngest is meant to run against local SQLite (./politrack.db). Scraping\n"
        "into a remote DB is slow and crashes on idle-connection drops.\n"
        "\nFix:\n"
        "  1.  unset DATABASE_URL\n"
        "  2.  python -m app.ingest <target>       # writes to local SQLite\n"
        "  3.  When the scrape finishes, push to Neon with:\n"
        '       export DATABASE_URL="postgresql://...neon.tech/...?sslmode=require"\n'
        "       python scripts/sqlite_to_postgres.py --reset\n"
        "\nTo override this guard anyway (NOT recommended), set:\n"
        "  export ALLOW_REMOTE_INGEST=1\n",
        file=sys.stderr,
    )
    sys.exit(2)


from app.database import Base, SessionLocal, engine
from app.models import (
    State, Party, Constituency, Election, Politician, ElectionAppearance
)
from app.scrapers.punjab import (
    scrape_all_punjab, scrape_all_punjab_candidates,
    scrape_candidate_detail, PUNJAB_CYCLES, WinnerRow,
)
from app.scrapers.punjab_ls import scrape_all_punjab_ls, LS_CYCLES, LSWinnerRow
from app.scrapers.bihar import scrape_all_bihar, scrape_all_bihar_candidates, BIHAR_CYCLES
from app.scrapers.goa    import scrape_all_goa,    scrape_all_goa_candidates,    GOA_CYCLES
from app.scrapers.sikkim import scrape_all_sikkim, scrape_all_sikkim_candidates, SIKKIM_CYCLES
from app.scrapers.delhi  import scrape_all_delhi,  scrape_all_delhi_candidates,  DELHI_CYCLES
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


def _ingest_one(session, row: WinnerRow, state, election, won: bool = True):
    """Insert/update one ElectionAppearance row. `won` indicates whether
    the candidate won this particular election (True for the winners list,
    False for losers from the all-candidates scrape)."""
    party, _ = get_or_create(session, Party, short_name=row.party)
    constituency, _ = get_or_create(
        session, Constituency,
        name=row.constituency, state_id=state.id, house="Assembly",
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
        politician = Politician(name=row.name, slug=slug, myneta_candidate_id=row.candidate_id)
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
    # Don't downgrade an existing winner to a loser when this is the all-candidates pass
    if not appearance.won:
        appearance.won = won
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


def ingest_detail(state_name: str | None = None, winners_only: bool = False,
                  force: bool = False):
    """
    Enrich politicians with per-candidate detail from myneta — age, profession,
    individual cases, asset breakdown.

    state_name    — only process politicians from this state (default: all states)
    winners_only  — skip losing candidates (huge speedup; default False)
    force         — re-fetch even if appearance already has Asset rows (default False)

    By default, appearances that already have asset data are skipped. This makes
    resuming after Ctrl+C effectively instant — only the unprocessed appearances
    get fetched. Pass `force=True` to re-scrape everything (e.g. after a parser fix).
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
        # Scope to a single state when requested so `bihar_detail` doesn't
        # also re-scrape every Punjab politician.
        q = session.query(ElectionAppearance)
        if state_name:
            q = (q.join(Election, ElectionAppearance.election_id == Election.id)
                  .join(State, Election.state_id == State.id)
                  .filter(State.name == state_name))
        if winners_only:
            q = q.filter(ElectionAppearance.won.is_(True))
        appearances = q.all()

        # Skip appearances that already have asset data unless force=True.
        # This makes resuming after Ctrl+C essentially free.
        already_done: set[int] = set()
        if not force:
            already_done = {
                aid for (aid,) in session.query(Asset.appearance_id).distinct().all()
            }
        target = [a for a in appearances if force or a.id not in already_done]

        log.info(
            "Enriching %d/%d appearances%s%s — skipping %d already enriched",
            len(target), len(appearances),
            f" in {state_name}" if state_name else "",
            " (winners only)" if winners_only else "",
            len(appearances) - len(target),
        )

        skipped_oversize = 0
        for i, app in enumerate(target, 1):
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
                    log.info("  ...%d/%d processed", i, len(target))
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


def ingest_punjab_all():
    """Scrape EVERY candidate (winner + loser) across Punjab cycles."""
    Base.metadata.create_all(bind=engine)
    session = SessionLocal()
    try:
        state, _ = get_or_create(session, State, name="Punjab", defaults={"code": "PB"})
        cycle_to_election = {}
        for cycle in PUNJAB_CYCLES:
            election, _ = get_or_create(
                session, Election,
                year=cycle["year"], house="Assembly", state_id=state.id,
                defaults={"myneta_slug": cycle["slug"]},
            )
            cycle_to_election[cycle["year"]] = election

        # First pass: ensure winners are marked won=True
        winners_by_cycle = scrape_all_punjab()
        winning_ids_by_cycle: dict[int, set[int]] = {}
        for year, winners in winners_by_cycle.items():
            winning_ids_by_cycle[year] = {w.candidate_id for w in winners}
            for row in winners:
                _ingest_one(session, row, state, cycle_to_election[year], won=True)
        session.commit()
        log.info("Winners pass complete.")

        # Second pass: scrape all candidates, mark won based on winner set
        all_by_cycle = scrape_all_punjab_candidates()
        total = 0
        for year, all_rows in all_by_cycle.items():
            winners_set = winning_ids_by_cycle.get(year, set())
            log.info("Ingesting %d total candidates for Punjab %d (%d winners + %d losers)",
                     len(all_rows), year, len(winners_set), len(all_rows) - len(winners_set))
            for i, row in enumerate(all_rows, 1):
                _ingest_one(session, row, state, cycle_to_election[year],
                            won=(row.candidate_id in winners_set))
                total += 1
                if i % 100 == 0:
                    session.commit()
                    log.info("  ...%d/%d processed for %d", i, len(all_rows), year)
            session.commit()

        log.info("Done. Total Punjab candidate appearances ingested: %d", total)
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def ingest_bihar():
    """Scrape Bihar Assembly winners across all available cycles."""
    Base.metadata.create_all(bind=engine)
    session = SessionLocal()
    try:
        state, _ = get_or_create(session, State, name="Bihar", defaults={"code": "BR"})
        cycle_to_election = {}
        for cycle in BIHAR_CYCLES:
            election, _ = get_or_create(
                session, Election,
                year=cycle["year"], house="Assembly", state_id=state.id,
                defaults={"myneta_slug": cycle["slug"]},
            )
            cycle_to_election[cycle["year"]] = election

        scraped = scrape_all_bihar()
        total = 0
        for year, winners in scraped.items():
            election = cycle_to_election[year]
            log.info("Ingesting %d Bihar winners for %d", len(winners), year)
            for row in winners:
                _ingest_one(session, row, state, election, won=True)
                total += 1
        session.commit()
        log.info("Done. Total Bihar winner appearances ingested: %d", total)
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def ingest_bihar_all():
    """Scrape EVERY candidate (winner + loser) across Bihar cycles."""
    Base.metadata.create_all(bind=engine)
    session = SessionLocal()
    try:
        state, _ = get_or_create(session, State, name="Bihar", defaults={"code": "BR"})
        cycle_to_election = {}
        for cycle in BIHAR_CYCLES:
            election, _ = get_or_create(
                session, Election,
                year=cycle["year"], house="Assembly", state_id=state.id,
                defaults={"myneta_slug": cycle["slug"]},
            )
            cycle_to_election[cycle["year"]] = election

        # Winners pass
        winners_by_cycle = scrape_all_bihar()
        winning_ids_by_cycle: dict[int, set[int]] = {}
        for year, winners in winners_by_cycle.items():
            winning_ids_by_cycle[year] = {w.candidate_id for w in winners}
            for row in winners:
                _ingest_one(session, row, state, cycle_to_election[year], won=True)
        session.commit()
        log.info("Bihar winners pass complete.")

        # All-candidates pass
        all_by_cycle = scrape_all_bihar_candidates()
        total = 0
        for year, all_rows in all_by_cycle.items():
            winners_set = winning_ids_by_cycle.get(year, set())
            log.info("Ingesting %d total candidates for Bihar %d (%d winners + %d losers)",
                     len(all_rows), year, len(winners_set), len(all_rows) - len(winners_set))
            for i, row in enumerate(all_rows, 1):
                _ingest_one(session, row, state, cycle_to_election[year],
                            won=(row.candidate_id in winners_set))
                total += 1
                if i % 100 == 0:
                    session.commit()
                    log.info("  ...%d/%d processed for %d", i, len(all_rows), year)
            session.commit()
        log.info("Done. Total Bihar candidate appearances ingested: %d", total)
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def ingest_punjab_detail():
    """Winners-only detail enrichment for Punjab — fast (~10 min)."""
    ingest_detail(state_name="Punjab", winners_only=True)


def ingest_punjab_detail_all():
    """All-candidates detail enrichment for Punjab — slow (~hours)."""
    ingest_detail(state_name="Punjab", winners_only=False)


def ingest_bihar_detail():
    """Winners-only detail enrichment for Bihar — fast (~15 min)."""
    ingest_detail(state_name="Bihar", winners_only=True)


def ingest_bihar_detail_all():
    """All-candidates detail enrichment for Bihar — slow (~hours)."""
    ingest_detail(state_name="Bihar", winners_only=False)


def ingest_goa():
    """Scrape Goa Assembly winners across all available cycles."""
    Base.metadata.create_all(bind=engine)
    session = SessionLocal()
    try:
        state, _ = get_or_create(session, State, name="Goa", defaults={"code": "GA"})
        cycle_to_election = {}
        for cycle in GOA_CYCLES:
            election, _ = get_or_create(
                session, Election,
                year=cycle["year"], house="Assembly", state_id=state.id,
                defaults={"myneta_slug": cycle["slug"]},
            )
            cycle_to_election[cycle["year"]] = election
        scraped = scrape_all_goa()
        total = 0
        for year, winners in scraped.items():
            election = cycle_to_election[year]
            log.info("Ingesting %d Goa winners for %d", len(winners), year)
            for row in winners:
                _ingest_one(session, row, state, election, won=True)
                total += 1
        session.commit()
        log.info("Done. Total Goa winner appearances ingested: %d", total)
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def ingest_goa_all():
    """Scrape EVERY Goa candidate (winners + losers) across all cycles."""
    Base.metadata.create_all(bind=engine)
    session = SessionLocal()
    try:
        state, _ = get_or_create(session, State, name="Goa", defaults={"code": "GA"})
        cycle_to_election = {}
        for cycle in GOA_CYCLES:
            election, _ = get_or_create(
                session, Election,
                year=cycle["year"], house="Assembly", state_id=state.id,
                defaults={"myneta_slug": cycle["slug"]},
            )
            cycle_to_election[cycle["year"]] = election
        winners_by_cycle = scrape_all_goa()
        winning_ids_by_cycle: dict[int, set[int]] = {}
        for year, winners in winners_by_cycle.items():
            winning_ids_by_cycle[year] = {w.candidate_id for w in winners}
            for row in winners:
                _ingest_one(session, row, state, cycle_to_election[year], won=True)
        session.commit()
        log.info("Goa winners pass complete.")
        all_by_cycle = scrape_all_goa_candidates()
        total = 0
        for year, all_rows in all_by_cycle.items():
            winners_set = winning_ids_by_cycle.get(year, set())
            log.info("Ingesting %d total candidates for Goa %d (%d winners + %d losers)",
                     len(all_rows), year, len(winners_set), len(all_rows) - len(winners_set))
            for i, row in enumerate(all_rows, 1):
                _ingest_one(session, row, state, cycle_to_election[year],
                            won=(row.candidate_id in winners_set))
                total += 1
                if i % 100 == 0:
                    session.commit()
                    log.info("  ...%d/%d processed for %d", i, len(all_rows), year)
            session.commit()
        log.info("Done. Total Goa candidate appearances ingested: %d", total)
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def ingest_goa_detail():
    """Winners-only detail enrichment for Goa — very fast (~3 min)."""
    ingest_detail(state_name="Goa", winners_only=True)


def ingest_goa_detail_all():
    """All-candidates detail enrichment for Goa."""
    ingest_detail(state_name="Goa", winners_only=False)


# ============================================================================
# Sikkim
# ============================================================================
def _ingest_state_winners(state_name: str, state_code: str, cycles, scrape_fn):
    """Generic winners-only ingest used by both new states."""
    Base.metadata.create_all(bind=engine)
    session = SessionLocal()
    try:
        state, _ = get_or_create(session, State, name=state_name,
                                 defaults={"code": state_code})
        cycle_to_election = {}
        for cycle in cycles:
            election, _ = get_or_create(
                session, Election,
                year=cycle["year"], house="Assembly", state_id=state.id,
                defaults={"myneta_slug": cycle["slug"]},
            )
            cycle_to_election[cycle["year"]] = election
        scraped = scrape_fn()
        total = 0
        for year, winners in scraped.items():
            log.info("Ingesting %d %s winners for %d", len(winners), state_name, year)
            for row in winners:
                _ingest_one(session, row, state, cycle_to_election[year], won=True)
                total += 1
        session.commit()
        log.info("Done. Total %s winner appearances ingested: %d", state_name, total)
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def _ingest_state_all_candidates(state_name: str, state_code: str, cycles,
                                  winners_fn, all_fn):
    """Generic winners + losers ingest used by both new states."""
    Base.metadata.create_all(bind=engine)
    session = SessionLocal()
    try:
        state, _ = get_or_create(session, State, name=state_name,
                                 defaults={"code": state_code})
        cycle_to_election = {}
        for cycle in cycles:
            election, _ = get_or_create(
                session, Election,
                year=cycle["year"], house="Assembly", state_id=state.id,
                defaults={"myneta_slug": cycle["slug"]},
            )
            cycle_to_election[cycle["year"]] = election
        winners_by_cycle = winners_fn()
        winning_ids_by_cycle: dict[int, set[int]] = {}
        for year, winners in winners_by_cycle.items():
            winning_ids_by_cycle[year] = {w.candidate_id for w in winners}
            for row in winners:
                _ingest_one(session, row, state, cycle_to_election[year], won=True)
        session.commit()
        log.info("%s winners pass complete.", state_name)
        all_by_cycle = all_fn()
        total = 0
        for year, all_rows in all_by_cycle.items():
            winners_set = winning_ids_by_cycle.get(year, set())
            log.info("Ingesting %d total candidates for %s %d (%d winners + %d losers)",
                     len(all_rows), state_name, year, len(winners_set),
                     len(all_rows) - len(winners_set))
            for i, row in enumerate(all_rows, 1):
                _ingest_one(session, row, state, cycle_to_election[year],
                            won=(row.candidate_id in winners_set))
                total += 1
                if i % 100 == 0:
                    session.commit()
                    log.info("  ...%d/%d processed for %d", i, len(all_rows), year)
            session.commit()
        log.info("Done. Total %s candidate appearances ingested: %d", state_name, total)
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def ingest_sikkim():
    """Scrape Sikkim Assembly winners across all cycles (~5 minutes at 2s rate)."""
    _ingest_state_winners("Sikkim", "SK", SIKKIM_CYCLES, scrape_all_sikkim)


def ingest_sikkim_all():
    """Scrape EVERY Sikkim candidate across all cycles (~30 min)."""
    _ingest_state_all_candidates("Sikkim", "SK", SIKKIM_CYCLES,
                                  scrape_all_sikkim, scrape_all_sikkim_candidates)


def ingest_sikkim_detail():
    """Winners-only detail enrichment for Sikkim — very fast (~2 min)."""
    ingest_detail(state_name="Sikkim", winners_only=True)


def ingest_sikkim_detail_all():
    """All-candidates detail enrichment for Sikkim (~20 min)."""
    ingest_detail(state_name="Sikkim", winners_only=False)


# ============================================================================
# Delhi (NCT)
# ============================================================================
def ingest_delhi():
    """Scrape Delhi Assembly winners across all cycles (~12 minutes)."""
    _ingest_state_winners("Delhi", "DL", DELHI_CYCLES, scrape_all_delhi)


def ingest_delhi_all():
    """Scrape EVERY Delhi candidate across all cycles (~3 hours)."""
    _ingest_state_all_candidates("Delhi", "DL", DELHI_CYCLES,
                                  scrape_all_delhi, scrape_all_delhi_candidates)


def ingest_delhi_detail():
    """Winners-only detail enrichment for Delhi (~12 min)."""
    ingest_detail(state_name="Delhi", winners_only=True)


def ingest_delhi_detail_all():
    """All-candidates detail enrichment for Delhi (~2 hours)."""
    ingest_detail(state_name="Delhi", winners_only=False)


def main():
    if len(sys.argv) < 2:
        print("Usage:")
        print("  python -m app.ingest punjab           # Punjab MLAs (winners only — fast)")
        print("  python -m app.ingest punjab_all       # Punjab MLAs (all candidates inc. losers)")
        print("  python -m app.ingest punjab_ls        # Punjab Lok Sabha MPs")
        print("  python -m app.ingest punjab_detail    # Enrich Punjab politicians (assets, cases)")
        print("  python -m app.ingest bihar            # Bihar MLAs (winners only)")
        print("  python -m app.ingest bihar_all        # Bihar MLAs (all candidates)")
        print("  python -m app.ingest bihar_detail     # Enrich Bihar politicians (assets, cases)")
        sys.exit(1)
    target = sys.argv[1]
    fn = {
        "punjab":            ingest_punjab,
        "punjab_all":        ingest_punjab_all,
        "punjab_ls":         ingest_punjab_ls,
        "punjab_detail":     ingest_punjab_detail,          # winners-only (fast)
        "punjab_detail_all": ingest_punjab_detail_all,      # all candidates (slow)
        "bihar":             ingest_bihar,
        "bihar_all":         ingest_bihar_all,
        "bihar_detail":      ingest_bihar_detail,           # winners-only (fast)
        "bihar_detail_all":  ingest_bihar_detail_all,       # all candidates (slow)
        "goa":               ingest_goa,
        "goa_all":           ingest_goa_all,
        "goa_detail":        ingest_goa_detail,
        "goa_detail_all":    ingest_goa_detail_all,
        "sikkim":            ingest_sikkim,
        "sikkim_all":        ingest_sikkim_all,
        "sikkim_detail":     ingest_sikkim_detail,
        "sikkim_detail_all": ingest_sikkim_detail_all,
        "delhi":             ingest_delhi,
        "delhi_all":         ingest_delhi_all,
        "delhi_detail":      ingest_delhi_detail,
        "delhi_detail_all":  ingest_delhi_detail_all,
    }.get(target)
    if not fn:
        print(f"Unknown target: {target}")
        sys.exit(1)
    fn()


if __name__ == "__main__":
    main()
