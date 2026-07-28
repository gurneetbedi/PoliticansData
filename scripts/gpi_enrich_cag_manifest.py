"""Back-fill report_type + audit_year from titles in the CAG manifest.

The catalog scraper (gpi_scrape_cag_catalog.py) captured titles + PDF URLs +
sectors reliably, but its report_type / audit_year / publication_date extraction
missed most rows (the CAG HTML doesn't put those fields where the parser looked).

Since CAG report titles are highly structured and contain enough info to classify
type + extract the audit year, this script back-fills those fields in-place using
title-pattern matching. Runs in <1s, no network calls.

Usage:
    python scripts/gpi_enrich_cag_manifest.py            # writes in place
    python scripts/gpi_enrich_cag_manifest.py --dry-run  # preview only

After running, gpi_download_cag.py's filters (--report-type, --min-year) work.
"""
from __future__ import annotations
import argparse
import csv
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
MANIFEST = ROOT / "data" / "cag" / "catalog" / "reports_manifest.csv"


# ── Type classifier ──────────────────────────────────────────────────────────
# Order matters — first match wins. More specific patterns first.
TYPE_PATTERNS = [
    # Performance Audits explicitly labeled
    (re.compile(r"\bperformance\s+audit(s)?\b", re.I), "Performance"),
    (re.compile(r"\bpa\s+on\b", re.I),                  "Performance"),

    # Financial reports (state finances, state revenues, GPFR)
    (re.compile(r"\bstate\s+finance", re.I),            "Financial"),
    (re.compile(r"\bstate\s+revenues?\b", re.I),        "Financial"),
    (re.compile(r"\brevenue\s+sector\b", re.I),         "Financial"),
    (re.compile(r"\bgeneral\s+purpose\s+financial", re.I), "Financial"),
    (re.compile(r"\bfinancial\s+audit\b", re.I),        "Financial"),
    (re.compile(r"\bfinances?\s+audit\s+report\b", re.I), "Financial"),

    # Compliance / Composite / Local Bodies / Non-PSU / PSU
    (re.compile(r"\bcompliance\s+audit\b", re.I),       "Compliance"),
    (re.compile(r"\bcompliance\s+report\b", re.I),      "Compliance"),
    (re.compile(r"\bcomposite\s+audit\b", re.I),        "Compliance"),
    (re.compile(r"\blocal\s+bodies?\s+report\b", re.I), "Compliance"),
    (re.compile(r"\blocal\s+government", re.I),         "Compliance"),
    (re.compile(r"\bpanchayati?\s+raj", re.I),          "Compliance"),
    (re.compile(r"\bpsus?\s*[\(\-]", re.I),             "Compliance"),
    (re.compile(r"\bpublic\s+sector\s+enterprises?", re.I), "Compliance"),
    (re.compile(r"\bpublic\s+sector\s+undertakings?", re.I),"Compliance"),
    (re.compile(r"\bnon[- ]psu", re.I),                 "Compliance"),
    (re.compile(r"\baudit\s+report\s+on\b", re.I),      "Compliance"),

    # ADC (Accounts of Autonomous Development Councils)
    (re.compile(r"\badc\b", re.I),                      "ADC reports"),
    (re.compile(r"\bautonomous\s+development", re.I),   "ADC reports"),
]


def classify(title: str) -> str:
    if not title:
        return ""
    for regex, label in TYPE_PATTERNS:
        if regex.search(title):
            return label
    return ""


# ── Audit-year extractor ─────────────────────────────────────────────────────
# CAG titles reference the audit period several ways. We try patterns in order.
AY_PATTERNS = [
    # "for the year 2023-24" / "for the year 2023-2024"
    (re.compile(r"\bfor\s+the\s+year\s+(\d{4})[-–](\d{2,4})", re.I), "range"),
    # "for the year(s) 2017-22" (multi-year performance audits)
    (re.compile(r"\bfor\s+the\s+years?\s+(\d{4})[-–](\d{2,4})", re.I), "range"),
    # "for the period 2019-22"
    (re.compile(r"\bfor\s+the\s+period\s+(\d{4})[-–](\d{2,4})", re.I), "range"),
    # "year ended 31 March 2024" or "year ended March 2024" → prev year – YY
    (re.compile(r"\byear\s+ended\s+(?:31\s+)?(?:march|mar)\s+(\d{4})", re.I), "ended"),
    # "period ended March 2023" → same logic
    (re.compile(r"\bperiod\s+ended\s+(?:march|mar)\s+(\d{4})", re.I), "ended"),
    # "period ended 31 March 2024"
    (re.compile(r"\bperiod\s+ended\s+31\s+(?:march|mar)\s+(\d{4})", re.I), "ended"),
    # Fallback: any "2019-20" pattern in title
    (re.compile(r"\b(\d{4})[-–](\d{2,4})\b"), "range"),
]


def normalize_year_range(start: int, end_frag: str) -> str:
    """Given start year (4 digits) and end fragment (2 or 4 digits), return 'YYYY-YY'."""
    if len(end_frag) == 2:
        return f"{start}-{end_frag}"
    if len(end_frag) == 4:
        end_year = int(end_frag)
        return f"{start}-{str(end_year)[-2:]}"
    return f"{start}-{end_frag}"


def extract_audit_year(title: str) -> str:
    if not title:
        return ""
    for regex, kind in AY_PATTERNS:
        m = regex.search(title)
        if not m:
            continue
        if kind == "range":
            return normalize_year_range(int(m.group(1)), m.group(2))
        if kind == "ended":
            end = int(m.group(1))
            start = end - 1
            return f"{start}-{str(end)[-2:]}"
    return ""


# ── Report-number extractor (bonus) ──────────────────────────────────────────
REPORT_NO_RE = re.compile(r"\bReport\s+No\.?\s*(\d+)\s+of\s+(\d{4})\b", re.I)


def extract_report_no(title: str) -> str:
    """'Report No. 1 of 2025' → '1/2025'. Useful for de-dup."""
    if not title:
        return ""
    m = REPORT_NO_RE.search(title)
    return f"{m.group(1)}/{m.group(2)}" if m else ""


# ── Publication year (from URL path or report number) ────────────────────────
# CAG stores PDFs at paths like:
#   https://cag.gov.in/webroot/uploads/download_audit_report/2019/....pdf
# The 4-digit year in the path is the audit-cycle year (year the audit covers,
# typically the fiscal year the report is FOR — not when it was published).
URL_YEAR_RE = re.compile(r"/download_audit_report/(\d{4})/", re.I)


def extract_url_year(pdf_url: str) -> int | None:
    if not pdf_url:
        return None
    m = URL_YEAR_RE.search(pdf_url)
    return int(m.group(1)) if m else None


def audit_year_fallback(row: dict) -> str:
    """Use URL path year or 'Report No X of YYYY' as fallback when title
    doesn't explicitly state the audit year.

    Preference order:
      1. Explicit audit year already extracted from title (e.g. '2017-18')
      2. URL path year — the /YYYY/ directory (usually = audit fiscal year end)
      3. Report No YYYY - 1 — reports are published ~1 year after the audit year
    """
    if row.get("audit_year"):
        return row["audit_year"]

    url_year = extract_url_year(row.get("pdf_url", ""))
    if url_year:
        # URL path year matches the audit year END (e.g., /2019/ = FY2018-19)
        return f"{url_year - 1}-{str(url_year)[-2:]}"

    rn = row.get("report_no", "")
    if rn and "/" in rn:
        pub_year = int(rn.split("/")[1])
        # Published ~1 year after audit — infer audit year ≈ pub_year - 2
        aud_end = pub_year - 1
        return f"{aud_end - 1}-{str(aud_end)[-2:]}"

    return ""


# ── Main ─────────────────────────────────────────────────────────────────────
def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                  formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dry-run", action="store_true",
                    help="Show enrichment counts without writing back")
    args = ap.parse_args()

    if not MANIFEST.exists():
        raise SystemExit(f"Manifest not found: {MANIFEST.relative_to(ROOT)}")

    with MANIFEST.open("r", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
        fieldnames = list(rows[0].keys()) if rows else []

    # Add report_no column if missing
    if "report_no" not in fieldnames:
        fieldnames.append("report_no")

    counts = {"type_filled": 0, "year_filled": 0, "report_no_filled": 0,
              "still_missing_type": 0, "still_missing_year": 0}

    for r in rows:
        title = r.get("title", "")

        if not r.get("report_type"):
            typ = classify(title)
            if typ:
                r["report_type"] = typ
                counts["type_filled"] += 1

        if not r.get("audit_year"):
            yr = extract_audit_year(title)
            if yr:
                r["audit_year"] = yr
                counts["year_filled"] += 1

        if not r.get("report_no"):
            rn = extract_report_no(title)
            if rn:
                r["report_no"] = rn
                counts["report_no_filled"] += 1

        # Fallback: use URL path year or report-no year for still-empty years.
        # Must run AFTER report_no extraction since audit_year_fallback uses it.
        if not r.get("audit_year"):
            fb = audit_year_fallback(r)
            if fb:
                r["audit_year"] = fb
                counts["year_from_fallback"] = counts.get("year_from_fallback", 0) + 1
                counts["year_filled"] += 1

        if not r.get("report_type"):
            counts["still_missing_type"] += 1
        if not r.get("audit_year"):
            counts["still_missing_year"] += 1

    print(f"Rows in manifest: {len(rows)}")
    print()
    print(f"Enrichment summary:")
    print(f"  report_type filled:     {counts['type_filled']}")
    print(f"  audit_year filled:      {counts['year_filled']}")
    print(f"  report_no filled:       {counts['report_no_filled']}")
    print(f"  Still missing type:     {counts['still_missing_type']}")
    print(f"  Still missing year:     {counts['still_missing_year']}")

    # Distribution
    from collections import Counter
    print()
    print("report_type distribution:")
    for t, n in Counter(r["report_type"] for r in rows).most_common():
        print(f"  [{t or '(empty)'}]  {n}")

    if args.dry_run:
        print("\n(dry run — no writes)")
        return

    # Write back
    tmp = MANIFEST.with_suffix(".csv.tmp")
    with tmp.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames)
        w.writeheader()
        for r in rows:
            w.writerow({k: r.get(k, "") for k in fieldnames})
    tmp.replace(MANIFEST)
    print(f"\nWritten to {MANIFEST.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
