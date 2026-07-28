"""Download CAG audit-report PDFs based on filters over the catalog manifest.

Reads  data/cag/catalog/reports_manifest.csv  (built by gpi_scrape_cag_catalog.py),
applies user-specified filters, and downloads the matching PDFs to
    data/cag/pdfs/{state_slug}/{original_basename}.pdf

Then writes  data/cag/pdfs/downloaded.csv  — a local manifest of what actually
landed on disk (with local_path + downloaded_at) so downstream extraction
tooling knows exactly what to work on.

Usage:
    # All Punjab reports, all years, all types
    python scripts/gpi_download_cag.py --state Punjab

    # Punjab State Finances Audit Reports post-2017 only
    python scripts/gpi_download_cag.py --state Punjab \\
        --report-type Financial --sector Finance --min-year 2017

    # All states' SFARs for FY22-24 (Congress-vs-AAP era comparison)
    python scripts/gpi_download_cag.py --report-type Financial \\
        --sector Finance --min-year 2021

    # Preview without downloading
    python scripts/gpi_download_cag.py --state Punjab --dry-run

    # Multiple states
    python scripts/gpi_download_cag.py --states Punjab,Delhi,Maharashtra \\
        --report-type Financial --min-year 2020

    # Match by title substring (useful for finding specific reports)
    python scripts/gpi_download_cag.py --state Punjab \\
        --title-contains "State Finances"
"""
from __future__ import annotations
import argparse
import csv
import re
import sys
import time
from datetime import datetime
from pathlib import Path
from urllib.parse import unquote, urlparse

ROOT = Path(__file__).resolve().parent.parent
CATALOG_CSV = ROOT / "data" / "cag" / "catalog" / "reports_manifest.csv"
PDFS_DIR = ROOT / "data" / "cag" / "pdfs"
LOCAL_MANIFEST = PDFS_DIR / "downloaded.csv"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/126.0.0.0 Safari/537.36"
    ),
    "Accept": "application/pdf,application/octet-stream,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://cag.gov.in/en/audit-report",
}


# ─── State-name → filesystem slug ────────────────────────────────────────────
def state_slug(state: str) -> str:
    """'Punjab' → 'punjab', 'Uttar Pradesh' → 'uttar-pradesh', 'Andaman & Nicobar Islands' → 'andaman-nicobar-islands'."""
    return re.sub(r"[^a-z0-9]+", "-", (state or "").lower()).strip("-")


def url_basename(url: str) -> str:
    """Return the filename portion of a URL, URL-decoded, sanitized for FS."""
    path = urlparse(url).path
    basename = unquote(path.rsplit("/", 1)[-1])
    # Strip weird chars that can cause FS issues on some systems
    basename = re.sub(r"[^A-Za-z0-9._\-()+]+", "-", basename)
    # Guarantee a .pdf extension
    if not basename.lower().endswith(".pdf"):
        basename = basename + ".pdf"
    return basename


# ─── Filter parsing ──────────────────────────────────────────────────────────
def audit_year_start(year_str: str) -> int | None:
    """Parse the starting year from an audit-year label ('2017-18' → 2017, '2017-22' → 2017)."""
    if not year_str:
        return None
    m = re.match(r"^(\d{4})", year_str)
    return int(m.group(1)) if m else None


def apply_filters(rows: list[dict], args) -> list[dict]:
    out = []

    # Multi-state via --states OR single --state
    states_filter = None
    if args.states:
        states_filter = {s.strip() for s in args.states.split(",") if s.strip()}
    elif args.state:
        states_filter = {args.state}

    types_filter = None
    if args.report_type:
        types_filter = {t.strip() for t in args.report_type.split(",") if t.strip()}

    sector_frag = args.sector.lower() if args.sector else None
    title_frag  = args.title_contains.lower() if args.title_contains else None

    for r in rows:
        if states_filter and r.get("state") not in states_filter:
            continue

        if types_filter:
            row_type = r.get("report_type", "")
            # Match if any requested type appears in the row's type string
            # (rows can have combos like "Compliance Performance")
            if not any(t.lower() in row_type.lower() for t in types_filter):
                continue

        if sector_frag and sector_frag not in (r.get("sector", "") or "").lower():
            continue

        if title_frag and title_frag not in (r.get("title", "") or "").lower():
            continue

        yr = audit_year_start(r.get("audit_year", ""))
        if args.min_year is not None and (yr is None or yr < args.min_year):
            continue
        if args.max_year is not None and (yr is None or yr > args.max_year):
            continue

        if args.no_pdf_size_filter is False and args.max_size_mb:
            try:
                s = float(r.get("pdf_size_mb") or 0)
                if s > args.max_size_mb:
                    continue
            except (ValueError, TypeError):
                pass

        out.append(r)
    return out


# ─── PDF validity + download ─────────────────────────────────────────────────
def is_valid_pdf(path: Path) -> bool:
    """Cheap validation: %PDF header + size sanity + EOF marker.

    We don't invoke pypdf here (too slow to run on every existing file during
    the skip-if-have check). But a truncated download typically:
      - misses the '%%EOF' trailer near end of file
      - is suspiciously small (<10KB for a CAG report)
    Catches most botched downloads without full parse cost.
    """
    try:
        size = path.stat().st_size
        if size < 10 * 1024:              # under 10KB — never a real CAG PDF
            return False
        with path.open("rb") as f:
            head = f.read(4)
            if head != b"%PDF":
                return False
            # Check for %%EOF marker in the last 1KB (allowing trailing whitespace)
            f.seek(max(0, size - 1024))
            tail = f.read()
            return b"%%EOF" in tail
    except Exception:
        return False


def download_one(row: dict, session, timeout: int = 180) -> tuple[Path | None, str]:
    """Download one row. Returns (local_path or None, status_msg)."""
    state = row["state"]
    url = row["pdf_url"]
    if not url:
        return None, "no_url"

    # Some manifest rows have relative hrefs (e.g. '/webroot/uploads/...')
    # because the scraper didn't prepend the host. Normalize here so the
    # requests library gets an absolute URL.
    if url.startswith("/"):
        url = "https://cag.gov.in" + url
    elif not url.startswith("http"):
        url = "https://cag.gov.in/" + url.lstrip("/")

    slug = state_slug(state) or "unknown"
    state_dir = PDFS_DIR / slug
    state_dir.mkdir(parents=True, exist_ok=True)

    out_path = state_dir / url_basename(url)

    # Already have a valid PDF?
    if out_path.exists() and is_valid_pdf(out_path):
        size_mb = out_path.stat().st_size / 1_048_576
        return out_path, f"skip_have  ({size_mb:.1f}MB)"

    # Purge any half-downloaded / bad file
    if out_path.exists():
        out_path.unlink()

    tmp = out_path.with_suffix(out_path.suffix + ".tmp")
    try:
        resp = session.get(url, timeout=timeout, stream=True)
        resp.raise_for_status()
        # Reject obviously non-PDF responses (HTML error pages, JSON auth
        # failures, etc.) BEFORE we start writing bytes to disk.
        ctype = resp.headers.get("content-type", "").lower()
        if ctype and "html" in ctype:
            return None, f"non_pdf_response: content-type={ctype}"

        bytes_written = 0
        with tmp.open("wb") as f:
            for chunk in resp.iter_content(chunk_size=64 * 1024):
                if chunk:
                    f.write(chunk)
                    bytes_written += len(chunk)

        # A stream that yielded 0 bytes can occasionally leave no tmp on
        # some filesystems (or antivirus can quarantine 0-byte files).
        # Guard rename explicitly with a friendlier error.
        if bytes_written == 0:
            if tmp.exists():
                tmp.unlink()
            return None, "empty_response (0 bytes)"
        if not tmp.exists():
            return None, "tmp_missing (write completed but file gone — AV/quarantine?)"

        tmp.rename(out_path)
    except Exception as e:
        if tmp.exists():
            tmp.unlink()
        return None, f"error: {type(e).__name__}: {e}"

    if not is_valid_pdf(out_path):
        out_path.unlink()
        return None, "not_a_pdf"

    size_mb = bytes_written / 1_048_576
    return out_path, f"downloaded ({size_mb:.1f}MB)"


# ─── Local manifest (of what got downloaded) ─────────────────────────────────
LOCAL_MANIFEST_COLS = [
    "state", "state_slug", "audit_year", "report_type", "sector",
    "publication_date", "title", "pdf_url", "local_path",
    "pdf_size_mb", "downloaded_at",
]


def load_local_manifest() -> dict[str, dict]:
    """Keyed by pdf_url for de-dup across runs."""
    if not LOCAL_MANIFEST.exists():
        return {}
    out = {}
    with LOCAL_MANIFEST.open("r", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            out[row["pdf_url"]] = row
    return out


def write_local_manifest(rows: dict[str, dict]):
    PDFS_DIR.mkdir(parents=True, exist_ok=True)
    tmp = LOCAL_MANIFEST.with_suffix(".csv.tmp")
    with tmp.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=LOCAL_MANIFEST_COLS)
        w.writeheader()
        # Sort by state then audit_year for browsability
        sorted_rows = sorted(
            rows.values(),
            key=lambda r: (r.get("state", ""), r.get("audit_year", ""), r.get("title", "")),
        )
        for row in sorted_rows:
            w.writerow({k: row.get(k, "") for k in LOCAL_MANIFEST_COLS})
    tmp.replace(LOCAL_MANIFEST)


# ─── Main ────────────────────────────────────────────────────────────────────
def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                  formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--state", help="Single state name (e.g. 'Punjab')")
    ap.add_argument("--states", help="Comma-separated states (overrides --state)")
    ap.add_argument("--report-type",
                    help="Filter by report type: Financial | Compliance | Performance")
    ap.add_argument("--sector", help="Substring match on sector column")
    ap.add_argument("--min-year", type=int,
                    help="Minimum audit year start (e.g. 2017 to include 2017-18 onward)")
    ap.add_argument("--max-year", type=int, help="Maximum audit year start")
    ap.add_argument("--title-contains",
                    help="Only rows whose title contains this substring")
    ap.add_argument("--max-size-mb", type=float,
                    help="Skip PDFs larger than this (Mb). Useful for quick tests.")
    ap.add_argument("--no-pdf-size-filter", action="store_true",
                    help="Ignore --max-size-mb (kept for symmetry)")
    ap.add_argument("--dry-run", action="store_true",
                    help="List matching reports, don't download")
    ap.add_argument("--delay", type=float, default=1.0,
                    help="Seconds between downloads (default 1.0)")
    ap.add_argument("--limit", type=int, default=0,
                    help="Cap downloads at this many rows (0 = no limit)")
    args = ap.parse_args()

    if not CATALOG_CSV.exists():
        raise SystemExit(
            f"Catalog manifest not found: {CATALOG_CSV.relative_to(ROOT)}\n"
            "Run scripts/gpi_scrape_cag_catalog.py first."
        )

    # Load full catalog
    with CATALOG_CSV.open("r", encoding="utf-8") as f:
        catalog = list(csv.DictReader(f))
    print(f"Catalog loaded: {len(catalog)} reports")

    # Filter
    matched = apply_filters(catalog, args)
    print(f"Matched:        {len(matched)} reports")

    if args.limit and len(matched) > args.limit:
        matched = matched[:args.limit]
        print(f"Limited to:     {len(matched)} (--limit={args.limit})")

    # Group by state for a preview breakdown
    from collections import Counter
    by_state = Counter(r["state"] for r in matched)
    total_mb = sum(float(r["pdf_size_mb"] or 0) for r in matched)
    print(f"Total est. size: {total_mb:.0f}MB")
    print(f"States in match: {len(by_state)}")
    if len(by_state) <= 15:
        for s, n in by_state.most_common():
            print(f"  {s:<30s}  {n}")
    print()

    if args.dry_run:
        print("── Dry-run · sample rows ─────────────────────────────────────")
        for r in matched[:15]:
            print(f"  [{r['state'][:14]:<14s}] {r['audit_year']:<10s} "
                  f"{r['report_type'][:12]:<12s} "
                  f"{(r['pdf_size_mb'] or '?')+'MB':<8s}  {r['title'][:80]}")
        if len(matched) > 15:
            print(f"  ... and {len(matched) - 15} more")
        return

    if not matched:
        print("Nothing to download.")
        return

    # Actual download
    try:
        import requests
    except ImportError:
        raise SystemExit("pip install requests")

    session = requests.Session()
    session.headers.update(HEADERS)

    local_manifest = load_local_manifest()
    counts = {"downloaded": 0, "skipped": 0, "failed": 0}
    t0 = time.time()

    for i, row in enumerate(matched, 1):
        print(f"[{i:>4d}/{len(matched)}] {row['state']:<18s} {row['audit_year']:<10s} "
              f"{row['title'][:50]}")
        local_path, status = download_one(row, session)
        print(f"           → {status}")

        if local_path:
            # Record in local manifest
            local_manifest[row["pdf_url"]] = {
                "state":            row["state"],
                "state_slug":       state_slug(row["state"]),
                "audit_year":       row.get("audit_year", ""),
                "report_type":      row.get("report_type", ""),
                "sector":           row.get("sector", ""),
                "publication_date": row.get("publication_date", ""),
                "title":            row.get("title", ""),
                "pdf_url":          row["pdf_url"],
                "local_path":       str(local_path.relative_to(ROOT)),
                "pdf_size_mb":      f"{local_path.stat().st_size / 1_048_576:.2f}",
                "downloaded_at":    datetime.utcnow().isoformat(timespec="seconds") + "Z",
            }
            if "skip_have" in status:
                counts["skipped"] += 1
            else:
                counts["downloaded"] += 1
        else:
            counts["failed"] += 1

        # Save local manifest every 20 downloads
        if i % 20 == 0:
            write_local_manifest(local_manifest)

        if i < len(matched):
            time.sleep(args.delay)

    write_local_manifest(local_manifest)
    dt = time.time() - t0

    print()
    print("═══════════════════════ Summary ═══════════════════════")
    print(f"  Downloaded: {counts['downloaded']}")
    print(f"  Skipped (already have): {counts['skipped']}")
    print(f"  Failed:     {counts['failed']}")
    print(f"  Time:       {dt/60:.1f} minutes ({dt:.0f}s)")
    print()
    print(f"Local manifest: {LOCAL_MANIFEST.relative_to(ROOT)}")
    print(f"PDFs directory: {PDFS_DIR.relative_to(ROOT)}/")


if __name__ == "__main__":
    main()
