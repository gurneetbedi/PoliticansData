"""Given the missing-data report, refetch PDFs for every DB candidate
whose wealth/education fields are still NULL — targeted, no full re-crawl.

Two-tier strategy:

  TIER 1 — Manifest lookup.
     For each missing candidate, find their manifest entry by
     (constituency, name). If a profile_url is available, refetch via
     the same shared-page mechanism the corrupt-refetch script uses.

  TIER 2 — Constituency-level URL.
     For candidates whose manifest lacks a profile_url (or who don't
     appear in the manifest at all), fall back to fetching the entire
     constituency's listing via a CandidateCustomFilter URL, then match
     the desired candidate by name.

Usage:
    # Report gaps only (no downloads)
    python scripts/refetch_missing_from_db.py --state "Uttar Pradesh" --year 2022 --report-only

    # Tier-1 refetch (fast, uses existing profile URLs)
    python scripts/refetch_missing_from_db.py --state "Uttar Pradesh" --year 2022 --cdp 9222 --tabs 4

    # Combined tier-1 + tier-2 (constituency-URL fallback)
    python scripts/refetch_missing_from_db.py \\
      --state "Rajasthan" --year 2023 --cdp 9222 --tabs 4 \\
      --tier2-listing-url-template 'https://affidavit.eci.gov.in/CandidateCustomFilter?electionType=22-AC-GENERAL-3-43&election=22-AC-GENERAL-3-43&states=S20&phase={phase}&constId={const_id}'

The tier-2 URL template MUST contain {const_id}. {phase} is optional —
omit and drop the &phase= param entirely if the state's election was
single-phase. You'll also need a constituency-name -> const_id mapping,
which we don't have programmatically yet — pass a CSV via
--tier2-constid-csv (columns: constituency,const_id[,phase]).
"""
from __future__ import annotations
import argparse
import asyncio
import csv
import json
import os
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


# ------------------------------------------------------------------
# DB query — same as report_missing_data but returns raw rows
# ------------------------------------------------------------------
def load_missing(state: str, year: int) -> list[dict]:
    import sqlalchemy
    from sqlalchemy import text
    from sqlalchemy.orm import sessionmaker
    DB_URL = os.environ.get(
        "DATABASE_URL", f"sqlite:///{ROOT / 'lokvani.db'}")
    engine = sqlalchemy.create_engine(DB_URL)
    Session = sessionmaker(bind=engine)
    sess = Session()
    q = text("""
      SELECT p.name AS candidate,
             c.name AS constituency,
             COALESCE(pa.full_name, pa.short_name) AS party
        FROM election_appearances ea
        JOIN politicians  p  ON ea.politician_id   = p.id
        JOIN elections    e  ON ea.election_id     = e.id
        JOIN constituencies c ON ea.constituency_id = c.id
        JOIN states       s  ON c.state_id         = s.id
        LEFT JOIN parties pa ON ea.party_id        = pa.id
       WHERE s.name = :state
         AND e.year = :year
         AND ea.total_assets_inr IS NULL
         AND ea.education IS NULL
       ORDER BY c.name, p.name
    """)
    return [dict(r._mapping) for r in
            sess.execute(q, {"state": state, "year": year}).fetchall()]


# ------------------------------------------------------------------
# Manifest lookup — join by normalized (constituency, name)
# ------------------------------------------------------------------
def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", (s or "").lower())


def find_cycle_dir(state: str, year: int) -> Path | None:
    slug = _norm(state)
    root = ROOT / "data" / "eci" / "raw_pdfs"
    for d in sorted(root.iterdir()):
        if d.name.startswith(slug) and d.name.endswith(str(year)) \
                and (d / "manifest.jsonl").exists():
            return d
    return None


def build_manifest_index(cycle_dir: Path) -> dict[tuple[str, str], dict]:
    idx: dict[tuple[str, str], dict] = {}
    for line in (cycle_dir / "manifest.jsonl").read_text().splitlines():
        try:
            r = json.loads(line)
        except json.JSONDecodeError:
            continue
        idx[(_norm(r.get("name", "")), _norm(r.get("constituency", "")))] = r
    return idx


def enrich(rows: list[dict], mf_idx: dict) -> tuple[list[dict], list[dict]]:
    """Split into (has_profile_url, no_profile_url)."""
    have, missing = [], []
    for r in rows:
        hit = mf_idx.get((_norm(r["candidate"]), _norm(r["constituency"])))
        if hit and hit.get("profile_url"):
            r["profile_url"] = hit["profile_url"]
            pdf = hit.get("pdf_path") or ""
            r["target_basename"] = Path(pdf).name if pdf else ""
            have.append(r)
        else:
            missing.append(r)
    return have, missing


# ------------------------------------------------------------------
# Tier 1 — refetch via profile_url (shared Playwright session)
# ------------------------------------------------------------------
async def tier1_refetch(cycle_dir: Path, rows: list[dict],
                          cdp_port: int, tabs: int, delay: float) -> None:
    if not rows:
        return
    from playwright.async_api import async_playwright

    raw_dir = cycle_dir / "raw_pdfs" if (cycle_dir / "raw_pdfs").exists() else cycle_dir
    print(f"\nTIER 1 — {len(rows)} candidates with profile_urls "
          f"({tabs} parallel tab{'s' if tabs > 1 else ''})", file=sys.stderr)

    # Round-robin shard
    shards: list[list[dict]] = [[] for _ in range(tabs)]
    for i, r in enumerate(rows):
        shards[i % tabs].append(r)

    total = len(rows)
    counter = {"done": 0, "ok": 0, "fail": 0}
    lock = asyncio.Lock()

    async def worker(tab_id: int, page, jobs):
        for r in jobs:
            basename = r["target_basename"] or _fallback_name(r)
            target = raw_dir / basename
            # Skip if a good file already exists
            if target.exists() and target.stat().st_size > 1024:
                async with lock:
                    counter["done"] += 1
                    counter["ok"] += 1
                    d = counter["done"]
                print(f"[{d:4d}/{total}] tab{tab_id}  ↷  "
                      f"{r['candidate'][:30]:<30s}  already valid",
                      file=sys.stderr)
                continue
            ok, msg = await _download_via_page(page, r["profile_url"],
                                                 target)
            async with lock:
                counter["done"] += 1
                if ok:
                    counter["ok"] += 1
                else:
                    counter["fail"] += 1
                d = counter["done"]
            mark = "✓" if ok else "✗"
            print(f"[{d:4d}/{total}] tab{tab_id}  {mark}  "
                  f"{r['candidate'][:30]:<30s}  {r['constituency'][:20]:<20s}  "
                  f"{basename[:32]:<32s}  {msg[:40]}", file=sys.stderr)
            await asyncio.sleep(delay)

    async with async_playwright() as pw:
        browser = await pw.chromium.connect_over_cdp(
            f"http://localhost:{cdp_port}")
        ctx = browser.contexts[0] if browser.contexts \
                else await browser.new_context()
        existing = list(ctx.pages)
        pages = [existing[i] if i < len(existing) and not existing[i].is_closed()
                 else await ctx.new_page() for i in range(tabs)]
        await asyncio.gather(*[worker(i, pages[i], shards[i])
                               for i in range(tabs)])

    print(f"\nTIER 1 summary: {counter['ok']} succeeded, "
          f"{counter['fail']} failed", file=sys.stderr)


def _fallback_name(r: dict) -> str:
    """When manifest has no pdf_path, invent a filename from name+constituency."""
    safe = re.sub(r"[^A-Za-z0-9]+", "_", (r["candidate"] or "unknown")).strip("_")
    const_hint = re.sub(r"[^A-Za-z0-9]+", "_", (r["constituency"] or ""))[:20]
    return f"{safe}__{const_hint}.pdf"


async def _download_via_page(page, profile_url: str,
                              target: Path) -> tuple[bool, str]:
    try:
        await page.goto(profile_url, wait_until="domcontentloaded",
                         timeout=60000)
        await asyncio.sleep(1.0)
        async with page.expect_download(timeout=60000) as dl_info:
            btn = await page.query_selector(
                "button.download-btn, button:has-text('Download'), a:has-text('Download')")
            if not btn:
                return False, "no Download button"
            await btn.click()
        download = await dl_info.value
        target.parent.mkdir(parents=True, exist_ok=True)
        await download.save_as(str(target))
        return True, target.name
    except Exception as e:
        return False, f"{type(e).__name__}: {str(e)[:60]}"


# ------------------------------------------------------------------
# Tier 2 — constituency-level URL, match by name
# ------------------------------------------------------------------
async def tier2_refetch(cycle_dir: Path, rows: list[dict],
                          url_template: str, constid_csv: Path,
                          cdp_port: int, tabs: int) -> None:
    """Fetch each unique constituency's listing, download the matching candidate."""
    if not rows:
        return
    if not constid_csv or not constid_csv.exists():
        print(f"\nTIER 2 skipped: --tier2-constid-csv is required "
              f"(need constituency → const_id[,phase] mapping)",
              file=sys.stderr)
        print(f"Missing candidates ({len(rows)}):", file=sys.stderr)
        by_const: dict[str, list[str]] = {}
        for r in rows:
            by_const.setdefault(r["constituency"], []).append(r["candidate"])
        for c, names in list(by_const.items())[:10]:
            print(f"  {c}: {', '.join(names[:3])}"
                  f"{f' + {len(names)-3} more' if len(names) > 3 else ''}",
                  file=sys.stderr)
        return

    # Load constid mapping: constituency -> {const_id, phase}
    cmap: dict[str, dict] = {}
    with constid_csv.open() as f:
        rd = csv.DictReader(f)
        for row in rd:
            key = _norm(row["constituency"])
            cmap[key] = {"const_id": row["const_id"],
                          "phase": row.get("phase", "")}

    # Group missing candidates by constituency
    by_const: dict[str, list[dict]] = {}
    for r in rows:
        by_const.setdefault(r["constituency"], []).append(r)

    print(f"\nTIER 2 — fetching {len(by_const)} constituency listing(s)",
          file=sys.stderr)

    from playwright.async_api import async_playwright
    raw_dir = cycle_dir / "raw_pdfs" if (cycle_dir / "raw_pdfs").exists() else cycle_dir

    async with async_playwright() as pw:
        browser = await pw.chromium.connect_over_cdp(
            f"http://localhost:{cdp_port}")
        ctx = browser.contexts[0] if browser.contexts \
                else await browser.new_context()
        page = ctx.pages[0] if ctx.pages else await ctx.new_page()

        for const_name, cand_list in by_const.items():
            info = cmap.get(_norm(const_name))
            if not info:
                print(f"  ✗ {const_name}: no const_id in CSV — skip",
                      file=sys.stderr)
                continue
            url = url_template.replace("{const_id}", info["const_id"]) \
                              .replace("{phase}",   info.get("phase", ""))
            print(f"\n  {const_name} → {url[:80]}", file=sys.stderr)
            print(f"    Wanted: {[c['candidate'] for c in cand_list]}",
                  file=sys.stderr)
            # Visit listing page; collect profile links; for each, fetch
            # its profile page and check if the name matches. Download
            # matches.
            try:
                await page.goto(url, wait_until="domcontentloaded",
                                 timeout=60000)
                await asyncio.sleep(2.0)
                links = await page.query_selector_all("a[href*='show-profile']")
                print(f"    Found {len(links)} profile link(s)",
                      file=sys.stderr)
                wanted_norms = {_norm(c["candidate"]): c for c in cand_list}
                # Extract href + surrounding text so we can name-match
                # without navigating each one.
                for a in links:
                    try:
                        card = await a.evaluate_handle(
                            "el => el.closest('.card, .row, li, tr, div')")
                        heading = await card.as_element().query_selector(
                            "h3, h4, h5, strong")
                        cand_txt = ""
                        if heading:
                            cand_txt = (await heading.inner_text() or "").strip()
                        norm_txt = _norm(cand_txt)
                        matched = wanted_norms.get(norm_txt)
                        if not matched:
                            continue
                        href = await a.get_attribute("href")
                        if not href:
                            continue
                        target = raw_dir / _fallback_name(matched)
                        ok, msg = await _download_via_page(page, href, target)
                        mark = "✓" if ok else "✗"
                        print(f"    {mark} {cand_txt[:30]:<30s}  {msg[:50]}",
                              file=sys.stderr)
                    except Exception:
                        continue
            except Exception as e:
                print(f"    ✗ listing fetch failed: {type(e).__name__}: {e}",
                      file=sys.stderr)


# ------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                  formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--state", required=True)
    ap.add_argument("--year", type=int, required=True)
    ap.add_argument("--report-only", action="store_true",
                    help="Don't refetch — just print what would happen")
    ap.add_argument("--cdp", type=int, default=9222)
    ap.add_argument("--tabs", type=int, default=4)
    ap.add_argument("--delay", type=float, default=1.5)
    ap.add_argument("--tier2-listing-url-template", default="",
                    help="URL template with {const_id} and optional {phase} placeholders")
    ap.add_argument("--tier2-constid-csv", type=Path, default=None,
                    help="CSV: constituency,const_id[,phase]")
    args = ap.parse_args()

    cycle_dir = find_cycle_dir(args.state, args.year)
    if not cycle_dir:
        sys.exit(f"No cycle dir for {args.state} {args.year} — need to fetch first")

    rows = load_missing(args.state, args.year)
    print(f"Missing in DB: {len(rows)}", file=sys.stderr)
    if not rows:
        return

    mf_idx = build_manifest_index(cycle_dir)
    print(f"Manifest rows: {len(mf_idx)}", file=sys.stderr)
    tier1, tier2 = enrich(rows, mf_idx)
    print(f"  Tier 1 (have profile_url):    {len(tier1)}", file=sys.stderr)
    print(f"  Tier 2 (need constituency URL): {len(tier2)}", file=sys.stderr)

    if args.report_only:
        print("\n--- report-only mode — nothing fetched ---", file=sys.stderr)
        return

    asyncio.run(tier1_refetch(cycle_dir, tier1, args.cdp, args.tabs, args.delay))

    if tier2 and args.tier2_listing_url_template:
        asyncio.run(tier2_refetch(cycle_dir, tier2,
                                    args.tier2_listing_url_template,
                                    args.tier2_constid_csv,
                                    args.cdp, args.tabs))
    elif tier2:
        print(f"\nTIER 2: {len(tier2)} candidates need constituency-URL fetch. "
              f"Pass --tier2-listing-url-template + --tier2-constid-csv "
              f"to auto-fetch them.", file=sys.stderr)


if __name__ == "__main__":
    main()
