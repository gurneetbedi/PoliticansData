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


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--preprocessed", required=True,
                    help="Path to data/eci/for_ai/preprocessed_<state>/")
    ap.add_argument("--manifest", required=True,
                    help="Path to raw_pdfs/<state>/manifest.jsonl")
    ap.add_argument("--raw-dir", required=True,
                    help="Path to raw_pdfs/<state>/raw_pdfs/")
    ap.add_argument("--cdp", type=int, default=9222,
                    help="CDP port for dedicated Chrome (default 9222)")
    args = ap.parse_args()
    asyncio.run(main_async(args))


if __name__ == "__main__":
    main()
