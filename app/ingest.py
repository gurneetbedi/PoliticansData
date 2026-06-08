"""
Ingest scraped myneta data into the database.

Run with:
  python -m app.ingest punjab       # Punjab assembly (MLAs)
  python -m app.ingest punjab_ls    # Punjab Lok Sabha MPs

This is idempotent — re-running merges new data and updates existing records
based on (politician.myneta_candidate_id, election.id). The election scope on
the dedup key is critical: myneta numbers candidates per-election (cand_id=2
in punjab2022 is a different person from cand_id=2 in Delhi2025). Without the
election scope, ingesting a new state silently merges its candidates into
existing politician rows from other states. See scripts/split_merged_politicians.py
for the cleanup that fixes already-polluted data.

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
from app.scrapers.small_states import (
    scrape_all_puducherry,   scrape_all_puducherry_candidates,   PUDUCHERRY_CYCLES,
    scrape_all_mizoram,      scrape_all_mizoram_candidates,      MIZORAM_CYCLES,
    scrape_all_manipur,      scrape_all_manipur_candidates,      MANIPUR_CYCLES,
    scrape_all_meghalaya,    scrape_all_meghalaya_candidates,    MEGHALAYA_CYCLES,
    scrape_all_nagaland,     scrape_all_nagaland_candidates,     NAGALAND_CYCLES,
    scrape_all_tripura,      scrape_all_tripura_candidates,      TRIPURA_CYCLES,
    scrape_all_arunachal,    scrape_all_arunachal_candidates,    ARUNACHAL_CYCLES,
    scrape_all_himachal,     scrape_all_himachal_candidates,     HIMACHAL_CYCLES,
    scrape_all_uttarakhand,  scrape_all_uttarakhand_candidates,  UTTARAKHAND_CYCLES,
    scrape_all_jharkhand,    scrape_all_jharkhand_candidates,    JHARKHAND_CYCLES,
    scrape_all_haryana,      scrape_all_haryana_candidates,      HARYANA_CYCLES,
    scrape_all_chhattisgarh, scrape_all_chhattisgarh_candidates, CHHATTISGARH_CYCLES,
    scrape_all_jk,           scrape_all_jk_candidates,           JK_CYCLES,
    scrape_all_telangana,    scrape_all_telangana_candidates,    TELANGANA_CYCLES,
    scrape_all_assam,        scrape_all_assam_candidates,        ASSAM_CYCLES,
)
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
    False for losers from the all-candidates scrape).

    Dedup is keyed on (election_id, myneta_candidate_id), NOT on candidate_id
    alone. myneta numbers candidates per-election, so cand_id=2 in punjab2022
    and cand_id=2 in Delhi2025 are different real people. Using candidate_id
    alone would merge them into one polluted record (the bug that motivated
    scripts/split_merged_politicians.py)."""
    party, _ = get_or_create(session, Party, short_name=row.party)
    constituency, _ = get_or_create(
        session, Constituency,
        name=row.constituency, state_id=state.id, house="Assembly",
    )

    # Election-scoped dedup: only match a politician who already has an
    # appearance in THIS specific election. New (election, cand_id) pairs
    # always allocate a fresh Politician row.
    politician = (
        session.query(Politician)
        .join(ElectionAppearance, ElectionAppearance.politician_id == Politician.id)
        .filter(Politician.myneta_candidate_id == row.candidate_id)
        .filter(ElectionAppearance.election_id == election.id)
        .first()
    )
    if not politician:
        # Slug format matches the splitter: <name>-<election_slug>-<cand_id>.
        # That makes URLs stable across re-ingests and avoids slug collisions
        # between (e.g.) punjab2022 cand_id=2 and Delhi2025 cand_id=2.
        base_slug = slugify(f"{row.name}-{election.myneta_slug}-{row.candidate_id}")[:240]
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
    """Lok Sabha equivalent of _ingest_one. Same per-election dedup rule:
    candidate_id is per-election on myneta, so we must scope by election_id
    to avoid merging different people into one politician row."""
    party, _ = get_or_create(session, Party, short_name=row.party)
    constituency, _ = get_or_create(
        session, Constituency,
        name=row.constituency, state_id=state.id, house="LokSabha",
    )

    politician = (
        session.query(Politician)
        .join(ElectionAppearance, ElectionAppearance.politician_id == Politician.id)
        .filter(Politician.myneta_candidate_id == row.candidate_id)
        .filter(ElectionAppearance.election_id == election.id)
        .first()
    )
    if not politician:
        base_slug = slugify(f"{row.name}-{election.myneta_slug}-{row.candidate_id}")[:240]
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


# ============================================================================
# Small-state batch — generated programmatically. Each state gets four CLI
# targets (winners, all, detail, detail_all) just like the older per-state
# helpers above. Wall-clock estimates assume the standard 2-second politeness
# rate limit and a fresh cache.
# ============================================================================

_SMALL_STATES = [
    # (CLI key,      Display name,         Code,  CYCLES,             scrape_all_winners,    scrape_all_candidates)
    ("puducherry",  "Puducherry",          "PY",  PUDUCHERRY_CYCLES,  scrape_all_puducherry,  scrape_all_puducherry_candidates),
    ("mizoram",     "Mizoram",             "MZ",  MIZORAM_CYCLES,     scrape_all_mizoram,     scrape_all_mizoram_candidates),
    ("manipur",     "Manipur",             "MN",  MANIPUR_CYCLES,     scrape_all_manipur,     scrape_all_manipur_candidates),
    ("meghalaya",   "Meghalaya",           "ML",  MEGHALAYA_CYCLES,   scrape_all_meghalaya,   scrape_all_meghalaya_candidates),
    ("nagaland",    "Nagaland",            "NL",  NAGALAND_CYCLES,    scrape_all_nagaland,    scrape_all_nagaland_candidates),
    ("tripura",     "Tripura",             "TR",  TRIPURA_CYCLES,     scrape_all_tripura,     scrape_all_tripura_candidates),
    ("arunachal",   "Arunachal Pradesh",   "AR",  ARUNACHAL_CYCLES,   scrape_all_arunachal,   scrape_all_arunachal_candidates),
    ("himachal",     "Himachal Pradesh", "HP", HIMACHAL_CYCLES,     scrape_all_himachal,     scrape_all_himachal_candidates),
    ("uttarakhand",  "Uttarakhand",      "UK", UTTARAKHAND_CYCLES,  scrape_all_uttarakhand,  scrape_all_uttarakhand_candidates),
    # Next-smallest tier (81-90 seats)
    ("jharkhand",    "Jharkhand",        "JH", JHARKHAND_CYCLES,    scrape_all_jharkhand,    scrape_all_jharkhand_candidates),
    ("haryana",      "Haryana",          "HR", HARYANA_CYCLES,      scrape_all_haryana,      scrape_all_haryana_candidates),
    ("chhattisgarh", "Chhattisgarh",     "CG", CHHATTISGARH_CYCLES, scrape_all_chhattisgarh, scrape_all_chhattisgarh_candidates),
    # Zone-balancing batch (90-126 seats)
    ("jk",           "Jammu and Kashmir","JK", JK_CYCLES,           scrape_all_jk,           scrape_all_jk_candidates),
    ("telangana",    "Telangana",        "TG", TELANGANA_CYCLES,    scrape_all_telangana,    scrape_all_telangana_candidates),
    ("assam",        "Assam",            "AS", ASSAM_CYCLES,        scrape_all_assam,        scrape_all_assam_candidates),
]

# Bind one ingest function per (state, mode) into module globals so the CLI
# dispatcher can look them up by name like `ingest_mizoram_all`. Mirrors the
# manual goa/sikkim/delhi pattern but without 36 hand-written functions.
_SMALL_STATE_DISPATCH = {}
for _key, _name, _code, _cycles, _winners_fn, _all_fn in _SMALL_STATES:
    def _make_winners(name, code, cycles, winners_fn):
        def _ingest(): _ingest_state_winners(name, code, cycles, winners_fn)
        _ingest.__doc__ = f"Scrape {name} Assembly winners across all available cycles."
        return _ingest
    def _make_all(name, code, cycles, winners_fn, all_fn):
        def _ingest(): _ingest_state_all_candidates(name, code, cycles, winners_fn, all_fn)
        _ingest.__doc__ = f"Scrape EVERY {name} candidate across all cycles."
        return _ingest
    def _make_detail(name):
        def _ingest(): ingest_detail(state_name=name, winners_only=True)
        _ingest.__doc__ = f"Winners-only detail enrichment for {name}."
        return _ingest
    def _make_detail_all(name):
        def _ingest(): ingest_detail(state_name=name, winners_only=False)
        _ingest.__doc__ = f"All-candidates detail enrichment for {name}."
        return _ingest

    _SMALL_STATE_DISPATCH[f"{_key}"]            = _make_winners(_name, _code, _cycles, _winners_fn)
    _SMALL_STATE_DISPATCH[f"{_key}_all"]        = _make_all(_name, _code, _cycles, _winners_fn, _all_fn)
    _SMALL_STATE_DISPATCH[f"{_key}_detail"]     = _make_detail(_name)
    _SMALL_STATE_DISPATCH[f"{_key}_detail_all"] = _make_detail_all(_name)


def main():
    targets = {
        # Per-state targets that pre-date the small-state batch — keep the
        # explicit mapping so the older codepaths stay clearly traceable.
        "punjab":            ingest_punjab,
        "punjab_all":        ingest_punjab_all,
        "punjab_ls":         ingest_punjab_ls,
        "punjab_detail":     ingest_punjab_detail,
        "punjab_detail_all": ingest_punjab_detail_all,
        "bihar":             ingest_bihar,
        "bihar_all":         ingest_bihar_all,
        "bihar_detail":      ingest_bihar_detail,
        "bihar_detail_all":  ingest_bihar_detail_all,
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
        # Small-state batch — bound dynamically from _SMALL_STATE_DISPATCH.
        **_SMALL_STATE_DISPATCH,
    }

    if len(sys.argv) < 2:
        print("Usage: python -m app.ingest <target>\n")
        print("Per-state targets (each has 4 variants: <state>, <state>_all,")
        print("<state>_detail, <state>_detail_all):\n")
        states_listed = sorted({k.split("_")[0] for k in targets if k != "punjab_ls"})
        for s in states_listed:
            print(f"  {s}")
        print("\nSuffixes:")
        print("  <state>           winners-only base scrape (fast)")
        print("  <state>_all       winners + losers (slow; comprehensive)")
        print("  <state>_detail    enrich existing winners with assets/cases")
        print("  <state>_detail_all   enrich all candidates (very slow)")
        print("\nExtras: punjab_ls  (Punjab Lok Sabha MPs)\n")
        sys.exit(1)

    target = sys.argv[1]
    fn = targets.get(target)
    if not fn:
        print(f"Unknown target: {target}")
        print(f"Known targets: {sorted(targets.keys())}")
        sys.exit(1)
    fn()


if __name__ == "__main__":
    main()
