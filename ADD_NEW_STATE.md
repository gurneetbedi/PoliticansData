# Adding a new state — runbook

The full end-to-end sequence for adding new state data, using **Sikkim + Delhi**
(just added to the codebase) as the worked example.

---

## 0. Prerequisites

```bash
cd /path/to/PoliticansData
source .venv/bin/activate
```

The states are already declared in `app/states.py`, scrapers in
`app/scrapers/{sikkim,delhi}.py`, ingest targets in `app/ingest.py`, and the
UI in `base.html` / `home.html` / `main.py` is already wired. **Don't push
anything yet** — verify the slugs first, scrape locally, then push the cleaned
data to Neon.

---

## 1. Verify slugs (~30 sec each)

myneta's cycle URLs use inconsistent casing — `Sikkim2024` for the latest
cycle but `sikkim2019` for older ones. Hit each slug once to confirm it's
real before committing to a multi-hour scrape against a 404.

```bash
python scripts/verify_state_slugs.py sikkim delhi
```

You should see something like:

```
Sikkim (sikkim):
  OK    Sikkim2024            →  https://myneta.info/Sikkim2024/
  OK    sikkim2019            →  https://myneta.info/sikkim2019/
  OK    sikkim2014            →  https://myneta.info/sikkim2014/
  OK    sikkim2009            →  https://myneta.info/sikkim2009/

Delhi (delhi):
  OK    Delhi2025             →  https://myneta.info/Delhi2025/
  OK    delhi2020             →  https://myneta.info/delhi2020/
  ...
```

**If any FAIL**, open the URL in a browser, find the right slug from
myneta's own navigation, edit `app/states.py`, and re-run.

---

## 2. Scrape (this is the long part)

Each command runs the scraper at the standard 2-second rate limit. You can
run Sikkim first (small) to validate the pipeline end-to-end before kicking
off the big Delhi scrape.

```bash
# Sikkim — small state, fast
python -m app.ingest sikkim_all     # winners + losers across 4 cycles, ~30 min
python -m app.ingest sikkim_detail_all   # per-affidavit enrichment, ~20 min

# Delhi — large state, slow
python -m app.ingest delhi_all      # winners + losers across 5 cycles, ~3 hours
python -m app.ingest delhi_detail_all    # per-affidavit enrichment, ~2 hours
```

You can run these in any order, and you can interrupt with `Ctrl+C` mid-scrape
— ingest is idempotent and the scraper's response cache persists, so a
restart resumes from where it left off (no double-fetching).

**Tip**: open another terminal and `tail -f` nothing — just watch the
`data/cache/myneta/` directory file count grow with `watch -n 5 'ls data/cache/myneta | wc -l'`
to confirm progress.

---

## 3. Re-run the splitter

After every fresh scrape, run the cleanup script to make sure no new
politicians collide on candidate_id with existing ones. (The splitter is
idempotent — re-running on already-split data is a no-op.)

```bash
python scripts/split_merged_politicians.py
```

Confirm `cross-state politicians: 0` at the end.

---

## 4. Push to Neon

```bash
export DATABASE_URL="postgresql://neondb_owner:...@ep-...neon.tech/neondb?sslmode=require"
python scripts/sqlite_to_postgres.py --reset
```

`--reset` drops the existing Neon tables and reloads from scratch — the
safest path. Takes ~2 minutes total at the optimized multi-row INSERT speed.

---

## 5. Push code + redeploy

```bash
git add app/states.py app/scrapers/sikkim.py app/scrapers/delhi.py app/ingest.py \
        app/main.py app/templates/base.html app/templates/home.html \
        scripts/verify_state_slugs.py ADD_NEW_STATE.md
git commit -m "Add Sikkim + Delhi state coverage"
git push origin main
```

Render auto-redeploys in ~3 minutes. After it goes live, hard-refresh the
homepage — Sikkim and Delhi should appear in the State Selector dropdown,
the India map should color them, and `/?state=Sikkim` / `/?state=Delhi`
should show their MLAs.

---

## Estimated total times

| Step | Sikkim | Delhi | Both |
|---|---|---|---|
| Verify slugs | 30s | 30s | 1m |
| Scrape (`*_all`) | 30m | 3h | 3h 30m |
| Detail enrichment (`*_detail_all`) | 20m | 2h | 2h 20m |
| Splitter | 2m | 2m | 2m |
| Push to Neon | 2m | 2m | 2m |
| **Total wall clock** | **~55 min** | **~5 h** | **~5h 55m** |

If you want to launch a "Sikkim added" milestone before Delhi finishes,
push after step 4 for Sikkim only — Delhi will appear in the UI as
"Coming soon" (since the registry has it but no data yet).

---

## Troubleshooting

**Scrape stalls or times out** — myneta occasionally returns 503 under load.
The scraper has exponential backoff; just leave it running. If it hard-fails,
restart the same command — the cache resumes from the last good page.

**Names show as garbage or empty after ingest** — your local bs4/lxml might
have picked the wrong HTML tag. The splitter's name extractor (in
`scripts/split_merged_politicians.py`) is the most robust; let it normalize
names by running step 3 immediately after the scrape.

**Render starts 500ing after Neon push** — most likely the schema doesn't
match. Re-run step 4 with the latest models.py committed locally so
`create_all` regenerates the schema correctly.
