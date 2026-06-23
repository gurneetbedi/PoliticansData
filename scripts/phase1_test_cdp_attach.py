"""
Phase 1 final escalation — CDP attach to a dedicated, hidden Chrome window.

WHY THIS APPROACH
=================
Pure headless = Akamai blocks warm-up.
Warm-then-headless = Akamai rejects warmed cookies in the headless context.

Both fail because Akamai fingerprints the BROWSER (TLS handshake / JA3,
WebGL renderer, navigator surface) — not just the cookies. So we have to
keep a real Chrome alive for the session.

But "real Chrome" doesn't have to mean "your everyday Chrome." We launch
a SEPARATE Chrome instance with:
  - --remote-debugging-port=9222  → Chrome opens a DevTools Protocol port
  - --user-data-dir=~/.eci-chrome → completely separate profile, doesn't
                                    touch your bookmarks/extensions/cookies
This Chrome window opens; you solve Akamai once (5–15 seconds); you
minimize it (Cmd+M). Then any number of background scripts can attach
to that running Chrome via CDP, open background tabs, do their thing, and
close those tabs — all without changing focus or interrupting your work.

USER FLOW
=========
ONE-TIME (or whenever Chrome dies / Akamai expires):
    python scripts/phase1_test_cdp_attach.py --launch
    # → A blank Chrome window opens to affidavit.eci.gov.in.
    # → Wait for the "Loading…" spinner to settle. Solve Akamai if it
    #   throws a "Pardon Our Interruption" page (rare).
    # → Hit Cmd+M to minimize. Don't close it.

ANYTIME AFTER (no attention needed):
    python scripts/phase1_test_cdp_attach.py
    # → Connects to that running Chrome via CDP. Downloads 3 random PDFs
    #   in background tabs. The Chrome window stays minimized; your real
    #   Chrome is untouched.

OUTCOMES
========
3/3 ✓ → bottleneck solved permanently. Scale this same pattern up to the
        full 675 (already done) and the future 2020 + 2015 cycles.
0/3   → the Chrome window is doing something Akamai still doesn't like
        (e.g. the new profile triggered re-challenge). Solve Akamai
        explicitly in the visible window before re-running.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import random
import subprocess
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

CDP_PORT = 9222
CDP_URL  = f"http://localhost:{CDP_PORT}"

# Dedicated profile dir — lives in user home so it survives across sessions.
# Completely separate from ~/Library/Application Support/Google/Chrome
ECI_PROFILE_DIR = Path.home() / ".eci-chrome-profile"

CHROME_PATHS = [
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "/Applications/Google Chrome Canary.app/Contents/MacOS/Google Chrome Canary",
    "/Applications/Brave Browser.app/Contents/MacOS/Brave Browser",
    "/Applications/Chromium.app/Contents/MacOS/Chromium",
]
PORTAL_HOME = "https://affidavit.eci.gov.in/"
NAV_TIMEOUT_MS = 60_000


def find_chrome() -> str:
    """Locate a Chromium-family binary that supports --remote-debugging-port."""
    for p in CHROME_PATHS:
        if Path(p).exists():
            return p
    sys.exit("No Chrome/Chromium binary found. Install Google Chrome from "
              "google.com/chrome, then re-run.")


def launch_chrome():
    """Open a dedicated Chrome window with CDP enabled. Don't wait for it."""
    chrome = find_chrome()
    ECI_PROFILE_DIR.mkdir(exist_ok=True)
    # Subprocess flags ensure the launched Chrome runs detached from this
    # Python process — it stays alive after this script exits.
    args = [
        chrome,
        f"--remote-debugging-port={CDP_PORT}",
        f"--user-data-dir={ECI_PROFILE_DIR}",
        "--new-window",
        PORTAL_HOME,
    ]
    print(f"Launching Chrome with CDP port {CDP_PORT} …", file=sys.stderr)
    print(f"   profile: {ECI_PROFILE_DIR}", file=sys.stderr)
    print(f"   binary:  {chrome}", file=sys.stderr)
    subprocess.Popen(
        args,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    time.sleep(2.0)
    print("\n" + "─" * 60, file=sys.stderr)
    print("Chrome should now be open. Steps:", file=sys.stderr)
    print(f"  1. Wait for {PORTAL_HOME} to finish loading.", file=sys.stderr)
    print(f"  2. If you see 'Pardon Our Interruption', wait or solve it.",
          file=sys.stderr)
    print(f"  3. When the affidavit search form is visible, minimize the", file=sys.stderr)
    print(f"     window (Cmd+M). DO NOT close it.", file=sys.stderr)
    print(f"  4. Re-run this script WITHOUT --launch to download 3 PDFs.",
          file=sys.stderr)
    print("─" * 60, file=sys.stderr)


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


async def cdp_test():
    test_set = pick_test_candidates(n=3)
    print("=== CDP-ATTACH download test ===", file=sys.stderr)
    print(f"Connecting to running Chrome at {CDP_URL}", file=sys.stderr)
    print(f"Candidates:", file=sys.stderr)
    for c in test_set:
        print(f"   • {c.get('name','(no name)')!r:40s} aff={c.get('affidavit_id')}",
              file=sys.stderr)

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    async with async_playwright() as pw:
        try:
            browser = await pw.chromium.connect_over_cdp(CDP_URL)
        except Exception as e:
            print(f"\n✗ Could not connect to Chrome at {CDP_URL}.\n"
                  f"   Reason: {e}\n"
                  f"   Did you run `--launch` first and leave the window open?",
                  file=sys.stderr)
            return

        # Reuse the EXISTING context that the real Chrome is using — that's
        # the one with Akamai's session cookies. Don't create a new context;
        # that would start fresh.
        if not browser.contexts:
            print("✗ No browser contexts available. Chrome launched but with\n"
                  "  no open windows? Re-run --launch.", file=sys.stderr)
            return
        context = browser.contexts[0]
        print(f"✓ Attached. Existing context has {len(context.pages)} open "
              f"page(s).", file=sys.stderr)

        # Re-validate that Akamai is happy in this context (we expect yes —
        # the user just solved it manually in this very Chrome).
        page = await context.new_page()
        await page.goto(PORTAL_HOME, wait_until="domcontentloaded",
                          timeout=NAV_TIMEOUT_MS)
        if await is_akamai_blocked(page):
            print("\n✗ Akamai still blocking even in the real Chrome window.\n"
                  "  Switch to the visible window, wait/solve Akamai, then re-run.",
                  file=sys.stderr)
            await page.close()
            return
        print("✓ Akamai accepts session. Downloading …", file=sys.stderr)
        await page.close()

        # Download attempts in background tabs
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

        # Don't close the browser — user wants Chrome to stay running
        # for the next CDP attach.

    ok = sum(1 for r in results if r["downloaded"])
    print("\n" + "=" * 60, file=sys.stderr)
    print(f"RESULT: {ok}/{len(results)} downloaded via CDP attach",
          file=sys.stderr)
    if ok == len(results):
        print("✓ CDP ATTACH WORKS — bottleneck permanently solved.\n"
              "  Going forward: keep the dedicated Chrome window minimized.\n"
              "  Run bulk fetchers attached to it; your real Chrome is free.",
              file=sys.stderr)
    elif ok == 0:
        print("✗ Even CDP attach is blocked. We're in residential-proxy /\n"
              "  Modal territory. Tell me and we'll plan the next step.",
              file=sys.stderr)
    else:
        print("~ Partial — likely transient. Re-run; if still partial, the\n"
              "  download click pattern needs more humanization.",
              file=sys.stderr)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--launch", action="store_true",
                    help="One-time: open a dedicated Chrome with CDP port. "
                         "Then minimize it and re-run without --launch.")
    args = ap.parse_args()
    if args.launch:
        launch_chrome()
    else:
        asyncio.run(cdp_test())


if __name__ == "__main__":
    main()
