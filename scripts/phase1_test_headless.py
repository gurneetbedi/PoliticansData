"""
HISTORICAL — RETIRED.  This script proved that pure headless mode does NOT
work against Akamai on the ECI portal. Kept in tree for reference only.
The production fetcher uses CDP attach (see phase1_test_cdp_attach.py +
fetch_eci_affidavits.py --cdp), which is what actually works.

----------------------------------------------------------------------------

Phase 1 smoke test — can we run the affidavit fetcher in HEADLESS mode
(your Chrome stays free for other work) and still get past Akamai?

WHY THIS TEST EXISTS
====================
The current `fetch_eci_affidavits.py` defaults to a VISIBLE Chrome window
because Akamai's headless-Chrome detection is aggressive on the ECI portal.
That works but ties up your Chrome so you can't browse while it runs.

Goal of this test: prove (or disprove) that the stealth patches already
in `fetch_eci_affidavits.py` are good enough to let `--headless` work
against three real candidate profile URLs. If yes → bulk-download phase
is fully unblocked: you start the script and walk away.

WHAT THIS SCRIPT DOES
=====================
1. Reads `data/eci/raw_pdfs/delhi-2025/manifest.jsonl` (already populated
   from the previous run — 1919 captured profile_urls).
2. Picks 3 candidates at random whose profile URLs we already verified
   work in headed mode.
3. Launches Playwright **headless** (the test condition) with the same
   stealth init scripts the production fetcher uses.
4. Tries to download each of their PDFs into `data/eci/_phase1_test/`.
5. Reports per-candidate: time-to-download, Akamai outcome, file size.

WHAT "SUCCESS" LOOKS LIKE
=========================
- 3/3 PDFs land in the test dir, each ~50-300 KB.
- No "akamai_blocked_*" error in stderr.
- Total wall time under 90 seconds (3 candidates × ~25 sec/each headed,
  faster in headless).

WHAT FAILURE LOOKS LIKE (and what to do)
========================================
- 0/3 PDFs land + you see "Access Denied" or "Pardon Our Interruption"
  in any error message → Akamai is detecting headless. Next step is to
  try `headless="new"` (the new Chromium headless mode is harder to
  fingerprint), then escalate to Modal-hosted Chromium with a residential
  proxy. We can decide based on the failure mode.
- 1/3 or 2/3 → flaky network, not a structural block. Retry the test.

HOW TO RUN
==========
    cd "/path/to/Politicians Project"
    source .venv-eci/bin/activate     # uses your existing playwright install
    python scripts/phase1_test_headless.py

That's it. The script writes a report to stdout — paste it back into chat.
"""
from __future__ import annotations

import asyncio
import json
import random
import sys
import time
from pathlib import Path

# Add project root to path so we can import the production fetcher's helpers
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

try:
    from playwright.async_api import async_playwright, TimeoutError as PWTimeout
except ImportError:
    sys.exit("Playwright not installed. From the .venv-eci venv, run:\n"
              "    pip install playwright && playwright install chromium")


MANIFEST_PATH = ROOT / "data/eci/raw_pdfs/delhi-2025/manifest.jsonl"
OUT_DIR       = ROOT / "data/eci/_phase1_test"
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/126.0.0.0 Safari/537.36"
)
CONTACT_HEADER = {"X-Project-Contact": "lokvani-test"}
PORTAL_HOME = "https://affidavit.eci.gov.in/"
NAV_TIMEOUT_MS = 60_000

# Stealth patches lifted verbatim from the production fetcher so this test
# is a true apples-to-apples on the only changed variable (headless=True).
STEALTH_INIT = """
    Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
    window.chrome = window.chrome || { runtime: {} };
    Object.defineProperty(navigator, 'plugins', {
        get: () => [1, 2, 3, 4, 5].map(i => ({ name: 'Plugin' + i })),
    });
    Object.defineProperty(navigator, 'languages', {
        get: () => ['en-IN', 'en-US', 'en'],
    });
    const originalQuery = window.navigator.permissions &&
                           window.navigator.permissions.query;
    if (originalQuery) {
        window.navigator.permissions.query = (parameters) => (
            parameters.name === 'notifications'
                ? Promise.resolve({ state: Notification.permission })
                : originalQuery(parameters)
        );
    }
"""


def pick_test_candidates(n: int = 3) -> list[dict]:
    """Pick `n` random candidates from the manifest whose profile URLs work."""
    rows = []
    with open(MANIFEST_PATH) as f:
        for line in f:
            try:
                r = json.loads(line)
            except json.JSONDecodeError:
                continue
            if r.get("profile_url") and r.get("download_succeeded") in (True, "True"):
                rows.append(r)
    if len(rows) < n:
        sys.exit(f"Manifest has only {len(rows)} successful entries; need {n}")
    random.seed(20260622)   # deterministic test set — easier to re-discuss
    return random.sample(rows, n)


async def is_akamai_blocked(page) -> bool:
    """Quick check for Akamai's denial pages."""
    try:
        title = (await page.title()).lower()
        body = (await page.locator("body").inner_text(timeout=2000)).lower()
        for needle in ("access denied", "pardon our interruption", "request blocked"):
            if needle in title or needle in body:
                return True
    except Exception:
        pass
    return False


async def warm_up(page) -> bool:
    """Hit the portal home so Akamai sets its sensor cookies."""
    try:
        await page.goto(PORTAL_HOME, wait_until="domcontentloaded",
                          timeout=NAV_TIMEOUT_MS)
        await asyncio.sleep(2.0)   # give Akamai sensors time to fire
        if await is_akamai_blocked(page):
            return False
        return True
    except Exception as e:
        print(f"  warm-up error: {e}", file=sys.stderr)
        return False


async def download_one(context, candidate: dict) -> dict:
    """Visit the profile, click Download, save the PDF. Return outcome dict."""
    result = {
        "name":              candidate.get("name") or candidate.get("affidavit_id"),
        "affidavit_id":      candidate.get("affidavit_id"),
        "profile_url":       candidate["profile_url"][:80] + "…",
        "akamai_blocked":    False,
        "downloaded":        False,
        "bytes":             0,
        "elapsed_sec":       0.0,
        "error":             "",
    }
    t0 = time.monotonic()
    page = await context.new_page()
    try:
        await page.goto(candidate["profile_url"], wait_until="domcontentloaded",
                          timeout=NAV_TIMEOUT_MS)
        await asyncio.sleep(1.0)

        if await is_akamai_blocked(page):
            result["akamai_blocked"] = True
            result["error"] = "Akamai denied profile page"
            return result

        # The Download button kicks off the PDF blob. We hook into the
        # `download` event so we can save it to disk.
        async with page.expect_download(timeout=NAV_TIMEOUT_MS) as dl_info:
            btn = await page.query_selector(
                "button.download-btn, button:has-text('Download'), a:has-text('Download')"
            )
            if not btn:
                result["error"] = "no download button found"
                return result
            await btn.hover()
            await asyncio.sleep(0.3)   # mouse-event-signature pacing
            await btn.click()
        download = await dl_info.value

        OUT_DIR.mkdir(parents=True, exist_ok=True)
        out_path = OUT_DIR / f"{result['name']}__{result['affidavit_id']}.pdf"
        await download.save_as(str(out_path))
        result["downloaded"] = True
        result["bytes"]      = out_path.stat().st_size
    except PWTimeout as e:
        result["error"] = f"timeout: {str(e)[:80]}"
    except Exception as e:
        result["error"] = f"{type(e).__name__}: {str(e)[:80]}"
    finally:
        await page.close()
        result["elapsed_sec"] = round(time.monotonic() - t0, 2)
    return result


async def main():
    test_set = pick_test_candidates(n=3)
    print(f"=== Phase 1 HEADLESS smoke test ===", file=sys.stderr)
    print(f"Candidates to test: {len(test_set)}", file=sys.stderr)
    for c in test_set:
        print(f"   • {c.get('name','(no name)')!r:40s} aff={c.get('affidavit_id')}",
              file=sys.stderr)

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(
            headless=True,        # ← the variable under test
            slow_mo=80,            # subtle pacing helps headless past sensors
            args=[
                "--disable-blink-features=AutomationControlled",
                "--disable-features=IsolateOrigins,site-per-process",
                "--no-sandbox",
            ],
        )
        context = await browser.new_context(
            user_agent=USER_AGENT,
            extra_http_headers=CONTACT_HEADER,
            viewport={"width": 1440, "height": 900},
            locale="en-IN",
            timezone_id="Asia/Kolkata",
            color_scheme="light",
        )
        await context.add_init_script(STEALTH_INIT)
        page = await context.new_page()

        print("\nWarming up Akamai session ...", file=sys.stderr)
        warmed = await warm_up(page)
        if not warmed:
            print("✗ Warm-up blocked by Akamai. Headless mode is NOT viable\n"
                  "  for this portal with current stealth patches.\n"
                  "  Next step: try Modal-hosted Playwright with residential proxy.",
                  file=sys.stderr)
            await browser.close()
            return
        print("✓ Warm-up succeeded", file=sys.stderr)
        await page.close()

        results = []
        for cand in test_set:
            print(f"\n→ Downloading: {cand.get('name','(no name)')} "
                  f"(aff_id={cand.get('affidavit_id')})", file=sys.stderr)
            r = await download_one(context, cand)
            results.append(r)
            line = (f"     {'✓' if r['downloaded'] else '✗'} "
                     f"{r['elapsed_sec']:6.2f}s  "
                     f"{r['bytes']:>9,} bytes  "
                     f"{r['error']}")
            print(line, file=sys.stderr)
            # Pacing — be polite even in a test
            await asyncio.sleep(2.0)

        await browser.close()

    # ── Summary ───────────────────────────────────────────────────────
    ok = sum(1 for r in results if r["downloaded"])
    print("\n" + "=" * 60, file=sys.stderr)
    print(f"RESULT: {ok}/{len(results)} downloaded in HEADLESS mode",
          file=sys.stderr)
    if ok == len(results):
        print("✓ Headless works → scale up to 50 parallel workers and walk away.",
              file=sys.stderr)
    elif ok == 0:
        print("✗ Headless blocked → escalate to Modal + residential proxy.",
              file=sys.stderr)
    else:
        print("~ Flaky → re-run; if still partial, suspect rate limit or local network.",
              file=sys.stderr)
    print("Per-candidate detail:", file=sys.stderr)
    for r in results:
        print(json.dumps(r, indent=2, ensure_ascii=False), file=sys.stderr)


if __name__ == "__main__":
    asyncio.run(main())
