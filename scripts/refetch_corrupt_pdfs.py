"""
Targeted re-fetch for PDFs that came back corrupt from a previous run.

Reads a manifest.jsonl + preprocessed_<state>/*.json, identifies which
outputs are corrupt sentinels, and re-downloads those specific PDFs one
at a time via CDP-attached Chrome (sequential, safest — no concurrent
tabs). Used when --concurrent-tabs > 1 caused Akamai to interrupt
some downloads.

Usage:
    python scripts/refetch_corrupt_pdfs.py \\
      --preprocessed data/eci/for_ai/preprocessed_jk_2024 \\
      --manifest data/eci/raw_pdfs/jk-2024/manifest.jsonl \\
      --raw-dir data/eci/raw_pdfs/jk-2024/raw_pdfs \\
      --cdp 9222
"""
from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parent.parent

MIN_VALID_SIZE = 1024   # PDFs smaller than this are stubs / failed downloads


def _is_valid_pdf(path: Path) -> bool:
    """Fast check: file exists, ≥ 1KB, starts with %PDF- magic bytes.
    Matches the criteria scan_corrupt_pdfs.py uses in --fast mode, so a
    file that passes here would NOT be re-flagged by the scanner."""
    try:
        if not path.exists() or path.stat().st_size < MIN_VALID_SIZE:
            return False
        with path.open("rb") as f:
            return f.read(8).startswith(b"%PDF-")
    except Exception:
        return False


def find_corrupt_filenames(preprocessed_dir: Path) -> set[str]:
    """Scan the preprocessed dir for corrupt sentinels; return the
    matching PDF filenames (without .json extension)."""
    corrupts = set()
    for f in preprocessed_dir.iterdir():
        if not f.name.endswith(".json"):
            continue
        if f.name.startswith("_"):
            continue
        try:
            r = json.loads(f.read_text())
            if r.get("corrupt") or r.get("skipped_corrupt"):
                corrupts.add(f.stem + ".pdf")   # e.g. NAME__123.pdf
        except Exception:
            corrupts.add(f.stem + ".pdf")
    return corrupts


def load_manifest_by_filename(manifest_path: Path) -> dict[str, dict]:
    """Map pdf_filename -> manifest row."""
    out: dict[str, dict] = {}
    for line in manifest_path.read_text().splitlines():
        try:
            r = json.loads(line)
        except json.JSONDecodeError:
            continue
        p = r.get("pdf_path") or ""
        if not p:
            continue
        out[Path(p).name] = r
    return out


async def run_refetch(profile_url: str, name: str, pdf_dir: Path,
                        cdp_port: int) -> tuple[bool, str]:
    """Visit one profile page and download its affidavit. Sequential
    single-tab — the safest way to re-fetch after Akamai interruption."""
    from playwright.async_api import async_playwright

    async with async_playwright() as pw:
        browser = await pw.chromium.connect_over_cdp(
            f"http://localhost:{cdp_port}")
        context = browser.contexts[0] if browser.contexts else \
                    await browser.new_context()
        page = await context.new_page()

        try:
            await page.goto(profile_url, wait_until="domcontentloaded",
                             timeout=30000)
            # Look for the Download button
            btn = await page.query_selector("a[href*='affidavit']")
            if not btn:
                return False, "No download button found"

            # Kick off the download
            async with page.expect_download(timeout=30000) as dl_info:
                await btn.click()
            download = await dl_info.value

            # Derive filename from source URL or fallback to name
            src_url = download.url
            m = re.search(r"/(\d+)/", src_url)
            aff_id = m.group(1) if m else "0"
            safe_name = re.sub(r"[^A-Za-z0-9]+", "_", name).strip("_")
            filename = f"{safe_name}__{aff_id}.pdf"
            target = pdf_dir / filename
            await download.save_as(str(target))
            return True, filename

        except Exception as e:
            return False, f"{type(e).__name__}: {str(e)[:200]}"
        finally:
            try:
                if not page.is_closed():
                    await page.close()
            except Exception:
                pass


async def main_async(args):
    preprocessed = Path(args.preprocessed)
    manifest = Path(args.manifest)
    raw_dir = Path(args.raw_dir)

    corrupts = find_corrupt_filenames(preprocessed)
    print(f"Found {len(corrupts)} corrupt sentinels in "
          f"{preprocessed.name}", file=sys.stderr)

    manifest_by_file = load_manifest_by_filename(manifest)
    to_refetch = []
    for cf in corrupts:
        if cf in manifest_by_file:
            row = manifest_by_file[cf]
            profile_url = row.get("profile_url", "")
            name = row.get("name", "")
            if profile_url and name:
                to_refetch.append((cf, name, profile_url))
        else:
            print(f"  ⚠ {cf} not in manifest — skipping", file=sys.stderr)

    print(f"Re-fetching {len(to_refetch)} candidates sequentially "
          f"(safe mode, no concurrent tabs)...\n", file=sys.stderr)

    succeeded = failed = 0
    for i, (cf, name, url) in enumerate(to_refetch, 1):
        # Delete old corrupt file if it still exists
        old_path = raw_dir / cf
        if old_path.exists():
            old_path.unlink()
            print(f"  Deleted stale {cf}", file=sys.stderr)
        # Also delete the sentinel
        sentinel = preprocessed / (Path(cf).stem + ".json")
        if sentinel.exists():
            sentinel.unlink()

        print(f"[{i}/{len(to_refetch)}] {name}", file=sys.stderr)
        ok, msg = await run_refetch(url, name, raw_dir, args.cdp)
        if ok:
            print(f"     ✓ {msg}", file=sys.stderr)
            succeeded += 1
        else:
            print(f"     ✗ {msg}", file=sys.stderr)
            failed += 1
        # Politeness delay
        await asyncio.sleep(1.5)

    print(f"\n========== REFETCH SUMMARY ==========", file=sys.stderr)
    print(f"  Succeeded: {succeeded}", file=sys.stderr)
    print(f"  Failed:    {failed}", file=sys.stderr)
    print(f"\nNow re-run cloud_vision_preprocess.py — it'll pick up "
          f"the new PDFs and OCR them.", file=sys.stderr)


async def main_from_scan_async(args):
    """Alt entry point: read corrupt list from scan_corrupt_pdfs.py output
    (data/eci/errors/corrupt_pdfs.jsonl) and refetch those specific PDFs
    by going directly to each candidate's profile_url. No preprocessing
    step needed — works purely from raw-download validation.
    """
    scan_log = Path(args.corrupt_log)
    if not scan_log.exists():
        sys.exit(f"Scan log not found: {scan_log}. "
                  f"Run: python scripts/scan_corrupt_pdfs.py --fast")

    # Group corrupt entries by cycle
    by_cycle: dict[str, list[dict]] = {}
    for line in scan_log.read_text().splitlines():
        if not line.strip():
            continue
        try:
            r = json.loads(line)
        except json.JSONDecodeError:
            continue
        cycle = r.get("cycle", "")
        if args.cycle and cycle != args.cycle:
            continue
        by_cycle.setdefault(cycle, []).append(r)

    if not by_cycle:
        sys.exit(f"No corrupt entries found (cycle filter={args.cycle!r})")

    # For each corrupt entry, look up profile_url from its cycle manifest
    to_refetch: list[tuple[str, str, str, Path]] = []
    for cycle, entries in by_cycle.items():
        cycle_dir = PROJECT_ROOT / "data" / "eci" / "raw_pdfs" / cycle
        manifest = cycle_dir / "manifest.jsonl"
        if not manifest.exists():
            print(f"  ⚠ {cycle}: no manifest.jsonl — skipping",
                  file=sys.stderr)
            continue
        mf_index = load_manifest_by_filename(manifest)
        raw_dir = cycle_dir / "raw_pdfs" if (cycle_dir / "raw_pdfs").exists() else cycle_dir
        matched = missed = 0
        for entry in entries:
            basename = Path(entry["path"]).name
            row = mf_index.get(basename)
            if not row or not row.get("profile_url"):
                missed += 1
                continue
            to_refetch.append((
                basename,
                row.get("name") or "unknown",
                row.get("party") or "",
                row.get("constituency") or "",
                row["profile_url"],
                raw_dir,
            ))
            matched += 1
        print(f"  {cycle:30s}  {matched:>5d} retryable  "
              f"({missed} not in manifest)", file=sys.stderr)

    if not to_refetch:
        sys.exit("Nothing retryable found.")

    # SKIP files that were already re-downloaded in a previous run and
    # are now valid — the scan log is a snapshot and doesn't get updated
    # after each refetch. Without this we'd delete the good file and
    # re-fetch pointlessly.
    still_bad: list[tuple[str, str, str, str, str, Path]] = []
    already_fixed = 0
    for item in to_refetch:
        basename, name, party, constituency, url, raw_dir = item
        if _is_valid_pdf(raw_dir / basename):
            already_fixed += 1
            continue
        still_bad.append(item)
    if already_fixed:
        print(f"  ↷ Skipping {already_fixed:,} that were already "
              f"re-downloaded (still-valid PDF on disk)", file=sys.stderr)
    to_refetch = still_bad
    if not to_refetch:
        sys.exit("All corrupt PDFs have already been re-downloaded. "
                 "Re-run scan_corrupt_pdfs.py to confirm zero corrupts.")

    n_tabs = max(1, int(args.tabs))
    print(f"\n→ {len(to_refetch)} PDFs to re-download "
          f"({n_tabs} parallel tab{'s' if n_tabs > 1 else ''}, "
          f"single Playwright session)\n", file=sys.stderr)

    # Split the work across N tabs — round-robin so any per-tab slowdown
    # spreads evenly. Each tab drains its own queue.
    shards: list[list[tuple[str, str, str, str, str, Path]]] = [[] for _ in range(n_tabs)]
    for i, item in enumerate(to_refetch):
        shards[i % n_tabs].append(item)

    # Shared counter + lock for progress reporting across workers.
    total = len(to_refetch)
    progress = {"done": 0, "ok": 0, "fail": 0}
    counter_lock = asyncio.Lock()

    async def worker(tab_id: int, page, jobs):
        for basename, name, party, constituency, url, raw_dir in jobs:
            # Delete the specific stale corrupt file right before we
            # re-download its replacement (safe: we already confirmed
            # it's not valid, and no other tab targets this same file).
            old = raw_dir / basename
            if old.exists():
                try:
                    old.unlink()
                except Exception:
                    pass
            ok, msg = await _download_via_page(
                page, url, name, raw_dir, target_basename=basename)
            async with counter_lock:
                progress["done"] += 1
                if ok:
                    progress["ok"] += 1
                else:
                    progress["fail"] += 1
                d = progress["done"]
            marker = "✓" if ok else "✗"
            # Include party + constituency so log rows for same-name
            # candidates (DINESH, AJAY, etc.) are visually distinct.
            party_short = (party or "?")[:14]
            const_short = (constituency or "?")[:18]
            print(f"[{d:4d}/{total}] tab{tab_id}  {marker}  "
                  f"{name[:30]:<30s}  {party_short:<14s}  "
                  f"{const_short:<18s}  {basename[:32]:<32s}  "
                  f"{msg[:40]}", file=sys.stderr)
            # Politeness delay per worker (so total effective rate ≈
            # n_tabs / delay requests/sec).
            await asyncio.sleep(args.delay)

    # Open Playwright + CDP ONCE. Reuse the same context; open N pages
    # (tabs) that share cookies + the Akamai session already warmed by
    # the user-launched Chrome. Opening a fresh CDP connection per
    # candidate would trigger:
    #   `Protocol error (Browser.setDownloadBehavior): Browser context
    #    management is not supported`
    # on CDP-attached Chrome — the browser rejects repeated download-
    # behavior negotiation.
    from playwright.async_api import async_playwright
    async with async_playwright() as pw:
        browser = await pw.chromium.connect_over_cdp(f"http://localhost:{args.cdp}")
        ctx = browser.contexts[0] if browser.contexts else await browser.new_context()
        # Reuse existing tabs where possible; open more as needed.
        existing = list(ctx.pages)
        pages = []
        for i in range(n_tabs):
            if i < len(existing) and not existing[i].is_closed():
                pages.append(existing[i])
            else:
                pages.append(await ctx.new_page())

        await asyncio.gather(*[
            worker(i, pages[i], shards[i]) for i in range(n_tabs)
        ])

    print(f"\n========== REFETCH SUMMARY ==========", file=sys.stderr)
    print(f"  Succeeded: {progress['ok']}", file=sys.stderr)
    print(f"  Failed:    {progress['fail']}", file=sys.stderr)


async def _download_via_page(page, profile_url: str, name: str,
                              pdf_dir: Path,
                              target_basename: str | None = None) -> tuple[bool, str]:
    """Navigate the shared page to a profile URL, click Download, save
    the PDF. Same pattern as fetch_eci_affidavits.download_pdf but
    called with the already-connected shared page instance (avoids the
    per-call CDP connect that trips setDownloadBehavior on attached
    Chrome).

    If `target_basename` is given, save the PDF at EXACTLY that filename
    (used when we're restoring a specific corrupt file whose original
    name is known — avoids the aff_id-regex bug where we derived the
    wrong ID from the download URL and overwrote unrelated candidates).

    Returns (success, message).
    """
    try:
        await page.goto(profile_url, wait_until="domcontentloaded",
                         timeout=60000)
        await asyncio.sleep(1.0)
        # Query + click the download button INSIDE expect_download —
        # mirrors the working pattern in fetch_eci_affidavits.py so the
        # download listener is armed before the click fires. Also uses
        # 60s timeout to allow for Akamai-rate-limited PDF downloads
        # (files are 2–9MB and can be slow after burst usage).
        async with page.expect_download(timeout=60000) as dl_info:
            btn = await page.query_selector(
                "button.download-btn, button:has-text('Download'), a:has-text('Download')"
            )
            if not btn:
                return False, "No download button on profile page"
            await btn.click()
        download = await dl_info.value

        if target_basename:
            # Restore path — save at the exact filename we're replacing.
            filename = target_basename
        else:
            # Ad-hoc download — derive filename from name + URL. NOTE:
            # this uses the first /(\d+)/ in the URL, which on ECI's
            # affidavit portal is the STATE code (e.g. 17), not the
            # candidate id. Only safe for one-off use where collisions
            # don't matter.
            src_url = download.url or ""
            m = re.search(r"/(\d+)/", src_url)
            aff_id = m.group(1) if m else "0"
            safe_name = re.sub(r"[^A-Za-z0-9]+", "_", name).strip("_")
            filename = f"{safe_name}__{aff_id}.pdf"

        target = pdf_dir / filename
        target.parent.mkdir(parents=True, exist_ok=True)
        await download.save_as(str(target))
        return True, filename
    except Exception as e:
        return False, f"{type(e).__name__}: {str(e)[:200]}"


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                  formatter_class=argparse.RawDescriptionHelpFormatter)
    # Two mutually-exclusive modes: preprocessed-sentinel (original) or
    # raw-scan (new — reads corrupt_pdfs.jsonl from scan_corrupt_pdfs.py).
    ap.add_argument("--from-scan", action="store_true",
                    help="Read corrupt list from data/eci/errors/corrupt_pdfs.jsonl "
                         "(produced by scripts/scan_corrupt_pdfs.py). "
                         "Auto-discovers profile URLs from each cycle's manifest.")
    ap.add_argument("--corrupt-log",
                    default=str(PROJECT_ROOT / "data" / "eci" / "errors" / "corrupt_pdfs.jsonl"),
                    help="Path to corrupt_pdfs.jsonl (for --from-scan mode)")
    ap.add_argument("--cycle", default="",
                    help="Restrict --from-scan to one cycle (e.g. uttarpradesh-2022)")
    # Original mode flags (preprocessing sentinels)
    ap.add_argument("--preprocessed", help="Path to preprocessed_<state>/ (original mode)")
    ap.add_argument("--manifest", help="Path to raw_pdfs/<state>/manifest.jsonl")
    ap.add_argument("--raw-dir", help="Path to raw_pdfs/<state>/raw_pdfs/")
    # Shared flags
    ap.add_argument("--cdp", type=int, default=9222,
                    help="CDP port for dedicated Chrome (default 9222)")
    ap.add_argument("--delay", type=float, default=1.5,
                    help="Politeness delay (sec) between requests per tab")
    ap.add_argument("--tabs", type=int, default=4,
                    help="Number of parallel tabs (default 4). Each tab "
                         "shares the same Chrome context / Akamai session.")
    args = ap.parse_args()

    if args.from_scan:
        asyncio.run(main_from_scan_async(args))
    else:
        # Original mode requires preprocessed / manifest / raw_dir
        missing = [n for n in ("preprocessed", "manifest", "raw_dir")
                   if not getattr(args, n.replace("-", "_"))]
        if missing:
            sys.exit(f"Original mode needs: --{', --'.join(missing)}\n"
                      f"Or use --from-scan to read corrupt_pdfs.jsonl.")
        asyncio.run(main_async(args))


if __name__ == "__main__":
    main()
