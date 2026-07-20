"""For a given state cycle, ensure every PDF in the allowlist is
present on disk. For each missing one, look up its profile_url in the
manifest and refetch via CDP-attached Chrome.

Also handles the "truncated _raw Gemini extraction" case: if any
Gemini JSON has extraction._raw that isn't parseable (usually a
max-token cutoff), delete it so llm_extract will re-process.

Usage:
    # Dry-run (default) — reports what's missing without changes
    python scripts/sync_allowlist_pdfs.py --state "Rajasthan" --year 2023

    # Commit — refetches missing PDFs + deletes truncated Gemini JSONs
    python scripts/sync_allowlist_pdfs.py --state "Rajasthan" --year 2023 --commit --cdp 9222 --tabs 1
"""
from __future__ import annotations
import argparse
import asyncio
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def find_cycle(state: str, year: int) -> Path:
    slug = state.lower().replace(" ", "")
    for d in sorted((ROOT / "data/eci/raw_pdfs").iterdir()):
        if d.name.startswith(slug) and d.name.endswith(str(year)):
            return d
    sys.exit(f"No cycle dir for {state} {year}")


def find_allowlist(state: str, year: int) -> Path:
    """Look for allowlist by trying two slug conventions:
      - space-stripped: "Uttar Pradesh" → "uttarpradesh"  (current convention)
      - underscore:     "Uttar Pradesh" → "uttar_pradesh" (never actually used)
    Then canonical name first, then legacy top-N patterns."""
    slugs_to_try = [
        state.lower().replace(" ", ""),      # uttarpradesh (canonical)
        state.lower().replace(" ", "_"),     # uttar_pradesh (defensive)
    ]
    allow_dir = ROOT / "data/allowlists"
    for slug in slugs_to_try:
        p = allow_dir / f"{slug}_{year}.txt"
        if p.exists():
            return p
        matches = sorted(allow_dir.glob(f"{slug}_{year}_top*.txt"))
        if matches:
            return matches[0]
    sys.exit(f"No allowlist found for {state} {year} in "
              f"{allow_dir.relative_to(ROOT)}/ "
              f"(tried slugs: {slugs_to_try})")


def load_manifest_by_name(cycle_dir: Path) -> dict[str, dict]:
    idx = {}
    for line in (cycle_dir / "manifest.jsonl").read_text().splitlines():
        try:
            r = json.loads(line)
        except Exception:
            continue
        p = r.get("pdf_path") or ""
        if p:
            idx[Path(p).name] = r
    return idx


def delete_truncated_gemini(lx: Path, commit: bool) -> list[str]:
    """Any Gemini JSON whose extraction._raw fails to parse gets deleted
    so the next llm_extract run re-processes it."""
    deleted = []
    for f in sorted(lx.glob("*.json")):
        if f.name.startswith("_"):
            continue
        try:
            r = json.loads(f.read_text())
        except Exception:
            continue
        ext = r.get("extraction") or {}
        if "_raw" not in ext:
            continue
        raw_str = ext["_raw"].strip()
        raw_str = re.sub(r"^```(?:json)?\s*", "", raw_str)
        raw_str = re.sub(r"\s*```$", "", raw_str)
        try:
            json.loads(raw_str)
            continue  # Actually parseable — leave alone
        except Exception:
            pass
        deleted.append(f.stem)
        if commit:
            f.unlink()
    return deleted


async def refetch_missing(cycle_dir: Path, jobs: list[dict],
                            cdp: int, tabs: int, delay: float) -> tuple[int, int]:
    from playwright.async_api import async_playwright
    raw_dir = cycle_dir / "raw_pdfs" if (cycle_dir / "raw_pdfs").exists() else cycle_dir

    # Shard
    shards: list[list[dict]] = [[] for _ in range(tabs)]
    for i, j in enumerate(jobs):
        shards[i % tabs].append(j)

    counter = {"ok": 0, "fail": 0, "done": 0}
    total = len(jobs)
    lock = asyncio.Lock()

    async def worker(tab_id: int, page, sublist):
        for j in sublist:
            ok, msg = await _download(page, j["url"], raw_dir, j["basename"])
            async with lock:
                counter["done"] += 1
                counter["ok" if ok else "fail"] += 1
                d = counter["done"]
            mark = "✓" if ok else "✗"
            print(f"[{d:>4d}/{total}] tab{tab_id} {mark} {j['basename'][:35]:<35s}  {msg[:60]}",
                  file=sys.stderr)
            await asyncio.sleep(delay)

    async with async_playwright() as pw:
        browser = await pw.chromium.connect_over_cdp(f"http://localhost:{cdp}")
        ctx = browser.contexts[0] if browser.contexts else await browser.new_context()
        existing = list(ctx.pages)
        pages = [existing[i] if i < len(existing) and not existing[i].is_closed()
                 else await ctx.new_page() for i in range(tabs)]
        await asyncio.gather(*[worker(i, pages[i], shards[i]) for i in range(tabs)])

    return counter["ok"], counter["fail"]


async def _download(page, profile_url: str, raw_dir: Path,
                     basename: str) -> tuple[bool, str]:
    try:
        await page.goto(profile_url, wait_until="domcontentloaded", timeout=60000)
        await asyncio.sleep(1.0)
        async with page.expect_download(timeout=60000) as dl:
            btn = await page.query_selector(
                "button.download-btn, button:has-text('Download'), a:has-text('Download')")
            if not btn:
                return False, "no Download button"
            await btn.click()
        d = await dl.value
        target = raw_dir / basename
        target.parent.mkdir(parents=True, exist_ok=True)
        await d.save_as(str(target))
        return True, basename
    except Exception as e:
        return False, f"{type(e).__name__}: {str(e)[:50]}"


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                  formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--state", required=True)
    ap.add_argument("--year", type=int, required=True)
    ap.add_argument("--commit", action="store_true")
    ap.add_argument("--cdp", type=int, default=9222)
    ap.add_argument("--tabs", type=int, default=1)
    ap.add_argument("--delay", type=float, default=2.5)
    args = ap.parse_args()

    cycle_dir = find_cycle(args.state, args.year)
    allowlist_path = find_allowlist(args.state, args.year)
    slug = args.state.lower().replace(" ", "") + f"_{args.year}"
    lx = ROOT / "data/eci/for_ai/llm_extracted" / slug

    # Some cycles have PDFs both directly in cycle_dir (flat) AND inside
    # a cycle_dir/raw_pdfs/ subfolder (nested), because different fetch
    # runs used different output paths. Walk the ENTIRE cycle_dir tree
    # so we don't false-flag files that exist in the sibling location.
    on_disk = {p.name for p in cycle_dir.rglob("*.pdf")}
    # Target folder for any NEW downloads — prefer the nested layout
    # (that's what Cloud Vision preprocessing points at). Fall back to
    # flat cycle_dir if the nested one doesn't exist yet.
    raw_dir = cycle_dir / "raw_pdfs" if (cycle_dir / "raw_pdfs").exists() else cycle_dir
    allowlist = [ln.strip() for ln in allowlist_path.read_text().splitlines()
                 if ln.strip() and not ln.startswith("#")]
    missing = [name for name in allowlist if name not in on_disk]

    print(f"Cycle:     {cycle_dir.relative_to(ROOT)}")
    print(f"Allowlist: {len(allowlist)} PDFs")
    print(f"On disk:   {len(on_disk):,} PDFs")
    print(f"Missing:   {len(missing)} PDFs from allowlist\n")

    # Build refetch jobs
    mf = load_manifest_by_name(cycle_dir)
    jobs, no_url = [], []
    for name in missing:
        row = mf.get(name)
        if not row or not row.get("profile_url"):
            no_url.append(name)
            continue
        jobs.append({"basename": name, "url": row["profile_url"],
                     "candidate": row.get("name", "")})
    if no_url:
        print(f"  ⚠ {len(no_url)} missing PDFs have no profile_url in manifest — skip",
              file=sys.stderr)

    # Check truncated Gemini
    truncated = delete_truncated_gemini(lx, commit=False) if lx.exists() else []
    print(f"Truncated Gemini _raw JSONs: {len(truncated)}")
    for s in truncated[:3]:
        print(f"    • {s}.json")

    print(f"\nWill refetch: {len(jobs)} PDFs")
    print(f"Will delete truncated Gemini: {len(truncated)} JSONs")

    if not args.commit:
        print("\n--- DRY RUN. Re-run with --commit to apply. ---")
        return

    # Commit: delete truncated + refetch
    if truncated:
        delete_truncated_gemini(lx, commit=True)
        print(f"\n✓ Deleted {len(truncated)} truncated Gemini JSONs")

    if jobs:
        print(f"\nRefetching {len(jobs)} missing PDFs "
              f"({args.tabs} tab{'s' if args.tabs > 1 else ''}, "
              f"{args.delay}s delay)...")
        ok, fail = asyncio.run(refetch_missing(cycle_dir, jobs, args.cdp,
                                                 args.tabs, args.delay))
        print(f"\n✓ Refetch: {ok} succeeded, {fail} failed")

    print(f"\nNext steps:")
    print(f"  python scripts/cloud_vision_preprocess.py \\")
    print(f"    --pdf-dir {(raw_dir).relative_to(ROOT)} \\")
    print(f"    --out-dir data/eci/for_ai/preprocessed_{slug} \\")
    print(f"    --pdf-allowlist {allowlist_path.relative_to(ROOT)}")
    print(f"  python scripts/llm_extract_via_gemini.py \\")
    print(f"    --in-dir data/eci/for_ai/preprocessed_{slug} \\")
    print(f"    --out-dir data/eci/for_ai/llm_extracted/{slug} \\")
    print(f"    --state \"{args.state}\" --year {args.year}")
    print(f"  python scripts/apply_llm_extraction.py --cycles {slug}")


if __name__ == "__main__":
    main()
