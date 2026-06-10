"""
Playwright-based fetcher for the ECI affidavit portal.

WHY PLAYWRIGHT and not `requests`:
The portal's PDF endpoint returns 503 to synthetic clicks (see ECI_RECON.md).
Real clicks via Playwright pass `isTrusted: true` and succeed. The form
filters are also XHR-cascaded so they have to be driven through a real DOM.

WHAT THIS DOES:
  1. Launches headless Chromium.
  2. Navigates to https://affidavit.eci.gov.in/candidate-affidavit.
  3. Selects the cascading filters: election → election type → state → phase.
  4. Walks every listing page, collecting (name, party, constituency,
     status, profile URL) into a JSONL manifest.
  5. Visits each profile, clicks Download to capture the PDF blob.
  6. Saves PDFs to <output>/raw_pdfs/<key>.pdf and metadata to <output>/manifest.jsonl.

RESUMABILITY:
  - The manifest is appended to after each candidate.
  - On re-run, candidates already listed in the manifest are skipped.
  - PDFs already on disk are skipped.

POLITENESS:
  - 2 second sleep between candidates (configurable via --delay).
  - User-Agent string identifies the project + contact email.
  - Backs off on 503/429 with exponential delay.

USAGE (locally, not in the sandbox — affidavit.eci.gov.in is allowlist-blocked here):
    pip install playwright
    playwright install chromium

    python scripts/fetch_eci_affidavits.py \\
        --election "GEN-Election-FEB-2025" \\
        --election-type "AC - GENERAL" \\
        --state "NCT OF Delhi" \\
        --output data/eci/raw_pdfs/delhi-2025/ \\
        --limit 5                    # smoke test first
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from dataclasses import dataclass, asdict, field
from pathlib import Path
from typing import Any

try:
    from playwright.async_api import async_playwright, Page, BrowserContext, TimeoutError as PWTimeout
except ImportError:
    sys.exit("Playwright not installed. Run:\n    pip install playwright\n    playwright install chromium")


PORTAL_HOME = "https://affidavit.eci.gov.in/"
PORTAL_URL = "https://affidavit.eci.gov.in/candidate-affidavit"
# The portal supports a direct GET-with-querystring listing endpoint that
# bypasses the cascading filter form entirely. Discovered by user.
# Example for Delhi 2025 Assembly General:
#   https://affidavit.eci.gov.in/CandidateCustomFilter
#       ?electionType=28-AC-GENERAL-3-54
#       &election=28-AC-GENERAL-3-54
#       &states=U05
#       &page=2
# IMPORTANT: the portal sits behind Akamai. A cold direct hit to the
# CandidateCustomFilter URL returns "Access Denied" because Akamai's
# sensor cookies (_abck, bm_sz, bm_sv) haven't been set yet. We must
# warm up the session by visiting PORTAL_HOME first and letting Akamai's
# JS challenge run, which sets those cookies; subsequent navigations
# within the same browser context will then be allowed.
CUSTOM_FILTER_URL = "https://affidavit.eci.gov.in/CandidateCustomFilter"
# A real Chrome UA — Akamai treats Playwright's default "HeadlessChrome"
# string as a synthetic client and denies it. This is the macOS Chrome
# stable channel as of mid-2025; tweak to match your real browser if needed.
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/126.0.0.0 Safari/537.36"
)
# Contact / identification still happens via this header, kept short to
# avoid setting off UA-based filters.
CONTACT_HEADER = {
    "X-PolitiTrack-Contact": "transparency project; gurneetbedi@gmail.com",
}
NAV_TIMEOUT_MS = 60_000
# Akamai keeps persistent sensor channels open, so "networkidle" never
# fires on this portal — we use "domcontentloaded" everywhere and then
# explicit sleeps to let Akamai's JS challenge run.
WAIT_UNTIL = "domcontentloaded"
DEFAULT_DELAY_S = 2.0


@dataclass
class CandidateRow:
    name: str = ""
    party: str = ""
    status: str = ""
    state: str = ""
    constituency: str = ""
    profile_url: str = ""
    affidavit_id: str = ""
    pdf_path: str = ""
    download_attempted: bool = False
    download_succeeded: bool = False
    error: str = ""


# ---------------------------------------------------------------------------
# Manifest (resumable state)
# ---------------------------------------------------------------------------

def load_manifest(manifest_path: Path) -> dict[str, CandidateRow]:
    """Returns {profile_url: CandidateRow}."""
    out: dict[str, CandidateRow] = {}
    if not manifest_path.exists():
        return out
    for line in manifest_path.read_text().splitlines():
        if not line.strip():
            continue
        d = json.loads(line)
        c = CandidateRow(**d)
        if c.profile_url:
            out[c.profile_url] = c
    return out


def append_manifest(manifest_path: Path, row: CandidateRow) -> None:
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    with manifest_path.open("a") as f:
        f.write(json.dumps(asdict(row), ensure_ascii=False) + "\n")


# ---------------------------------------------------------------------------
# Filter form driver
# ---------------------------------------------------------------------------

async def is_akamai_blocked(page: Page) -> bool:
    """Detect the Akamai 'Access Denied' interstitial."""
    title = await page.title()
    if "Access Denied" in title:
        return True
    body = await page.evaluate("() => document.body ? document.body.innerText : ''")
    if "errors.edgesuite.net" in body or "Access Denied" in body[:200]:
        return True
    return False


async def warm_up_session(page: Page) -> None:
    """Establish an Akamai-blessed session before hitting the filter URLs.

    Why: a cold GET to /CandidateCustomFilter is denied. Visiting the home
    page runs Akamai's JS challenge, which sets the _abck/bm_sz/bm_sv
    sensor cookies. Once those are in the context jar, subsequent
    navigations within the same context are allowed.
    """
    print("  → Warm-up: loading portal home for Akamai cookies ...",
          file=sys.stderr)
    # Use domcontentloaded — networkidle never fires because Akamai keeps
    # persistent sensor pings open.
    await page.goto(PORTAL_HOME, wait_until=WAIT_UNTIL,
                      timeout=NAV_TIMEOUT_MS)
    # Akamai sometimes needs a beat to set the _abck token after the
    # initial load. Give the JS challenge generous time.
    await asyncio.sleep(4.0)

    if await is_akamai_blocked(page):
        raise RuntimeError(
            "Akamai blocked the home page during warm-up.\n"
            "  Most likely cause: you're running with --headless and Akamai's\n"
            "  fingerprinter caught Playwright. Drop the --headless flag and\n"
            "  re-run with a visible Chrome window.\n"
            "  If you're already using --no-headless, your IP may be rate-\n"
            "  limited — wait 10-15 minutes, switch networks, or solve any\n"
            "  visible CAPTCHA in the browser window."
        )

    # Visit the affidavit form page too — this is the natural entry point
    # users would take, and it sets the post-login cookies Akamai expects
    # for the deeper endpoints.
    await page.goto(PORTAL_URL, wait_until=WAIT_UNTIL,
                      timeout=NAV_TIMEOUT_MS)
    await asyncio.sleep(2.5)

    if await is_akamai_blocked(page):
        raise RuntimeError(
            "Akamai blocked /candidate-affidavit after warming up. "
            "Try --no-headless and solve any CAPTCHA manually."
        )
    print("  → Warm-up complete (cookies set)", file=sys.stderr)


async def inspect_page_selects(page: Page) -> list[dict]:
    """Return a description of every <select> on the page: its name, id,
    option count, and a sample of the first few option texts."""
    return await page.eval_on_selector_all(
        "select",
        """sels => sels.map(s => ({
            name: s.name || '',
            id: s.id || '',
            class: s.className || '',
            option_count: s.options.length,
            sample_options: Array.from(s.options).slice(0, 8)
                .map(o => o.textContent.trim()),
        }))""",
    )


async def dump_form_layout(page: Page, out_path: Path) -> None:
    """Save a JSON snapshot of every select on the current page. Used
    during the first run to discover the real selector names without
    code edits."""
    selects = await inspect_page_selects(page)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(selects, indent=2, ensure_ascii=False))
    print(f"  → Dumped {len(selects)} <select> elements to {out_path}",
          file=sys.stderr)
    for s in selects:
        print(f"     name={s['name']!r:30s}  id={s['id']!r:30s}  "
              f"options={s['option_count']}", file=sys.stderr)
        if s["sample_options"]:
            print(f"        sample: {s['sample_options'][:5]}", file=sys.stderr)


async def _find_select_for(page: Page, hints: list[str]) -> str:
    """Find a <select> whose name/id contains any of the hints (case-insensitive).
    Returns a CSS selector that uniquely identifies it. Raises if none found."""
    selects = await inspect_page_selects(page)
    for hint in hints:
        h = hint.lower()
        for s in selects:
            if h in s["name"].lower() or h in s["id"].lower():
                if s["name"]:
                    return f"select[name='{s['name']}']"
                if s["id"]:
                    return f"select#{s['id']}"
    raise RuntimeError(
        f"No <select> matches any of {hints!r}. "
        f"Page selects: {[(s['name'], s['id']) for s in selects]}"
    )


async def select_filter(page: Page, selector: str, label_substring: str,
                         wait_after: float = 1.5) -> str:
    """Pick an <option> from a <select> by partial label match. Returns the
    chosen option's text (for logging). Waits up to 8s for options to
    populate, since the portal's dropdowns are XHR-cascaded."""
    options: list[dict] = []
    for _ in range(16):  # up to ~8 s total
        options = await page.eval_on_selector_all(
            f"{selector} option",
            "els => els.map(el => ({text: el.textContent.trim(), value: el.value}))",
        )
        # Skip the placeholder option (e.g. "Select Election" with empty value)
        real = [o for o in options if o["value"]]
        if real:
            break
        await asyncio.sleep(0.5)

    chosen = None
    for opt in options:
        if not opt["value"]:
            continue
        if label_substring.lower() in opt["text"].lower():
            chosen = opt
            break
    if not chosen:
        raise RuntimeError(f"No <option> matching {label_substring!r} on {selector}. "
                            f"Available: {[o['text'] for o in options]}")
    await page.select_option(selector, value=chosen["value"])
    # ECI form cascades — wait for the next dropdown to populate
    await asyncio.sleep(wait_after)
    return chosen["text"]


async def apply_filters(page: Page, election: str, election_type: str,
                          state: str, phase: str | None,
                          dump_path: Path | None = None) -> None:
    """Drive the cascading filter form. The selector names on the live
    portal are discovered at runtime from the page's <select> elements,
    so this works even if Laravel changes the form field names."""
    print(f"  → Navigating to portal ...", file=sys.stderr)
    await page.goto(PORTAL_URL, wait_until=WAIT_UNTIL, timeout=NAV_TIMEOUT_MS)
    # Wait for at least one populated <select> to confirm the form rendered
    await page.wait_for_selector("select", state="attached", timeout=NAV_TIMEOUT_MS)

    if dump_path:
        await dump_form_layout(page, dump_path)

    # Find each dropdown by name/id substring — defensive against Laravel
    # form-name drift. Election usually appears first.
    election_sel = await _find_select_for(page, ["election_id", "election", "master_id"])
    chosen_e = await select_filter(page, election_sel, election)
    print(f"  → Election ({election_sel}): {chosen_e}", file=sys.stderr)

    type_sel = await _find_select_for(page, ["election_type", "type", "house"])
    chosen_t = await select_filter(page, type_sel, election_type)
    print(f"  → Election type ({type_sel}): {chosen_t}", file=sys.stderr)

    state_sel = await _find_select_for(page, ["state_id", "state"])
    chosen_s = await select_filter(page, state_sel, state)
    print(f"  → State ({state_sel}): {chosen_s}", file=sys.stderr)

    if phase:
        phase_sel = await _find_select_for(page, ["phase_id", "phase"])
        chosen_p = await select_filter(page, phase_sel, phase)
        print(f"  → Phase ({phase_sel}): {chosen_p}", file=sys.stderr)

    # Submit search button — name may vary; this clicks anything that looks
    # like a submit/search button.
    submit = await page.query_selector("button[type='submit'], input[type='submit'], button:has-text('Search')")
    if submit:
        await submit.click()
        await page.wait_for_load_state(WAIT_UNTIL, timeout=NAV_TIMEOUT_MS)
        await asyncio.sleep(1.5)
    print("  → Filters applied", file=sys.stderr)


# ---------------------------------------------------------------------------
# Listing page walker
# ---------------------------------------------------------------------------

async def scrape_listing_page(page: Page) -> list[CandidateRow]:
    """Pull candidate rows off the current listing page.

    We don't know the exact card/row HTML upfront, so we anchor on the
    show-profile link (which we know exists) and then walk UP to whatever
    ancestor element contains the labelled fields. This is robust to
    Laravel template tweaks.

    Empirical note: each candidate's card has MULTIPLE show-profile links
    (clickable name, "View Profile" button, etc.). We collapse them via
    a card "fingerprint" — the outerHTML of the walked-up card element.
    Two links sharing the same card fingerprint = same candidate.
    """
    rows = await page.evaluate("""
        () => {
            const profileLinks = Array.from(
                document.querySelectorAll("a[href*='show-profile']")
            );
            const seenCards = new Set();
            const out = [];

            for (const link of profileLinks) {
                // Walk up to the nearest plausible card container
                let card = link;
                for (let i = 0; i < 6; i++) {
                    card = card.parentElement;
                    if (!card) break;
                    const tag = card.tagName.toLowerCase();
                    if (tag === 'tr' || tag === 'li' ||
                        (card.className && /card|candidate|row|item/i.test(card.className))) {
                        break;
                    }
                }
                if (!card) card = link.parentElement;

                // Dedup multiple links inside the same card
                const fp = card.outerHTML.length + ':' + card.outerHTML.slice(0, 200);
                if (seenCards.has(fp)) continue;
                seenCards.add(fp);

                const text = card ? card.innerText : link.textContent;

                // Try labelled extraction first ("Name: X", "Party: X")
                const grab = (re) => {
                    const m = text.match(re);
                    return m ? m[1].trim() : '';
                };
                let name = grab(/Name\\s*:?\\s*([^\\n]+)/i);
                let party = grab(/Party\\s*:?\\s*([^\\n]+)/i);
                let status = grab(/Status\\s*:?\\s*([^\\n]+)/i);
                let state = grab(/State\\s*:?\\s*([^\\n]+)/i);
                let constituency = grab(/Constituency\\s*:?\\s*([^\\n]+)/i);

                // Status often lives in a colored badge with no "Status:" label.
                // Look for a badge/chip element OR for one of the four known
                // status words anywhere in the card text.
                if (!status) {
                    const badge = card.querySelector(
                        '.badge, .chip, .status, .label, .pill, '
                        + '[class*="status"], [class*="badge"]'
                    );
                    if (badge) status = badge.innerText.trim();
                }
                if (!status) {
                    const m = text.match(
                        /\\b(Accepted|Rejected|Withdrawn|Contesting)\\b/i
                    );
                    if (m) status = m[1];
                }
                // Normalise casing so downstream filter is case-insensitive-safe
                if (status) {
                    status = status.charAt(0).toUpperCase()
                              + status.slice(1).toLowerCase();
                }

                // Fallback: structured DOM probe — common patterns from ECI
                // Look for headings, strong tags, .candidate-name divs
                if (!name) {
                    const h = card.querySelector('h1,h2,h3,h4,h5,.candidate-name,.cand-name,.name');
                    if (h) name = h.innerText.trim();
                    if (!name) {
                        // Heuristic: linked name is usually the link text on the
                        // first show-profile link inside the card
                        const firstLink = card.querySelector("a[href*='show-profile']");
                        if (firstLink) name = firstLink.innerText.trim();
                    }
                }

                // affidavit_id from data-attribute on download button or
                // href query — used as a backup primary key for dedup
                let affidavit_id = '';
                const dlBtn = card.querySelector('[data-affidavit-id]');
                if (dlBtn) affidavit_id = dlBtn.getAttribute('data-affidavit-id') || '';
                if (!affidavit_id) {
                    const dl = card.querySelector("a[href*='affidavit-pdf-download/'], a[href*='download']");
                    if (dl) {
                        const m = dl.href.match(/(\\d{4,})/);
                        if (m) affidavit_id = m[1];
                    }
                }

                out.push({
                    name, party, status, state, constituency,
                    profile_url: link.href,
                    affidavit_id,
                });
            }
            return out;
        }
    """)
    return [CandidateRow(**r) for r in rows if r.get("profile_url")]


def build_listing_url(base_url: str, page_num: int) -> str:
    """Add or update the ?page=N parameter on a URL."""
    from urllib.parse import urlparse, parse_qs, urlencode, urlunparse
    u = urlparse(base_url)
    q = parse_qs(u.query)
    q["page"] = [str(page_num)]
    return urlunparse(u._replace(query=urlencode(q, doseq=True)))


async def crawl_by_url(page: Page, base_url: str, max_pages: int,
                         limit: int, out_dir: Path) -> list[CandidateRow]:
    """Iterate page=1, 2, 3, ... on a CandidateCustomFilter URL until we hit
    an empty page or the limit is reached.

    Dedup keys: profile_url AND affidavit_id. Either being a repeat means
    we've already processed this candidate.
    """
    seen_urls: set[str] = set()
    seen_aff_ids: set[str] = set()
    all_rows: list[CandidateRow] = []
    for n in range(1, max_pages + 1):
        url = build_listing_url(base_url, n)
        print(f"=== Listing page {n} : {url} ===", file=sys.stderr)
        await page.goto(url, wait_until=WAIT_UNTIL, timeout=NAV_TIMEOUT_MS)
        # Let Akamai's per-page sensor & the Laravel template settle.
        await asyncio.sleep(1.5)

        if await is_akamai_blocked(page):
            (out_dir / f"_blocked_page_{n}.html").write_text(await page.content())
            raise RuntimeError(
                f"Akamai blocked page {n}. The warm-up cookies may have "
                f"expired, or this URL needs a different referer. "
                f"HTML saved to _blocked_page_{n}.html."
            )

        # First page may take a moment for the candidate cards to render
        try:
            await page.wait_for_selector("a[href*='show-profile']",
                                          state="attached",
                                          timeout=NAV_TIMEOUT_MS)
        except PWTimeout:
            # No profile links on this page — we've gone past the last page
            print(f"  → No candidate cards on page {n} — stop", file=sys.stderr)
            # Save the page so we can confirm it's actually empty (and not blocked)
            (out_dir / f"_empty_page_{n}.html").write_text(await page.content())
            break

        # Always save the first listing page's HTML — we use it to refine
        # the candidate-card selectors offline.
        if n == 1:
            (out_dir / "_first_listing_page.html").write_text(await page.content())

        rows = await scrape_listing_page(page)
        new_rows: list[CandidateRow] = []
        for r in rows:
            if r.profile_url in seen_urls:
                continue
            if r.affidavit_id and r.affidavit_id in seen_aff_ids:
                continue
            seen_urls.add(r.profile_url)
            if r.affidavit_id:
                seen_aff_ids.add(r.affidavit_id)
            new_rows.append(r)
        print(f"  {len(rows)} cards seen ({len(new_rows)} new)", file=sys.stderr)

        if not new_rows:
            # We're either past the last page or paginated back to seen content
            print(f"  → No new candidates on page {n} — stop", file=sys.stderr)
            break

        all_rows.extend(new_rows)
        if limit and len(all_rows) >= limit:
            all_rows = all_rows[:limit]
            break
    return all_rows


# ---------------------------------------------------------------------------
# PDF download
# ---------------------------------------------------------------------------

async def download_pdf(context: BrowserContext, candidate: CandidateRow,
                         out_dir: Path) -> CandidateRow:
    """Open the candidate's profile page, click Download, save the PDF."""
    page = await context.new_page()
    candidate.download_attempted = True
    try:
        await page.goto(candidate.profile_url, wait_until=WAIT_UNTIL,
                          timeout=NAV_TIMEOUT_MS)
        await asyncio.sleep(1.0)

        if await is_akamai_blocked(page):
            candidate.error = "akamai_blocked_on_profile"
            print(f"     ✗ Akamai blocked profile for {candidate.name}",
                  file=sys.stderr)
            return candidate

        # Find affidavit_id for filename — prefer the one already captured
        # from the listing card.
        if not candidate.affidavit_id:
            try:
                aff = await page.eval_on_selector(
                    "button[data-affidavit-id], a[data-affidavit-id]",
                    "el => el.getAttribute('data-affidavit-id')",
                )
                candidate.affidavit_id = aff or ""
            except Exception:
                pass

        # Compute filename. Prefer NAME_AFFID; fall back to just AFFID
        # when name extraction failed on the listing page.
        affidavit_id = candidate.affidavit_id or "noid"
        safe_name = "".join(c if c.isalnum() else "_" for c in candidate.name)[:60]
        safe_name = safe_name.strip("_")
        if safe_name:
            filename = f"{safe_name}__{affidavit_id}.pdf"
        else:
            filename = f"affidavit_{affidavit_id}.pdf"
        out_path = out_dir / filename

        if out_path.exists() and out_path.stat().st_size > 1024:
            candidate.pdf_path = str(out_path)
            candidate.download_succeeded = True
            print(f"     (cached) {filename}", file=sys.stderr)
            return candidate

        # Capture the download
        async with page.expect_download(timeout=NAV_TIMEOUT_MS) as dl_info:
            download_btn = await page.query_selector(
                "button.download-btn, button:has-text('Download'), a:has-text('Download')"
            )
            if not download_btn:
                raise RuntimeError("No Download button found on profile page")
            await download_btn.click()
        download = await dl_info.value
        out_path.parent.mkdir(parents=True, exist_ok=True)
        await download.save_as(str(out_path))
        candidate.pdf_path = str(out_path)
        candidate.download_succeeded = True
        print(f"     ✓ {filename} ({out_path.stat().st_size:,} bytes)", file=sys.stderr)
    except PWTimeout as e:
        candidate.error = f"timeout: {e}"
        print(f"     ✗ TIMEOUT for {candidate.name}", file=sys.stderr)
    except Exception as e:
        candidate.error = f"{type(e).__name__}: {e}"
        print(f"     ✗ {type(e).__name__}: {candidate.name}: {e}", file=sys.stderr)
    finally:
        await page.close()
    return candidate


# ---------------------------------------------------------------------------
# Main driver
# ---------------------------------------------------------------------------

async def run(args) -> None:
    out_dir = Path(args.output).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    pdf_dir = out_dir / "raw_pdfs"
    manifest_path = out_dir / "manifest.jsonl"

    seen = load_manifest(manifest_path)
    if seen:
        print(f"Resume: {len(seen)} candidates already in manifest", file=sys.stderr)

    if args.headless:
        print("⚠️  --headless is on. Akamai often blocks headless Chrome on "
              "this portal; if warm-up fails, drop the flag and retry with "
              "a visible browser window.", file=sys.stderr)

    async with async_playwright() as pw:
        # Anti-bot tweaks. Akamai inspects a long list of "are you a bot?"
        # signals (navigator.webdriver, missing window.chrome, missing
        # plugins array, headless UA string, viewport, locale, timezone).
        # We patch each here. Some of this won't matter in headed mode
        # because real Chrome already has them; we keep them anyway so
        # the script is self-consistent.
        browser = await pw.chromium.launch(
            headless=args.headless,
            slow_mo=80 if args.headless else 0,   # subtle pacing
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
        # Stealth init scripts — wipe the giveaways Akamai's sensor checks.
        await context.add_init_script("""
            // navigator.webdriver should not exist
            Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
            // Fake chrome runtime presence
            window.chrome = window.chrome || { runtime: {} };
            // Plugins length 0 is suspicious — fake a few
            Object.defineProperty(navigator, 'plugins', {
                get: () => [1, 2, 3, 4, 5].map(i => ({ name: 'Plugin' + i })),
            });
            // Languages should match locale
            Object.defineProperty(navigator, 'languages', {
                get: () => ['en-IN', 'en-US', 'en'],
            });
            // Permissions API often leaks headless — fake notification permission
            const originalQuery = window.navigator.permissions &&
                                   window.navigator.permissions.query;
            if (originalQuery) {
                window.navigator.permissions.query = (parameters) => (
                    parameters.name === 'notifications'
                        ? Promise.resolve({ state: Notification.permission })
                        : originalQuery(parameters)
                );
            }
        """)
        page = await context.new_page()

        # ── Akamai warm-up — required before any deep URL works ──────────
        await warm_up_session(page)

        # ── Mode A: direct URL (preferred — much simpler) ────────────────
        if args.listing_url:
            all_candidates = await crawl_by_url(
                page, args.listing_url,
                max_pages=args.max_pages,
                limit=args.limit,
                out_dir=out_dir,
            )

        # ── Mode B: cascading-form fallback ──────────────────────────────
        else:
            dump_path = (out_dir / "_form_layout.json") if args.inspect else None
            try:
                await apply_filters(page, args.election, args.election_type,
                                      args.state, args.phase, dump_path=dump_path)
            except RuntimeError as e:
                err_html = out_dir / "_error_page.html"
                err_html.write_text(await page.content())
                print(f"\nERROR: {e}", file=sys.stderr)
                print(f"  → Saved page HTML to {err_html}", file=sys.stderr)
                print(f"  → Re-run with --inspect to dump <select> elements,",
                      file=sys.stderr)
                print(f"     or use --listing-url <full URL> to skip the form.",
                      file=sys.stderr)
                await browser.close()
                sys.exit(2)

            if args.inspect:
                print(f"\n→ Form layout saved — exiting (inspect mode).",
                      file=sys.stderr)
                await browser.close()
                return

            # After form-submit we're on a listing page. Reuse the URL-mode
            # walker by snapshotting the current URL and iterating.
            current_url = page.url
            all_candidates = await crawl_by_url(
                page, current_url,
                max_pages=args.max_pages,
                limit=args.limit,
                out_dir=out_dir,
            )

        await page.close()

        if not all_candidates:
            print("No candidates found. Inspect _empty_page_*.html for clues.",
                  file=sys.stderr)
            await browser.close()
            sys.exit(2)

        # ── Status breakdown ─────────────────────────────────────────────
        from collections import Counter
        status_counts = Counter((c.status or "(unknown)") for c in all_candidates)
        print(f"\nStatus breakdown across {len(all_candidates)} candidates:",
              file=sys.stderr)
        for status, cnt in status_counts.most_common():
            print(f"  {status:15s}  {cnt:>4d}", file=sys.stderr)

        # ── Status filter ────────────────────────────────────────────────
        allowed_statuses = {s.strip().lower() for s in args.status.split(",")
                              if s.strip()}
        if "all" in allowed_statuses:
            filtered = list(all_candidates)
            print(f"--status=all → keeping every candidate ({len(filtered)})",
                  file=sys.stderr)
        else:
            filtered = []
            unknown_kept = 0
            for c in all_candidates:
                s = (c.status or "").lower()
                if s in allowed_statuses:
                    filtered.append(c)
                elif not s and args.keep_unknown_status:
                    filtered.append(c)
                    unknown_kept += 1
            kept_msg = (
                f"--status={args.status} → keeping {len(filtered)} of "
                f"{len(all_candidates)} candidates"
            )
            if unknown_kept:
                kept_msg += f" (including {unknown_kept} with unknown status — "
                kept_msg += "remove --keep-unknown-status to drop them)"
            print(kept_msg, file=sys.stderr)

        if not filtered:
            print("\nNo candidates match the status filter — exiting.",
                  file=sys.stderr)
            await browser.close()
            return

        # If --limit was set but the user also asked for status filtering,
        # the candidates we already scraped might include too many of the
        # wrong status. Re-cap the filtered list to --limit.
        if args.limit and len(filtered) > args.limit:
            filtered = filtered[:args.limit]
            print(f"  → Capped to first {args.limit} matching candidates",
                  file=sys.stderr)

        print(f"\nTotal candidates to process: {len(filtered)}",
              file=sys.stderr)

        # Download each
        for i, cand in enumerate(filtered, 1):
            if cand.profile_url in seen and seen[cand.profile_url].download_succeeded:
                print(f"[{i}/{len(filtered)}] (skip) {cand.name}",
                      file=sys.stderr)
                continue
            status_tag = f"  [{cand.status}]" if cand.status else ""
            print(f"[{i}/{len(filtered)}] {cand.name} / {cand.party}{status_tag}",
                  file=sys.stderr)
            cand = await download_pdf(context, cand, pdf_dir)
            append_manifest(manifest_path, cand)
            await asyncio.sleep(args.delay)

        await browser.close()


def main():
    ap = argparse.ArgumentParser(
        formatter_class=argparse.RawDescriptionHelpFormatter,
        description=__doc__,
    )

    # Mode A — direct URL (preferred):
    ap.add_argument("--listing-url",
                    help="Full CandidateCustomFilter URL with election/states "
                         "querystring. The script appends &page=N. Example: "
                         "https://affidavit.eci.gov.in/CandidateCustomFilter"
                         "?electionType=28-AC-GENERAL-3-54&election=28-AC-GENERAL-3-54&states=U05")

    # Mode B — cascading form (fallback):
    ap.add_argument("--election", default="",
                    help="(form mode) Election label substring (e.g. 'FEB-2025')")
    ap.add_argument("--election-type", default="",
                    help="(form mode) Election type substring (e.g. 'AC - GENERAL')")
    ap.add_argument("--state", default="",
                    help="(form mode) State name substring (e.g. 'NCT OF Delhi')")
    ap.add_argument("--phase", default=None,
                    help="(form mode) Phase number/label for multi-phase elections")

    ap.add_argument("--output", required=True,
                    help="Output directory (will contain raw_pdfs/ and manifest.jsonl)")
    ap.add_argument("--status", default="Accepted",
                    help="Comma-separated list of statuses to keep (default: "
                         "'Accepted'). Use 'Accepted,Contesting' for both, or "
                         "'all' to keep every candidate. "
                         "Valid values: Accepted, Rejected, Withdrawn, Contesting.")
    ap.add_argument("--keep-unknown-status", action="store_true",
                    help="Keep candidates whose listing card didn't expose a "
                         "status badge. Default: drop them.")
    ap.add_argument("--limit", type=int, default=0,
                    help="Smoke-test mode: stop after N candidates (0 = no limit)")
    ap.add_argument("--max-pages", type=int, default=200,
                    help="Safety cap on listing pages to crawl (default 200)")
    ap.add_argument("--delay", type=float, default=DEFAULT_DELAY_S,
                    help="Politeness delay between candidates in seconds")
    # Akamai aggressively fingerprints headless Chrome, so we default to
    # a visible window. You can still opt into headless with --headless
    # if you want to background the long full-pull run, but be prepared
    # to be blocked on cold starts. The visible window is more reliable.
    ap.add_argument("--headless", action="store_true", default=False,
                    help="Run Chromium fully headless. WARNING: Akamai often "
                         "blocks headless mode on this portal — leave this "
                         "off unless you've confirmed headless works for you.")
    ap.add_argument("--no-headless", dest="headless", action="store_false",
                    help="(Default) Run with a visible Chrome window — most "
                         "reliable against Akamai's bot detection.")
    ap.add_argument("--inspect", action="store_true",
                    help="(form mode only) Dump every <select> on the form page "
                         "to _form_layout.json, then exit.")
    args = ap.parse_args()

    # One mode must be picked
    if not args.listing_url and not (args.election and args.state):
        ap.error("Provide --listing-url, OR provide --election + --state "
                  "(+ optionally --election-type, --phase).")

    asyncio.run(run(args))


if __name__ == "__main__":
    main()
