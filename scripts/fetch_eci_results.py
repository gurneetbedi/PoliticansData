"""
Fetch official ECI election results for a state assembly election.

Replaces the Wikipedia loader (top-2 candidates) with the ECI results
portal's per-constituency data (every candidate with vote counts).

Uses Playwright attached to a CDP-controlled Chrome (same infrastructure
as fetch_eci_affidavits.py) because ECI's Akamai bot check TLS-fingerprints
requests and blocks python-requests even with browser User-Agent headers.

Prerequisite: Chrome running with --remote-debugging-port=9222
    /Applications/Google\\ Chrome.app/Contents/MacOS/Google\\ Chrome \\
        --remote-debugging-port=9222 &

Usage:
    python scripts/fetch_eci_results.py \\
        --state "West Bengal" \\
        --year 2026 \\
        --state-code S25 \\
        --results-base https://results.eci.gov.in/ResultAcGenMay2026/ \\
        --out data/eci/results/westbengal_2026_eci_results.json

Output JSON schema:
    {
      "state": "West Bengal",
      "year": 2026,
      "state_code": "S25",
      "source": "https://results.eci.gov.in/ResultAcGenMay2026/",
      "assembly_size": 294,
      "constituencies": [
        {
          "number": 144,
          "name": "FALTA",
          "candidates": [
            {"rank":1, "name":"...", "party":"...", "evm_votes":..., "postal_votes":..., "total_votes":..., "vote_pct":..., "won":true},
            ...
          ]
        }
      ]
    }
"""
from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
import time
from pathlib import Path
from urllib.parse import urljoin


CDP_PORT = 9222


def _get_bs4():
    try:
        from bs4 import BeautifulSoup
        return BeautifulSoup
    except ImportError:
        sys.exit("pip install beautifulsoup4")


async def _connect_playwright():
    """Attach Playwright to the user's already-running Chrome via CDP."""
    try:
        from playwright.async_api import async_playwright
    except ImportError:
        sys.exit("pip install playwright && playwright install chromium")

    pw = await async_playwright().start()
    try:
        browser = await pw.chromium.connect_over_cdp(
            f"http://localhost:{CDP_PORT}", timeout=10000)
    except Exception as e:
        await pw.stop()
        sys.exit(
            f"Could not attach to Chrome on port {CDP_PORT}: {e}\n"
            "Start Chrome first with:\n"
            "  /Applications/Google\\ Chrome.app/Contents/MacOS/Google\\ Chrome "
            f"--remote-debugging-port={CDP_PORT} &"
        )
    context = browser.contexts[0] if browser.contexts else await browser.new_context()
    return pw, browser, context


async def _fetch_html(page, url: str, retries: int = 3) -> str:
    """Navigate to url and return page HTML. Retries on transient failures."""
    last_err = None
    for i in range(retries):
        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=30000)
            await page.wait_for_timeout(500)  # let JS settle
            return await page.content()
        except Exception as e:
            last_err = e
            await asyncio.sleep(1 + i)
    raise RuntimeError(f"Failed to fetch {url}: {last_err}")


def list_constituencies_from_html(html: str) -> list[dict]:
    """Extract (name, number) pairs from the partywiseresult page HTML."""
    BeautifulSoup = _get_bs4()
    soup = BeautifulSoup(html, "html.parser")
    text = soup.get_text("\n", strip=True)

    constituencies = []
    seen = set()
    for line in text.splitlines():
        m = re.match(r"^(.+?)\s+-\s+(\d+)$", line.strip())
        if m:
            name, num = m.group(1).strip(), int(m.group(2))
            if num in seen:
                continue
            # Constituency lines are ALL-CAPS names + a number 1-500
            if 1 <= num <= 500 and len(name) >= 2 and name.upper() == name:
                constituencies.append({"number": num, "name": name})
                seen.add(num)
    constituencies.sort(key=lambda c: c["number"])
    return constituencies


def _cell_int(text: str) -> int | None:
    if not text:
        return None
    m = re.search(r"[-+]?\d[\d,]*", text)
    if not m:
        return None
    try:
        return int(m.group(0).replace(",", ""))
    except ValueError:
        return None


def _cell_float(text: str) -> float | None:
    if not text:
        return None
    m = re.search(r"[-+]?\d+(?:\.\d+)?", text)
    if not m:
        return None
    try:
        return float(m.group(0))
    except ValueError:
        return None


def parse_constituency_page(html: str) -> list[dict]:
    """Parse the ConstituencywiseSXXN.htm table into ranked candidates."""
    BeautifulSoup = _get_bs4()
    soup = BeautifulSoup(html, "html.parser")

    target = None
    for table in soup.find_all("table"):
        headers = [th.get_text(" ", strip=True).lower()
                    for th in table.find_all(["th", "td"])[:8]]
        header_str = " ".join(headers)
        if "candidate" in header_str and "party" in header_str and \
                ("total votes" in header_str or "votes" in header_str):
            target = table
            break
    if not target:
        return []

    rows = target.find_all("tr")
    candidates = []
    for tr in rows[1:]:
        cells = [td.get_text(" ", strip=True) for td in tr.find_all(["td", "th"])]
        if len(cells) < 6:
            continue
        try:
            name = cells[1].strip()
            party = cells[2].strip()
            evm = _cell_int(cells[3])
            postal = _cell_int(cells[4])
            total = _cell_int(cells[5])
            pct = _cell_float(cells[6]) if len(cells) > 6 else None
        except Exception:
            continue
        if not name or name.lower() == "total":
            continue
        candidates.append({
            "name": name,
            "party": party,
            "evm_votes": evm,
            "postal_votes": postal,
            "total_votes": total,
            "vote_pct": pct,
        })

    candidates.sort(key=lambda c: -(c["total_votes"] or 0))
    for i, c in enumerate(candidates, 1):
        c["rank"] = i
        c["won"] = (i == 1)
    return candidates


async def scrape_state_async(base_url: str, state_code: str, state_name: str,
                              year: int, delay: float = 0.5,
                              assembly_size: int = 0) -> dict:
    pw, browser, context = await _connect_playwright()
    page = await context.new_page()

    # Warm up: hit the homepage first so Akamai issues session cookies
    print(f"→ Warming up ECI session ...", file=sys.stderr)
    try:
        await _fetch_html(page, "https://results.eci.gov.in/")
        await asyncio.sleep(1.0)
        await _fetch_html(page, urljoin(base_url, "index.htm"))
        await asyncio.sleep(1.0)
    except Exception as e:
        print(f"  warm-up warning: {e}", file=sys.stderr)

    # Enumerate constituencies. Two strategies:
    #   1. Parse the "All Constituencies" list off partywiseresult-<code>.htm
    #      (works for 2026 URLs).
    #   2. If that yields nothing (2024 pages omit that list), iterate
    #      1..assembly_size directly. The caller supplies --assembly-size.
    list_url = urljoin(base_url, f"partywiseresult-{state_code}.htm")
    print(f"→ Listing constituencies from {list_url}", file=sys.stderr)
    html = await _fetch_html(page, list_url)
    constituencies = list_constituencies_from_html(html)
    print(f"  found {len(constituencies)} in partywise list", file=sys.stderr)
    if not constituencies:
        if assembly_size <= 0:
            await page.close()
            await browser.close()
            await pw.stop()
            sys.exit(
                "No constituencies found on the partywise page and "
                "--assembly-size not set. Pass --assembly-size <N> where N "
                "is the number of seats (e.g. 147 for Odisha, 175 for AP)."
            )
        print(f"  falling back to iteration 1..{assembly_size} — "
              f"names will be scraped from each constituency page",
              file=sys.stderr)
        constituencies = [{"number": n, "name": ""}
                            for n in range(1, assembly_size + 1)]

    out = {
        "state": state_name,
        "year": year,
        "state_code": state_code,
        "source": base_url,
        "assembly_size": len(constituencies),
        "constituencies": [],
    }

    # Fetch per-constituency results
    for i, c in enumerate(constituencies, 1):
        num = c["number"]
        url = urljoin(base_url, f"Constituencywise{state_code}{num}.htm")
        try:
            html = await _fetch_html(page, url)
            # Dump the very first fetched page for debugging when caller
            # asks for it — helpful when a new URL pattern uses a
            # different table layout and parse_constituency_page returns 0.
            if i == 1:
                dump_path = Path("_eci_first_page_debug.html")
                dump_path.write_text(html)
                print(f"  (saved first page to {dump_path} for debugging)",
                      file=sys.stderr)
            cands = parse_constituency_page(html)
            # If the caller didn't pre-populate a name (iteration fallback),
            # extract "Assembly Constituency NNN - NAME (State)" from the
            # page's H2 heading.
            if not c["name"]:
                nm = re.search(
                    r"Assembly\s+Constituency\s+\d+\s*-\s*(.+?)\s*\(",
                    html)
                if nm:
                    c["name"] = nm.group(1).strip().upper()
                else:
                    c["name"] = f"AC-{num}"
        except Exception as e:
            print(f"  ! {c.get('name') or num}: {e}", file=sys.stderr)
            cands = []

        out["constituencies"].append({
            "number": num,
            "name": c["name"],
            "candidates": cands,
        })

        if i % 25 == 0:
            print(f"  progress: {i}/{len(constituencies)} constituencies parsed",
                  file=sys.stderr)
        await asyncio.sleep(delay)

    await page.close()
    await browser.close()
    await pw.stop()
    return out


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                  formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--state", required=True,
                    help='State name in TitleCase, e.g. "West Bengal"')
    ap.add_argument("--year", type=int, required=True)
    ap.add_argument("--state-code", required=True,
                    help="ECI state code, e.g. S25 (West Bengal)")
    ap.add_argument("--results-base", required=True,
                    help="Base URL of the ECI results portal for this election, "
                         "e.g. https://results.eci.gov.in/ResultAcGenMay2026/")
    ap.add_argument("--out", required=True,
                    help="Output JSON path")
    ap.add_argument("--delay", type=float, default=0.5,
                    help="Delay between per-constituency fetches (seconds)")
    ap.add_argument("--assembly-size", type=int, default=0,
                    help="State assembly size (e.g. 147 for Odisha). "
                         "Only needed for URLs where the partywise page "
                         "doesn't include the constituency dropdown "
                         "(most 2024 elections). 2026 URLs auto-discover.")
    args = ap.parse_args()

    base = args.results_base
    if not base.endswith("/"):
        base += "/"

    result = asyncio.run(scrape_state_async(
        base_url=base, state_code=args.state_code,
        state_name=args.state, year=args.year, delay=args.delay,
        assembly_size=args.assembly_size))

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(result, indent=2, ensure_ascii=False))
    total_cands = sum(len(c['candidates']) for c in result['constituencies'])
    print(f"→ Wrote {out_path} — {len(result['constituencies'])} constituencies "
          f"({total_cands} candidates)", file=sys.stderr)


if __name__ == "__main__":
    main()
