"""
HISTORICAL — RETIRED.  This script proved that warmed Akamai cookies do NOT
survive into a headless Playwright context — Akamai's fingerprinting is
done at the browser binary level (TLS / WebGL / navigator), not just
cookies. Kept in tree for reference only. The production fetcher uses
CDP attach (see phase1_test_cdp_attach.py + fetch_eci_affidavits.py --cdp).

----------------------------------------------------------------------------

Phase 1 follow-up smoke test — does a HEADED warm-up + HEADLESS download
cycle work? This is the cheapest possible fix for the bottleneck.

CONTEXT
=======
The previous test (`phase1_test_headless.py`) confirmed that pure headless
mode is blocked by Akamai at warm-up — even with stealth patches.

This test asks a different question: if a real Chrome window opens **once**,
loads the portal long enough for Akamai's sensor script to set its cookies
(_abck, bm_sz, bm_sv, plus any Laravel session), then we **save the storage
state** to disk and close that window — can a subsequent **headless** run
use those cookies to bypass Akamai's fingerprinting?

The bet: Akamai's runtime checks fire on warm-up. Once a session is warmed,
the cookies act as a "you've already proved you're a browser" token for
some window (typically 1–24 hours).

WHY THIS MATTERS
================
If this works, the user-facing flow becomes:
  1. Once a day (or whenever Akamai cookies expire), you run
     `python scripts/phase1_test_warm_then_headless.py --warm`
     → a Chrome window opens to affidavit.eci.gov.in
     → you watch it for ~10 seconds while Akamai's challenge resolves
     → it closes automatically
  2. The bulk fetcher then runs entirely headless. Your Chrome is free.
     You start the script and walk away.

OUTCOMES
========
If 3/3 download in headless mode using warmed cookies → bottleneck solved.
If 0/3 → Akamai is doing per-session fingerprinting that won't transfer
to a headless context. Next fallback is CDP-attach (the script connects
to a Chrome you keep open in a hidden window).

USAGE
=====
    cd "/path/to/Politicians Project"
    source .venv-eci/bin/activate

    # Step A — warm the session (one-time per day-ish)
    python scripts/phase1_test_warm_then_headless.py --warm
    # → Chrome opens, loads portal, sleeps 15s, saves state, closes.

    # Step B — run headless using the warmed state
    python scripts/phase1_test_warm_then_headless.py
    # → no visible Chrome; downloads 3 random PDFs.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import random
import sys
import time
from pathlib import Path

try:
    from playwright.async_api import async_playwright, TimeoutError as PWTimeout
except ImportError:
    sys.exit("Playwright not installed. From the .venv-eci venv, run:\n"
              "    pip install playwright && playwright install chromium")


ROOT = Path(__file__).resolve().parent.parent
MANIFEST_PATH = ROOT / "data/eci/raw_pdfs/delhi-2025/manifest.jsonl"
OUT_DIR       = ROOT / "data/eci/_phase1_test"
STATE_PATH    = ROOT / "data/eci/_akamai_state.json"

PORTAL_HOME = "https://affidavit.eci.gov.in/"
NAV_TIMEOUT_MS = 60_000

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/126.0.0.0 Safari/537.36"
)
STEALTH_INIT = """
    Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
    window.chrome = window.chrome || { runtime: {} };
    Object.defineProperty(navigator, 'plugins', {
        get: () => [1, 2, 3, 4, 5].map(i => ({ name: 'Plugin' + i })),
    });
    Object.defineProperty(navigator, 'languages', {
        get: () => ['en-IN', 'en-US', 'en'],
    });
"""


async def is_akamai_blocked(page) -> bool:
    try:
        title = (await page.title()).lower()
        body = (await page.locator("body").inner_text(timeout=2000)).lower()
        for needle in ("access denied", "pardon our interruption", "request blocked"):
            if needle in title or needle in body:
                return True
    except Exception:
        pass
    return False


async def warm(*, dwell_sec: int = 15):
    """Open visible Chrome, hit portal, save storage state."""
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=False,
                                             args=["--disable-blink-features=AutomationControlled"])
        context = await browser.new_context(
            user_agent=USER_AGENT,
            viewport={"width": 1440, "height": 900},
            locale="en-IN",
            timezone_id="Asia/Kolkata",
        )
        await context.add_init_script(STEALTH_INIT)
        page = await context.new_page()
        print(f"Opening {PORTAL_HOME} — watch the window …", file=sys.stderr)
        await page.goto(PORTAL_HOME, wait_until="domcontentloaded",
                          timeout=NAV_TIMEOUT_MS)
        # Give Akamai's sensor script time to run + drop cookies
        for i in range(dwell_sec, 0, -1):
            print(f"   warm-up: {i}s left … (do not close window)", file=sys.stderr)
            await asyncio.sleep(1.0)
        # Confirm not blocked
        if await is_akamai_blocked(page):
            print("✗ Akamai still showing block page; warm-up FAILED.",
                  file=sys.stderr)
            await browser.close()
            sys.exit(2)
        # Save context to disk for the headless run
        await context.storage_state(path=str(STATE_PATH))
        await browser.close()
        size = STATE_PATH.stat().st_size
        print(f"✓ Warm-up saved {size:,} bytes of session state to "
              f"{STATE_PATH}", file=sys.stderr)


def pick_test_candidates(n: int = 3) -> list[dict]:
    rows = []
    with open(MANIFEST_PATH) as f:
        for line in f:
            try: r = json.loads(line)
            except: continue
            if r.get("profile_url") and r.get("download_succeeded") in (True, "True"):
                rows.append(r)
    if len(rows) < n:
        sys.exit(f"Manifest has only {len(rows)} successful entries; need {n}")
    random.seed(20260622)
    return random.sample(rows, n)


async def headless_test():
    if not STATE_PATH.exists():
        sys.exit(f"No warmed session state at {STATE_PATH}. "
                  f"Run with --warm first.")
    test_set = pick_test_candidates(n=3)
    print(f"=== HEADLESS download using warmed cookies ===", file=sys.stderr)
    print(f"State file: {STATE_PATH} "
          f"({STATE_PATH.stat().st_size:,} bytes)", file=sys.stderr)
    print(f"Candidates: {len(test_set)}", file=sys.stderr)
    for c in test_set:
        print(f"   • {c.get('name','(no name)')!r:40s} aff={c.get('affidavit_id')}",
              file=sys.stderr)

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(
            headless=True,
            args=["--disable-blink-features=AutomationControlled", "--no-sandbox"],
        )
        context = await browser.new_context(
            storage_state=str(STATE_PATH),   # ← the magic: carry warmed cookies
            user_agent=USER_AGENT,
            viewport={"width": 1440, "height": 900},
            locale="en-IN",
            timezone_id="Asia/Kolkata",
        )
        await context.add_init_script(STEALTH_INIT)

        # Quick re-check warm-up still valid in headless context
        page = await context.new_page()
        await page.goto(PORTAL_HOME, wait_until="domcontentloaded",
                          timeout=NAV_TIMEOUT_MS)
        if await is_akamai_blocked(page):
            print("\n✗ Akamai REJECTED warmed cookies in headless context.\n"
                  "   Conclusion: warm-then-headless doesn't help.\n"
                  "   Next escalation: CDP-attach to a hidden Chrome window\n"
                  "   (you keep the Chrome window minimized; we drive it).",
                  file=sys.stderr)
            await browser.close()
            return
        print("✓ Akamai accepted warmed cookies in headless. Downloading …",
              file=sys.stderr)
        await page.close()

        # Download attempts
        results = []
        for cand in test_set:
            t0 = time.monotonic()
            r = {"name": cand.get("name") or cand.get("affidavit_id"),
                 "aff": cand.get("affidavit_id"),
                 "downloaded": False, "bytes": 0, "error": ""}
            page = await context.new_page()
            try:
                await page.goto(cand["profile_url"],
                                  wait_until="domcontentloaded",
                                  timeout=NAV_TIMEOUT_MS)
                await asyncio.sleep(1.0)
                if await is_akamai_blocked(page):
                    r["error"] = "akamai_blocked_on_profile"
                else:
                    async with page.expect_download(timeout=NAV_TIMEOUT_MS) as dl:
                        btn = await page.query_selector(
                            "button.download-btn, button:has-text('Download'), "
                            "a:has-text('Download')")
                        if not btn:
                            r["error"] = "no_download_button"
                        else:
                            await btn.hover()
                            await asyncio.sleep(0.3)
                            await btn.click()
                    if not r["error"]:
                        d = await dl.value
                        out = OUT_DIR / f"{r['name']}__{r['aff']}.pdf"
                        await d.save_as(str(out))
                        r["downloaded"] = True
                        r["bytes"] = out.stat().st_size
            except PWTimeout as e:
                r["error"] = f"timeout: {str(e)[:80]}"
            except Exception as e:
                r["error"] = f"{type(e).__name__}: {str(e)[:80]}"
            finally:
                await page.close()
                r["elapsed_sec"] = round(time.monotonic() - t0, 2)
            results.append(r)
            mark = "✓" if r["downloaded"] else "✗"
            print(f"   {mark} {r['elapsed_sec']:6.2f}s  {r['bytes']:>9,} bytes  "
                  f"{r['name']}  {r['error']}", file=sys.stderr)
            await asyncio.sleep(2.0)

        await browser.close()

    ok = sum(1 for r in results if r["downloaded"])
    print("\n" + "=" * 60, file=sys.stderr)
    print(f"RESULT: {ok}/{len(results)} downloaded HEADLESS using warmed cookies",
          file=sys.stderr)
    if ok == len(results):
        print("✓ WARM-THEN-HEADLESS WORKS. Bottleneck solved.\n"
              "  Daily flow: --warm once, then run headless fetcher unattended.",
              file=sys.stderr)
    elif ok == 0:
        print("✗ Warmed cookies don't survive into headless. Escalate to\n"
              "  CDP-attach or Modal.",
              file=sys.stderr)
    else:
        print("~ Partial. Likely rate limit or per-request Akamai signal\n"
              "  (TLS / JA3 fingerprint). Investigate Modal next.",
              file=sys.stderr)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--warm", action="store_true",
                    help="Step A: open visible Chrome, save Akamai cookies, exit.")
    ap.add_argument("--dwell-sec", type=int, default=15,
                    help="How long to keep the visible Chrome open during warm-up.")
    args = ap.parse_args()

    if args.warm:
        asyncio.run(warm(dwell_sec=args.dwell_sec))
    else:
        asyncio.run(headless_test())


if __name__ == "__main__":
    main()
