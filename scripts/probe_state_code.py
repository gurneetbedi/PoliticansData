"""Verify an ECI state code by hitting the LS 2024 listing page and
printing 5 sample candidate names. Fast — no downloads, just reads the
first page of results.

Use this before running fetch_ls_2024_by_state.py for a state you
haven't confirmed. If the sample names don't match the expected
state, the code in STATE_CODES is wrong.

Usage:
    python scripts/probe_state_code.py --code S03 --cdp 9222
    python scripts/probe_state_code.py --code U01 --cdp 9222

    # Probe several at once
    python scripts/probe_state_code.py --codes S01,S02,S03,S04 --cdp 9222
"""
from __future__ import annotations
import argparse
import asyncio
import sys


async def probe(page, code: str) -> list[str]:
    url = (f"https://affidavit.eci.gov.in/CandidateCustomFilter"
           f"?electionType=24-PC-GENERAL-1-46"
           f"&election=24-PC-GENERAL-1-46"
           f"&states={code}&page=1")
    print(f"\n{'='*70}", file=sys.stderr)
    print(f"code={code}", file=sys.stderr)
    print(f"URL: {url}", file=sys.stderr)
    print(f"{'='*70}", file=sys.stderr)
    try:
        await page.goto(url, wait_until="domcontentloaded", timeout=60000)
        await asyncio.sleep(1.5)
    except Exception as e:
        print(f"  ✗ goto failed: {type(e).__name__}: {e}", file=sys.stderr)
        return []

    # First try the breadcrumb (usually contains "... >> <State Name>") —
    # cheapest and most reliable way to verify the state code even before
    # candidate cards render.
    try:
        bc = await page.query_selector("h1, .breadcrumb, [class*='breadcrumb']")
        if bc:
            t = (await bc.inner_text() or "").strip()
            if t and ">>" in t:
                print(f"  Breadcrumb: {t.splitlines()[0][:100]}", file=sys.stderr)
    except Exception:
        pass

    # Candidate cards — same selector the fetcher uses. Each profile link's
    # own text is usually the constituency name; the candidate name is a
    # sibling. Walk up to the card container and pull the heading text.
    names: list[str] = []
    try:
        # Wait a beat for the cards JS to hydrate
        await page.wait_for_selector("a[href*='show-profile']",
                                      state="attached", timeout=8000)
    except Exception:
        pass
    try:
        profile_links = await page.query_selector_all("a[href*='show-profile']")
        for a in profile_links[:10]:
            # Try common patterns: h3/h4/h5 sibling, strong text, or the
            # anchor's own text if it holds the name.
            for sel in ["h3", "h4", "h5", "strong", "span"]:
                try:
                    card = await a.evaluate_handle("el => el.closest('.card, .row, li, tr, div')")
                    heading = await card.as_element().query_selector(sel)
                    if heading:
                        t = (await heading.inner_text() or "").strip()
                        if 3 < len(t) < 80 and any(c.isalpha() for c in t):
                            names.append(t)
                            break
                except Exception:
                    continue
            if len(names) >= 5:
                break
    except Exception as e:
        print(f"  ({type(e).__name__} during card scan)", file=sys.stderr)

    if not names:
        print("  (no candidate names found — but breadcrumb above should "
              "still identify the state)", file=sys.stderr)
    else:
        print("  Sample candidates:", file=sys.stderr)
        for n in names[:5]:
            print(f"    • {n[:60]}", file=sys.stderr)
    return names


async def run(args):
    from playwright.async_api import async_playwright
    codes = [c.strip() for c in (args.codes or args.code).split(",") if c.strip()]

    async with async_playwright() as pw:
        browser = await pw.chromium.connect_over_cdp(f"http://localhost:{args.cdp}")
        ctx = browser.contexts[0] if browser.contexts else await browser.new_context()
        page = ctx.pages[0] if ctx.pages else await ctx.new_page()
        for c in codes:
            await probe(page, c)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--code",  default="", help="Single state code (e.g. S03)")
    ap.add_argument("--codes", default="", help="Comma-separated codes")
    ap.add_argument("--cdp", type=int, default=9222)
    args = ap.parse_args()
    if not args.code and not args.codes:
        sys.exit("Need --code or --codes")
    asyncio.run(run(args))


if __name__ == "__main__":
    main()
