"""Download Punjab CAG audit reports post-2017 for Congress-vs-AAP comparison.

The user's focus: compare governance under Congress (2017-2022) to AAP
(2022-onward). Punjab elections: Feb 2017 (Congress win, Amarinder → Channi)
and Feb 2022 (AAP win, Bhagwant Mann).

This script pulls 30 CAG reports covering audit years 2017-18 through
2023-24 across every report category:

    STATE FINANCES (SFAR)       — annual fiscal audit (debt, deficit, expenditure)
    STATE REVENUES              — tax + non-tax revenue audits
    COMPLIANCE                  — audit paras raised, expenditure irregularities
    PSU / GPFR                  — public sector enterprise finances
    LOCAL BODIES                — municipality + panchayat audits
    PERFORMANCE                 — sector-specific deep-dives (Health, Education,
                                  Power/UDAY, DBT, MGNREGA, PAU, SWM, 74th CAA)

Reports are tagged with tenure:
    congress   — audit year fully within Congress tenure (2017-18 to 2021-22)
    aap        — audit year fully within AAP tenure (2022-23 onwards)
    transition — audit spans the Feb 2022 handover

Saves to  data/cag/punjab/  with a manifest CSV listing every downloaded file.

Usage:
    python scripts/gpi_download_cag_punjab.py                # all 30 reports
    python scripts/gpi_download_cag_punjab.py --category sfar       # SFARs only
    python scripts/gpi_download_cag_punjab.py --tenure aap          # AAP era only
    python scripts/gpi_download_cag_punjab.py --tenure congress     # Congress era
    python scripts/gpi_download_cag_punjab.py --force               # re-download
    python scripts/gpi_download_cag_punjab.py --dry-run             # just list

Total: ~250MB across 30 PDFs. 5-15 minutes typical.
"""
from __future__ import annotations
import argparse
import csv
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT_DIR = ROOT / "data" / "cag" / "punjab"
MANIFEST = OUT_DIR / "manifest.csv"


# ══════════════════════════════════════════════════════════════════════════════
# Reports catalog. Each tuple:
#   (audit_year, category, tenure, filename, size_mb, title, url)
#
# Categories:
#   sfar          — State Finances Audit Report (annual, fiscal indicators)
#   revenue       — State Revenues audit
#   compliance    — Compliance / Non-PSU / Composite audit
#   psu           — State Public Sector Enterprises
#   local_bodies  — Municipal + Panchayat audit
#   performance   — Sector-specific performance audit
#
# Tenure (Punjab elections: Feb 2017 = Congress, Feb 2022 = AAP):
#   congress   — audit year 2017-18 to 2021-22 (Congress tenure)
#   aap        — audit year 2022-23 onwards (AAP tenure)
#   transition — audit period spans both (multi-year PAs like 2017-22)
# ══════════════════════════════════════════════════════════════════════════════
REPORTS = [
    # ── STATE FINANCES (SFARs) — 7 reports across FY18-FY24 ────────────────
    ("2017-18", "sfar", "congress", "sfar-2017-18.pdf", 4.68,
     "SFAR 2017-18 (Report No. 1 of 2019)",
     "https://cag.gov.in/webroot/uploads/download_audit_report/2019/Report_No_1_of_2019_State_Finances_Government_of_Punjab.pdf"),
    ("2018-19", "sfar", "congress", "sfar-2018-19.pdf", 9.72,
     "SFAR 2018-19 (Report No. 1 of 2020)",
     "https://cag.gov.in/webroot/uploads/download_audit_report/2020/Report_No_1_of_2020_State_Finances_Government_of_Punjab-060421c396cbde7.41535789.pdf"),
    ("2019-20", "sfar", "congress", "sfar-2019-20.pdf", 4.09,
     "SFAR 2019-20 (Report No. 2 of 2021)",
     "https://cag.gov.in/webroot/uploads/download_audit_report/2020/SFAR 2019-20_Report No. 2 of 2021_English-062bd436598c824.74192833.pdf"),
    ("2020-21", "sfar", "congress", "sfar-2020-21.pdf", 4.54,
     "SFAR 2020-21 (Report No. 8 of 2021)",
     "https://cag.gov.in/webroot/uploads/download_audit_report/2021/SFAR 2020-21_Report No. 8 of 2021_English_GoP-062bd69db64fce1.23154342.pdf"),
    ("2021-22", "sfar", "congress", "sfar-2021-22.pdf", 2.5,
     "SFAR 2021-22 (Report No. 2 of 2023)",
     "https://cag.gov.in/webroot/uploads/download_audit_report/2022/SFAR-2021-22_Report-No.-2-of-2023_English-GoP-064072244c03603.74271054.pdf"),
    ("2022-23", "sfar", "aap", "sfar-2022-23.pdf", 15.98,
     "SFAR 2022-23 (Report No. 2 of 2024)",
     "https://cag.gov.in/webroot/uploads/download_audit_report/2023/STATE-FINANCE-AUDIT-REPORT-ENGLISH-final-Version-3.6.2024-066d85035115c02.99920146.pdf"),
    ("2023-24", "sfar", "aap", "sfar-2023-24.pdf", 69.46,
     "SFAR 2023-24 (Report No. 1 of 2025)",
     "https://cag.gov.in/webroot/uploads/download_audit_report/2024/Report-No.-1-of-2025-(English)-069b80076a56220.73144506.pdf"),

    # ── STATE REVENUES — 4 reports ─────────────────────────────────────────
    ("2017-18", "revenue", "congress", "revenue-2017-18.pdf", 2.24,
     "Revenue Sector 2017-18 (Report No. 3 of 2019)",
     "https://cag.gov.in/webroot/uploads/download_audit_report/2019/Report_No_3_of_2019_Revenue_Sector_Government_of_Punjab.pdf"),
    ("2019-20", "revenue", "congress", "revenue-2019-20.pdf", 1.17,
     "Revenue Sector 2019-20 (Report No. 3 of 2021)",
     "https://cag.gov.in/webroot/uploads/download_audit_report/2021/Report No. 3 of 2021 Govt. of Punjab_RS-062e24e042b7c91.64232930.pdf"),
    ("2022-23", "revenue", "aap", "revenue-2022-23.pdf", 37.03,
     "State Revenues 2022-23 (Report No. 3 of 2025)",
     "https://cag.gov.in/webroot/uploads/download_audit_report/2026/Revenue-Report-3of2025-English-069b7f9ea3b6970.29786894.pdf"),
    ("2023-24", "revenue", "aap", "revenue-2023-24.pdf", 9.9,
     "State Revenues 2023-24 (Report No. 6 of 2025)",
     "https://cag.gov.in/webroot/uploads/download_audit_report/2024/Revenue-Report-6-of-2025-English-069b80b9fbeaf87.22849838.pdf"),

    # ── COMPLIANCE AUDITS — 7 reports ──────────────────────────────────────
    ("2017-18", "compliance", "congress", "compliance-non-psu-2017-18.pdf", 12.32,
     "Non-PSU Social/General/Economic 2017-18 (Report No. 4 of 2019)",
     "https://cag.gov.in/webroot/uploads/download_audit_report/2019/Report_No_4_of_2019_Social_General_and_Economic_Sectors_Non_PSUs_Government_of_Punjab.pdf"),
    ("2018-19", "compliance", "congress", "compliance-non-psu-2018-19.pdf", 3.46,
     "Non-PSU + Revenue 2018-19 (Report No. 1 of 2021)",
     "https://cag.gov.in/webroot/uploads/download_audit_report/2019/Report No. 1 of 2021 (N-PSUs & Revenue 2018-19)_English-062bd40b90df757.51859911.pdf"),
    ("2019-20", "compliance", "congress", "compliance-2019-20.pdf", 1.65,
     "Compliance Social/General/Economic 2019-20 (Report No. 4 of 2021)",
     "https://cag.gov.in/webroot/uploads/download_audit_report/2021/Report No. 4 of 2021 Govt. of Punjab_CA-062e77ee6b95750.20819070.pdf"),
    ("2020-21", "compliance", "congress", "compliance-2020-21.pdf", 5.81,
     "Compliance Audit 2020-21 (Report No. 3 of 2022)",
     "https://cag.gov.in/webroot/uploads/download_audit_report/2021/REPORT~1-064072f81596b47.66840194.pdf"),
    ("2021-22", "compliance", "congress", "compliance-i-2021-22.pdf", 5.07,
     "Compliance Audit-I 2021-22 (Report No. 1 of 2024)",
     "https://cag.gov.in/webroot/uploads/download_audit_report/2022/Report-No.-1-of-2024-(Compliance-Audit-I_-2021-22)-_GoP-(English)-066d847e382e4a8.83864913.pdf"),
    ("2021-22", "compliance", "congress", "compliance-ii-2021-22.pdf", 16.15,
     "Compliance Audit-II 2021-22 (Report No. 3 of 2024)",
     "https://cag.gov.in/webroot/uploads/download_audit_report/2022/Report-No.-3-of-2024-066d833442a9290.00434272.pdf"),
    ("2022-23", "compliance", "aap", "compliance-composite-2022-23.pdf", 8.98,
     "Composite Audit (Civil) 2022-23 (Report No. 4 of 2025)",
     "https://cag.gov.in/webroot/uploads/download_audit_report/2023/Report-no.-4-of-2025-(English)-069b7fc7885b9c7.82304696.pdf"),
    ("2023-24", "compliance", "aap", "compliance-civil-2023-24.pdf", 5.58,
     "Compliance Audit (Civil) 2023-24 (Report No. 7 of 2025)",
     "https://cag.gov.in/webroot/uploads/download_audit_report/2024/Report-No.-7-of-2025-(English)-069b80931716528.36504892.pdf"),

    # ── PSU / GPFR — 3 reports ─────────────────────────────────────────────
    ("2018-19", "psu", "congress", "psu-social-general-2018-19.pdf", 2.16,
     "PSUs Social/General/Economic 2018-19 (Report No. 2 of 2020)",
     "https://cag.gov.in/webroot/uploads/download_audit_report/2020/Report_No_2_of_2020_PSUs_Social_General_and_Economic_Sectors_Government_of_Punjab-060422409c47c31.61109984.pdf"),
    ("2019-20", "psu", "congress", "psu-gpfr-2019-20.pdf", 1.62,
     "PSE GPFR 2019-20 (Report No. 5 of 2021)",
     "https://cag.gov.in/webroot/uploads/download_audit_report/2020/Report No. 5 of 2021_GPFR 2019-20_English (single file)-062bd70ed543722.47589621.pdf"),
    ("2022-23", "psu", "aap", "psu-2022-23.pdf", 5.02,
     "State PSEs 2022-23 (Report No. 2 of 2025)",
     "https://cag.gov.in/webroot/uploads/download_audit_report/2023/Report-No.2-of-2025--State-Public-Sector-Enterprises---Government-of-Punjab-(English)-069b7f6caa566c0.97619237.pdf"),

    # ── LOCAL BODIES — 1 report ────────────────────────────────────────────
    ("2022-23", "local_bodies", "aap", "local-bodies-2022-23.pdf", 3.05,
     "Local Bodies 2022-23 (Report No. 1 of 2026)",
     "https://cag.gov.in/webroot/uploads/download_audit_report/2023/Local-Bodies-Report-2022-23_Report-No.1-of-2026-(English)-069b7febb6c80f2.63929734.pdf"),

    # ── PERFORMANCE AUDITS — 7 sector deep-dives ───────────────────────────
    ("2017-22", "performance", "transition", "pa-pau-swm-2017-22.pdf", 5.43,
     "PA: Punjab Agri University + Solid Waste Mgmt 2017-22 (Report No. 5 of 2025)",
     "https://cag.gov.in/webroot/uploads/download_audit_report/2022/REPORT-NO.-5-_PAU_SWM-_English-069b7f589571ed5.58982746.pdf"),
    ("2019-20", "performance", "congress", "pa-uday-power-2019-20.pdf", 1.4,
     "PA: Pre/Post UDAY Power 2019-20 (Report No. 6 of 2021)",
     "https://cag.gov.in/webroot/uploads/download_audit_report/2022/Report No. 6 of 2021_UDAY 2019-20_English-062bd6ef6366a82.99991410.pdf"),
    ("2020-21", "performance", "congress", "pa-higher-education.pdf", 4.49,
     "PA: Higher Education (Report No. 2 of 2022)",
     "https://cag.gov.in/webroot/uploads/download_audit_report/2022/Report No. 2 of 2022_Higher Education_English_GoP-062bd69d05e2eb6.14183298.pdf"),
    ("2020-21", "performance", "congress", "pa-dbt.pdf", 3.46,
     "PA: Direct Benefit Transfer (Report No. 1 of 2022)",
     "https://cag.gov.in/webroot/uploads/download_audit_report/2022/Report No. 1 of 2022_PA on DBT_English_GoP-062bd528e6530b5.83866648.pdf"),
    ("2020-21", "performance", "congress", "pa-mgnrega.pdf", 2.39,
     "PA: MGNREGA (Report No. 1 of 2023)",
     "https://cag.gov.in/webroot/uploads/download_audit_report/2022/REPORT~1-064071f344dfbf4.83585179.pdf"),
    ("2020-21", "performance", "congress", "pa-74th-caa.pdf", 1.48,
     "PA: 74th Constitutional Amendment (Report No. 7 of 2021)",
     "https://cag.gov.in/webroot/uploads/download_audit_report/2021/Report No. 7 of 2021 Govt. of Punjab_PA-062e0dc92d20019.93581063.pdf"),
    ("2017-22", "performance", "transition", "pa-public-health-2017-22.pdf", 9.5,
     "PA: Public Health Infrastructure (Report No. 4 of 2024)",
     "https://cag.gov.in/webroot/uploads/download_audit_report/2024/PA-Report-on-PHIMHS-(Report-No.-4-of-2024)-067e28cda491020.60257581.pdf"),
]


HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/126.0.0.0 Safari/537.36"
    ),
    "Accept": "application/pdf,application/octet-stream,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://cag.gov.in/ag/punjab/en/audit-report?state%5B0%5D=85",
}


def _is_valid_pdf(path: Path) -> bool:
    """Check the first 4 bytes are the PDF magic (%PDF)."""
    try:
        with path.open("rb") as f:
            return f.read(4) == b"%PDF"
    except Exception:
        return False


def download_one(url: str, out_path: Path, size_mb: float, session) -> bool:
    print(f"  ↓ {out_path.name}  ({size_mb:.1f}MB expected) ...", flush=True)
    t0 = time.time()
    try:
        resp = session.get(url, timeout=180, stream=True)
        resp.raise_for_status()
        tmp = out_path.with_suffix(out_path.suffix + ".tmp")
        bytes_written = 0
        with tmp.open("wb") as f:
            for chunk in resp.iter_content(chunk_size=64 * 1024):
                if chunk:
                    f.write(chunk)
                    bytes_written += len(chunk)
        tmp.rename(out_path)

        if not _is_valid_pdf(out_path):
            out_path.unlink()
            print(f"    ✗ Not a valid PDF (got {bytes_written / 1_048_576:.1f}MB of something else)")
            return False

        dt = time.time() - t0
        actual_mb = bytes_written / 1_048_576
        print(f"    ✓ {actual_mb:.1f}MB in {dt:.1f}s ({actual_mb / max(dt, 0.01):.1f}MB/s)")
        return True
    except Exception as e:
        print(f"    ✗ Failed: {type(e).__name__}: {e}")
        tmp_path = out_path.with_suffix(out_path.suffix + ".tmp")
        if tmp_path.exists():
            tmp_path.unlink()
        return False


def write_manifest(reports_downloaded):
    """Write a CSV of downloaded files so the extractor knows what to process."""
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    with MANIFEST.open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["audit_year", "category", "tenure", "filename",
                     "size_mb", "title", "url"])
        for row in reports_downloaded:
            w.writerow(row)
    print(f"\nManifest → {MANIFEST.relative_to(ROOT)}")


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                  formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--category", choices=["sfar", "revenue", "compliance",
                                             "psu", "local_bodies", "performance"],
                    help="Only download reports from this category")
    ap.add_argument("--tenure", choices=["congress", "aap", "transition"],
                    help="Only download reports from this tenure")
    ap.add_argument("--force", action="store_true",
                    help="Re-download even if file exists")
    ap.add_argument("--dry-run", action="store_true",
                    help="List targets without downloading")
    args = ap.parse_args()

    # Filter
    reports = REPORTS
    if args.category:
        reports = [r for r in reports if r[1] == args.category]
    if args.tenure:
        reports = [r for r in reports if r[2] == args.tenure]

    print(f"CAG Punjab audit reports")
    print(f"Output dir: {OUT_DIR}")
    print(f"Reports:    {len(reports)} (from {len(REPORTS)} total)")
    print(f"Total est.: {sum(r[4] for r in reports):.0f}MB")
    if args.category:
        print(f"Category:   {args.category}")
    if args.tenure:
        print(f"Tenure:     {args.tenure}")
    print()

    # Print grouped summary
    by_tenure = {"congress": [], "aap": [], "transition": []}
    for r in reports:
        by_tenure[r[2]].append(r)
    for tenure, group in by_tenure.items():
        if group:
            print(f"  {tenure.upper():<11s} ({len(group):>2d} reports, "
                  f"{sum(r[4] for r in group):.0f}MB)")
    print()

    if args.dry_run:
        for r in reports:
            print(f"  {r[2]:<11s} {r[1]:<13s} {r[0]:<8s} {r[3]:<40s} {r[4]:>5.1f}MB  {r[5]}")
        return

    try:
        import requests
    except ImportError:
        raise SystemExit("pip install requests")

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    counts = {"downloaded": 0, "skipped": 0, "failed": 0}
    successful = []

    with requests.Session() as sess:
        sess.headers.update(HEADERS)

        for audit_year, category, tenure, filename, size_mb, title, url in reports:
            out_path = OUT_DIR / filename

            if out_path.exists() and _is_valid_pdf(out_path) and not args.force:
                existing_mb = out_path.stat().st_size / 1_048_576
                print(f"  ✓ {filename}  already have ({existing_mb:.1f}MB) — skipping")
                counts["skipped"] += 1
                successful.append((audit_year, category, tenure, filename,
                                     size_mb, title, url))
                continue

            if out_path.exists() and not _is_valid_pdf(out_path):
                out_path.unlink()

            ok = download_one(url, out_path, size_mb, sess)
            if ok:
                counts["downloaded"] += 1
                successful.append((audit_year, category, tenure, filename,
                                     size_mb, title, url))
            else:
                counts["failed"] += 1

    write_manifest(successful)

    print()
    print("═══════════════ Summary ═══════════════")
    print(f"  Downloaded: {counts['downloaded']}")
    print(f"  Skipped (already have): {counts['skipped']}")
    print(f"  Failed: {counts['failed']}")
    print()
    print(f"Files: {OUT_DIR.relative_to(ROOT)}/")


if __name__ == "__main__":
    main()
