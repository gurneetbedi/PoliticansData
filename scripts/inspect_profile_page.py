"""Navigate to a specific candidate profile URL via CDP-attached Chrome
and dump what's on the page — URL after load, page title, all button /
anchor text, any 'download' matches. Read-only, no download attempted.

Usage:
    # Pass a profile URL directly
    python scripts/inspect_profile_page.py --url 'https://affidavit.eci.gov.in/...'

    # Or pass a corrupt basename and it'll look up the URL from manifest
    python scripts/inspect_profile_page.py --basename AJAY__9372.pdf --cycle uttarpradesh-2022

    # Or dump the first N corrupts from a cycle (see if any load right)
    python scripts/inspect_profile_page.py --cycle uttarpradesh-2022 --count 3
"""
from __future__ import annotations
import argparse
import asyncio
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


async def inspect_one(page, url: str, label: str = ""):
    print(f"\n{'='*78}", file=sys.stderr)
    print(f"URL: {url}", file=sys.stderr)
    if label:
        print(f"     ({label})", file=sys.stderr)
    print(f"{'='*78}", file=sys.stderr)
    try:
        resp = await page.goto(url, wait_until="domcontentloaded", timeout=30000)
        await asyncio.sleep(1.5)
    except Exception as e:
        print(f"  ✗ goto failed: {type(e).__name__}: {e}", file=sys.stderr)
        return
    print(f"  HTTP status: {resp.status if resp else '?'}", file=sys.stderr)
    print(f"  Final URL:   {page.url}", file=sys.stderr)
    print(f"  Title:       {await page.title()}", file=sys.stderr)

    # Enumerate download-y elements
    for sel_desc, sel in [
        ("button.download-btn",  "button.download-btn"),
        ("button:has-text('Download')", "button:has-text('Download')"),
        ("a:has-text('Download')",      "a:has-text('Download')"),
        ("a[href*='affidavit']",        "a[href*='affidavit']"),
        ("a[href$='.pdf']",              "a[href$='.pdf']"),
    ]:
        try:
            elts = await page.query_selector_all(sel)
            if elts:
                print(f"\n  MATCH  {sel_desc}: {len(elts)} element(s)",
                      file=sys.stderr)
                for e in elts[:3]:
                    txt = (await e.inner_text() or "").strip()[:60]
                    href = await e.get_attribute("href")
                    print(f"     text='{txt}'  href={href!r}", file=sys.stderr)
        except Exception:
            pass

    # Show all buttons / links (first 15) so we can eyeball the layout
    btns = await page.query_selector_all("button, a")
    print(f"\n  All buttons+anchors on page ({len(btns)} total, first 10):",
          file=sys.stderr)
    for b in btns[:10]:
        txt = (await b.inner_text() or "").strip()[:50]
        if txt:
            print(f"     '{txt}'", file=sys.stderr)


async def run(args):
    from playwright.async_api import async_playwright

    urls: list[tuple[str, str]] = []
    if args.url:
        urls = [(args.url, "manual")]
    else:
        cycle = args.cycle
        if not cycle:
            sys.exit("Need --url or --cycle")
        manifest = ROOT / "data" / "eci" / "raw_pdfs" / cycle / "manifest.jsonl"
        scan = ROOT / "data" / "eci" / "errors" / "corrupt_pdfs.jsonl"
        if not manifest.exists():
            sys.exit(f"No manifest: {manifest}")

        mf = {}
        for line in manifest.read_text().splitlines():
            try:
                r = json.loads(line)
            except json.JSONDecodeError:
                continue
            if r.get("pdf_path"):
                mf[Path(r["pdf_path"]).name] = r

        if args.basename:
            row = mf.get(args.basename)
            if not row:
                sys.exit(f"{args.basename} not in manifest")
            urls = [(row["profile_url"], row.get("name", ""))]
        else:
            # Take first N corrupts for this cycle
            entries = [json.loads(l) for l in scan.read_text().splitlines() if l.strip()]
            entries = [e for e in entries if e["cycle"] == cycle]
            for e in entries[:args.count]:
                bn = Path(e["path"]).name
                row = mf.get(bn)
                if row and row.get("profile_url"):
                    urls.append((row["profile_url"], f"{row.get('name')} ({bn})"))

    if not urls:
        sys.exit("No URLs resolved.")

    async with async_playwright() as pw:
        browser = await pw.chromium.connect_over_cdp(f"http://localhost:{args.cdp}")
        ctx = browser.contexts[0] if browser.contexts else await browser.new_context()
        pages = ctx.pages
        page = pages[0] if pages else await ctx.new_page()

        for url, label in urls:
            await inspect_one(page, url, label)


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                  formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--url")
    ap.add_argument("--basename", help="e.g. AJAY__9372.pdf")
    ap.add_argument("--cycle", default="")
    ap.add_argument("--count", type=int, default=3,
                    help="How many corrupts to inspect (used only when neither "
                         "--url nor --basename is given)")
    ap.add_argument("--cdp", type=int, default=9222)
    asyncio.run(run(ap.parse_args()))


if __name__ == "__main__":
    main()
