"""Scrape the full CAG audit-report catalog for all 28+ states.

CAG (Comptroller and Auditor General of India) publishes state-level audit
reports on cag.gov.in. Rather than per-state scraping (each state has a slug
we'd have to discover), this crawls the MASTER listing:

    https://cag.gov.in/en/audit-report?gt=49

which returns state audit reports across every state, paginated (272 pages,
~2,717 reports as of July 2026). Each entry carries the state name in its
own heading, so we parse it out at the same time.

Output: `data/cag/catalog/reports_manifest.csv` with columns:

    state, title, publication_date, report_type, sector, audit_year,
    detail_url, pdf_url, pdf_size_mb, scraped_at

Once this manifest exists, downstream tools (download-then-extract) can:
  - Filter by state / year / report_type / sector
  - Selectively download only what we want
  - Cross-reference against RBI Handbook + NCRB data for cross-source validation

Usage:
    # Full crawl (all 272 pages, ~10 minutes with polite delay)
    python scripts/gpi_scrape_cag_catalog.py

    # Test run — only first 5 pages
    python scripts/gpi_scrape_cag_catalog.py --pages 5

    # Resume interrupted crawl from a specific page
    python scripts/gpi_scrape_cag_catalog.py --start-page 100

    # Fresh crawl (overwrite existing manifest)
    python scripts/gpi_scrape_cag_catalog.py --fresh

Design notes:
    • Polite rate-limit: 1.5s between page requests (~5-8 minutes total).
    • Resumable: writes to manifest CSV incrementally, so a network hiccup
      doesn't cost you the whole scrape.
    • De-dupe by (state, title) key when merging into existing manifest.
    • Failure tolerant: HTTP errors log and continue, page-level failures
      just skip that page.
"""
from __future__ import annotations
import argparse
import csv
import re
import sys
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT_DIR = ROOT / "data" / "cag" / "catalog"
MANIFEST = OUT_DIR / "reports_manifest.csv"

BASE_URL = "https://cag.gov.in/en/audit-report"
DEFAULT_QUERY = {"gt": "49"}   # gt=49 = "State Reports" filter (excludes Union/Local)

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/126.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://cag.gov.in/en/",
}


# ═══════════════════════════════════════════════════════════════════════════
# Parsing — extract report entries from a listing page's HTML.
#
# Each report entry in the HTML looks like:
#
#     <div class="report-item">
#       <p>16 March 2026</p>
#       <p>Financial</p>
#       <h5>Punjab</h5>
#       <a href="/ag/punjab/en/audit-report/details/124572">Report No. 1 of 2025 ... </a>
#       <p>In accordance with Article 151...</p>
#       <p>Sector: Finance</p>
#       <a href="https://cag.gov.in/webroot/uploads/download_audit_report/2024/....pdf"
#          class="download-link">... (PDF 69.46MB)</a>
#     </div>
#
# We use BeautifulSoup to extract these fields robustly.
# ═══════════════════════════════════════════════════════════════════════════
DATE_RE = re.compile(r"^\d{1,2}\s+[A-Z][a-z]+\s+\d{4}$")


def parse_page_html(html: str) -> list[dict]:
    """Return a list of report dicts parsed from a listing page's HTML."""
    from bs4 import BeautifulSoup
    soup = BeautifulSoup(html, "html.parser")

    reports = []

    # CAG uses a repeating pattern. The most stable anchor is the download
    # link (contains 'webroot/uploads/download_audit_report/' in URL). Walk
    # each such anchor and reconstruct the surrounding context.
    pdf_re = re.compile(r"webroot/uploads/download_audit_report/", re.I)

    for pdf_link in soup.find_all("a", href=pdf_re):
        pdf_url = pdf_link["href"].strip()
        # Some anchors use relative paths — normalize to absolute so the
        # downloader can fetch without any additional plumbing.
        if pdf_url.startswith("/"):
            pdf_url = "https://cag.gov.in" + pdf_url
        elif not pdf_url.startswith("http"):
            pdf_url = "https://cag.gov.in/" + pdf_url.lstrip("/")

        # Extract file-size annotation ("(PDF 9.9 MB)" appears in surrounding text)
        pdf_context = pdf_link.get_text(" ", strip=True)
        size_match = re.search(r"(\d+(?:\.\d+)?)\s*MB", pdf_context, re.I)
        pdf_size_mb = float(size_match.group(1)) if size_match else None

        # Walk up to find the enclosing report block. The parent chain varies
        # by page template — try increasing ancestor levels until we find
        # a container that also has the report title link.
        container = pdf_link
        for _ in range(6):
            container = container.parent
            if not container:
                break
            title_links = container.find_all("a",
                                                 href=re.compile(r"/audit-report/details/"))
            if title_links:
                break

        if not container:
            continue

        # Report title + detail page URL
        title = None
        detail_url = None
        for a in container.find_all("a", href=re.compile(r"/audit-report/details/")):
            t = a.get_text(" ", strip=True)
            if t and len(t) > 10:  # skip empty / icon-only anchors
                title = t
                href = a["href"]
                detail_url = href if href.startswith("http") else "https://cag.gov.in" + href
                break

        if not title:
            continue

        # State name — usually in an h5 tag, but sometimes in a heading class.
        state_name = None
        for header_tag in ["h5", "h4", "h3"]:
            for h in container.find_all(header_tag):
                text = h.get_text(strip=True)
                # Skip generic headings — state names are 3-40 chars, no digits
                if 3 <= len(text) <= 40 and not re.search(r"\d", text):
                    state_name = text
                    break
            if state_name:
                break

        # Publication date + report type — first two non-empty <p> tags in the block
        pub_date = None
        report_type = None
        for p in container.find_all("p"):
            text = p.get_text(strip=True)
            if not text:
                continue
            if DATE_RE.match(text):
                pub_date = text
            elif text in ("Financial", "Compliance", "Performance",
                             "Compliance Performance", "Compliance Financial Performance",
                             "ADC reports"):
                report_type = text
            if pub_date and report_type:
                break

        # Sector — appears in the container text as "Sector: X | Y | Z"
        sector = None
        container_text = container.get_text(" ", strip=True)
        sector_match = re.search(r"Sector:\s*([^\n]+?)(?:\s*\[|$|\s{2,})",
                                  container_text)
        if sector_match:
            sector = sector_match.group(1).strip()
            # Trim off any trailing report-title fragment
            sector = re.sub(r"\s+Download.*$", "", sector).strip()
            sector = sector[:200]  # cap length

        # Audit year — extract from title where possible
        audit_year = None
        year_match = re.search(r"(\d{4})[-–](\d{2}|\d{4})", title)
        if year_match:
            audit_year = f"{year_match.group(1)}-{year_match.group(2)}"

        reports.append({
            "state":            state_name or "",
            "title":            title,
            "publication_date": pub_date or "",
            "report_type":      report_type or "",
            "sector":           sector or "",
            "audit_year":       audit_year or "",
            "detail_url":       detail_url or "",
            "pdf_url":          pdf_url,
            "pdf_size_mb":      pdf_size_mb or "",
            "scraped_at":       datetime.utcnow().isoformat(timespec="seconds") + "Z",
        })

    return reports


# ═══════════════════════════════════════════════════════════════════════════
# Manifest CSV helpers
# ═══════════════════════════════════════════════════════════════════════════
CSV_COLUMNS = [
    "state", "title", "publication_date", "report_type", "sector",
    "audit_year", "detail_url", "pdf_url", "pdf_size_mb", "scraped_at",
]


def load_manifest() -> dict[tuple[str, str], dict]:
    """Return existing manifest keyed by (state, title) for de-dup."""
    if not MANIFEST.exists():
        return {}
    out = {}
    with MANIFEST.open("r", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            key = (row["state"], row["title"])
            out[key] = row
    return out


def write_manifest(rows: dict[tuple[str, str], dict]):
    """Write manifest atomically (temp file + rename)."""
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    tmp = MANIFEST.with_suffix(".csv.tmp")
    with tmp.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=CSV_COLUMNS)
        w.writeheader()
        # Sort by state then publication date so the manifest is human-scannable
        sorted_rows = sorted(rows.values(),
                              key=lambda r: (r.get("state", ""),
                                             r.get("publication_date", "")))
        for row in sorted_rows:
            # Ensure only known columns land in the CSV
            w.writerow({k: row.get(k, "") for k in CSV_COLUMNS})
    tmp.replace(MANIFEST)


# ═══════════════════════════════════════════════════════════════════════════
# Scrape driver
# ═══════════════════════════════════════════════════════════════════════════
TOTAL_RE = re.compile(r"Page\s+(\d+)\s+of\s+(\d+)", re.I)


def detect_total_pages(html: str) -> int | None:
    m = TOTAL_RE.search(html)
    if m:
        return int(m.group(2))
    return None


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                  formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--pages", type=int, default=None,
                    help="Only scrape this many pages (default: all)")
    ap.add_argument("--start-page", type=int, default=1,
                    help="Resume from this page number (default 1)")
    ap.add_argument("--fresh", action="store_true",
                    help="Discard existing manifest before scraping")
    ap.add_argument("--delay", type=float, default=1.5,
                    help="Seconds between page requests (be polite; default 1.5)")
    args = ap.parse_args()

    try:
        import requests
        from bs4 import BeautifulSoup  # noqa: F401 — used in parse_page_html
    except ImportError:
        raise SystemExit("pip install requests beautifulsoup4")

    OUT_DIR.mkdir(parents=True, exist_ok=True)

    manifest = {} if args.fresh else load_manifest()
    print(f"Manifest starting with {len(manifest)} existing rows")
    print(f"Base URL:   {BASE_URL}?gt=49")
    print(f"Rate limit: {args.delay}s per page")
    print()

    session = requests.Session()
    session.headers.update(HEADERS)

    # ── Probe page 1 to discover total pages ────────────────────────────────
    if args.pages is None:
        print("Detecting total page count from page 1...")
        try:
            resp = session.get(BASE_URL, params={**DEFAULT_QUERY, "page": 1}, timeout=60)
            resp.raise_for_status()
            total = detect_total_pages(resp.text)
            if total:
                print(f"  → {total} pages total")
                max_page = total
            else:
                print(f"  → couldn't detect; defaulting to 300")
                max_page = 300
        except Exception as e:
            print(f"  → probe failed ({e}); defaulting to 300")
            max_page = 300
    else:
        max_page = args.start_page + args.pages - 1

    # ── Crawl ───────────────────────────────────────────────────────────────
    total_parsed = 0
    total_new = 0
    fail_count = 0
    t0 = time.time()

    for page in range(args.start_page, max_page + 1):
        try:
            resp = session.get(BASE_URL, params={**DEFAULT_QUERY, "page": page},
                                 timeout=60)
            resp.raise_for_status()
        except Exception as e:
            print(f"  ✗ page {page}: {type(e).__name__}: {e}")
            fail_count += 1
            time.sleep(args.delay * 2)
            continue

        rows = parse_page_html(resp.text)
        if not rows:
            print(f"  · page {page}: 0 reports parsed (possibly end of list)")
            # Two consecutive empty pages → we've walked past the end
            if page > 5:
                # Give a couple more pages then break
                fail_count += 1
                if fail_count >= 3:
                    print(f"  Stopping — {fail_count} consecutive empty/failed pages")
                    break
            continue

        new_this_page = 0
        for row in rows:
            key = (row["state"], row["title"])
            if key not in manifest:
                new_this_page += 1
                total_new += 1
            manifest[key] = row
            total_parsed += 1

        # Save every 10 pages so partial progress persists
        if page % 10 == 0:
            write_manifest(manifest)

        print(f"  ✓ page {page:>3d}/{max_page}: {len(rows)} rows "
              f"({new_this_page} new); total={total_parsed}, "
              f"manifest={len(manifest)}")
        fail_count = 0
        time.sleep(args.delay)

    write_manifest(manifest)
    dt = time.time() - t0

    # ── Summary ─────────────────────────────────────────────────────────────
    print()
    print("═══════════════════════ Summary ═══════════════════════")
    print(f"  Pages scraped: {min(page, max_page) - args.start_page + 1}")
    print(f"  Rows parsed:   {total_parsed}")
    print(f"  New rows:      {total_new}")
    print(f"  Manifest size: {len(manifest)}")
    print(f"  Time:          {dt/60:.1f} minutes ({dt:.0f}s)")
    print()
    print(f"Manifest: {MANIFEST.relative_to(ROOT)}")

    # Quick state breakdown
    from collections import Counter
    states = Counter(r.get("state", "?") for r in manifest.values())
    print()
    print("Reports per state (top 15):")
    for state, n in states.most_common(15):
        print(f"  {state:<30s}  {n:>4d}")


if __name__ == "__main__":
    main()
