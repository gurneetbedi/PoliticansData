"""Fetch Lok Sabha 2024 affidavits state-by-state via ECI's
CandidateCustomFilter endpoint.

The listing endpoint has a hard 2000-row cap, so we shard by state.
Each state gets its own cycle folder under
data/eci/raw_pdfs/loksabha-2024-<state-slug>/  — same structure the rest
of the pipeline (Cloud Vision, Gemini, apply) already understands.

ECI state codes (states=Xnn in the URL) come from the affidavit portal's
own state dropdown. Confirmed values are hardcoded below; add more as
you discover them by inspecting the portal URL when you filter to a
state manually.

Usage:
    # One state (Assam)
    python scripts/fetch_ls_2024_by_state.py --states assam --cdp 9222

    # Multiple sequential
    python scripts/fetch_ls_2024_by_state.py --states assam,bihar --cdp 9222

    # All confirmed states (sequential)
    python scripts/fetch_ls_2024_by_state.py --states all --cdp 9222

Concurrency: each state runs sequentially through this script. Within a
state, fetch_eci_affidavits.py itself parallelises PDF downloads via
--concurrent-tabs (default 4 here). True cross-state parallelism would
need separate Chrome instances on different CDP ports — not implemented
here because Akamai fingerprints aggressively when multiple sessions hit
from the same IP.
"""
from __future__ import annotations
import argparse
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# LS 2024 electionType — same across states.
LS_2024_ELECTION_TYPE = "24-PC-GENERAL-1-46"

# ECI state codes as they appear in `?states=Xnn` on the affidavit portal.
# Fill in more as you confirm them (visit the portal, filter by state,
# read the URL). The map below is initial best-guess for LS 2024 — verify
# each code by opening the URL and confirming the listing shows the
# right state before including it in a batch run.
#
# CORRECTIONS AFTER FIRST RUN (Assam mis-mapped as U01, which is
# actually Andaman & Nicobar Islands). All codes below are the standard
# ECI convention (S-prefix = states, U-prefix = UTs), but each one still
# needs to be verified against the portal — visit
# https://affidavit.eci.gov.in/CandidateCustomFilter?...&states=<code>
# and confirm the returned candidate names match the intended state.
#
# Rows with ✓ have been confirmed by running the fetch; unmarked rows
# are best-guess from ECI convention and MUST be verified.
STATE_CODES: dict[str, tuple[str, str]] = {
    # slug             : (display_name,         eci_code)
    "andamanandnicobar": ("Andaman & Nicobar",   "U01"),  # ✓ confirmed (was mis-labelled Assam)
    "andhrapradesh":    ("Andhra Pradesh",       "S01"),
    "arunachalpradesh": ("Arunachal Pradesh",    "S02"),
    "assam":            ("Assam",                "S03"),  # ✓ confirmed via probe (breadcrumb)
    "bihar":            ("Bihar",                "S04"),
    "chandigarh":       ("Chandigarh",           "U02"),
    "chhattisgarh":     ("Chhattisgarh",         "S26"),
    "dnhdd":            ("Dadra & Nagar Haveli and Daman & Diu", "U03"),
    "delhi":            ("Delhi",                "U05"),
    "goa":              ("Goa",                  "S05"),
    "gujarat":          ("Gujarat",              "S06"),
    "haryana":          ("Haryana",              "S07"),
    "himachalpradesh":  ("Himachal Pradesh",     "S08"),
    "jammuandkashmir":  ("Jammu and Kashmir",    "U08"),
    "jharkhand":        ("Jharkhand",            "S27"),
    "karnataka":        ("Karnataka",            "S10"),
    "kerala":           ("Kerala",               "S11"),
    "ladakh":           ("Ladakh",               "U09"),
    "lakshadweep":      ("Lakshadweep",          "U06"),
    "madhyapradesh":    ("Madhya Pradesh",       "S12"),
    "maharashtra":      ("Maharashtra",          "S13"),
    "manipur":          ("Manipur",              "S14"),
    "meghalaya":        ("Meghalaya",            "S15"),
    "mizoram":          ("Mizoram",              "S16"),
    "nagaland":         ("Nagaland",             "S17"),
    "odisha":           ("Odisha",               "S18"),
    "puducherry":       ("Puducherry",           "U07"),
    "punjab":           ("Punjab",               "S19"),
    "rajasthan":        ("Rajasthan",            "S20"),
    "sikkim":           ("Sikkim",               "S21"),
    "tamilnadu":        ("Tamil Nadu",           "S22"),
    "telangana":        ("Telangana",            "S29"),
    "tripura":          ("Tripura",              "S23"),
    "uttarakhand":      ("Uttarakhand",          "S28"),
    "uttarpradesh":     ("Uttar Pradesh",        "S24"),
    "westbengal":       ("West Bengal",          "S25"),
}


def build_url(state_code: str) -> str:
    """LS 2024 listing URL for one state."""
    return (f"https://affidavit.eci.gov.in/CandidateCustomFilter"
            f"?electionType={LS_2024_ELECTION_TYPE}"
            f"&election={LS_2024_ELECTION_TYPE}"
            f"&states={state_code}")


def fetch_one_state(slug: str, cdp_port: int, tabs: int,
                    dry_run: bool = False) -> int:
    """Invoke fetch_eci_affidavits.py for a single state. Returns exit code."""
    if slug not in STATE_CODES:
        print(f"  ✗ Unknown state slug: {slug!r}. Available: "
              f"{', '.join(sorted(STATE_CODES))}", file=sys.stderr)
        return 1

    name, code = STATE_CODES[slug]
    url = build_url(code)
    output_dir = ROOT / "data" / "eci" / "raw_pdfs" / f"loksabha-2024-{slug}"

    print(f"\n{'='*78}", file=sys.stderr)
    print(f"▶ {name} (states={code})", file=sys.stderr)
    print(f"  URL:    {url}", file=sys.stderr)
    print(f"  Output: {output_dir.relative_to(ROOT)}", file=sys.stderr)
    print(f"  Tabs:   {tabs}", file=sys.stderr)
    print(f"{'='*78}", file=sys.stderr)

    if dry_run:
        return 0

    cmd = [
        sys.executable,
        str(ROOT / "scripts" / "fetch_eci_affidavits.py"),
        "--listing-url", url,
        "--output", str(output_dir),
        "--cdp", str(cdp_port),
        "--concurrent-tabs", str(tabs),
    ]
    t0 = time.time()
    rc = subprocess.call(cmd)
    dt = time.time() - t0
    status = "✓" if rc == 0 else "✗"
    print(f"\n  {status} {name} finished in {dt:.0f}s (exit {rc})", file=sys.stderr)
    return rc


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                  formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--states", default="",
                    help="Comma-separated state slugs (see STATE_CODES in source), "
                         "or 'all' for every confirmed state.")
    ap.add_argument("--cdp", type=int, default=9222,
                    help="CDP port for the running Chrome (default 9222)")
    ap.add_argument("--tabs", type=int, default=4,
                    help="Parallel download tabs within each state (default 4)")
    ap.add_argument("--dry-run", action="store_true",
                    help="Print what would run, don't invoke fetch")
    ap.add_argument("--list", action="store_true",
                    help="Print all known state slugs and their codes, then exit")
    args = ap.parse_args()

    if args.list:
        print(f"{'slug':22s}  {'display name':28s}  code", file=sys.stderr)
        print("-" * 62, file=sys.stderr)
        for slug, (name, code) in sorted(STATE_CODES.items()):
            print(f"{slug:22s}  {name:28s}  {code}", file=sys.stderr)
        return

    if not args.states:
        sys.exit("Need --states (or --list to see options)")
    if args.states.lower() == "all":
        slugs = sorted(STATE_CODES)
    else:
        slugs = [s.strip() for s in args.states.split(",") if s.strip()]

    print(f"Running LS 2024 fetch for {len(slugs)} state(s): {slugs}",
          file=sys.stderr)
    print(f"CDP port: {args.cdp}   Tabs per state: {args.tabs}\n",
          file=sys.stderr)

    ok = fail = 0
    for slug in slugs:
        rc = fetch_one_state(slug, args.cdp, args.tabs, args.dry_run)
        if rc == 0:
            ok += 1
        else:
            fail += 1

    print(f"\n{'='*78}", file=sys.stderr)
    print(f"BATCH SUMMARY: {ok} succeeded, {fail} failed",
          file=sys.stderr)
    print(f"{'='*78}", file=sys.stderr)


if __name__ == "__main__":
    main()
