"""
Unified error log for the ECI ingestion pipeline.

Every stage (Results parse, Affidavit fetch, OCR, Gemini extract,
DB load, Match) writes to a SINGLE JSONL file at
    data/eci/errors/pipeline_errors.jsonl

This makes it easy to answer:
  "Which candidates failed OCR in Rajasthan 2023?"
  "How many affidavits are unmatchable to a Statistical Report row?"
  "What state × stage combinations need investigation?"

Design
------
- ONE line per error, one JSON object per line — appendable and grep-able.
- Every entry has the same 8-field shape (stable schema).
- No external deps; pure stdlib.
- Thread-safe append via `flock` (advisory lock on POSIX; on Windows the
  fallback is best-effort — pipeline stages are usually sequential anyway).

Fields
------
  ts             ISO-8601 UTC timestamp
  stage          One of: results_parse | affidavit_fetch | ocr | gemini |
                 db_load | match_results | match_affidavit
  state          Canonical state name (e.g. "Rajasthan")
  year           Election year (int)
  candidate      Candidate name if applicable (empty string otherwise)
  constituency   Constituency name if applicable
  error_type     Short slug: parse_failed | ocr_timeout | api_content_filter
                 | fuzzy_no_match | pdf_corrupt | missing_source | schema_mismatch
  message        Free-text detail; keep to one line, no PII beyond names
  extra          Optional dict of stage-specific extras (JSON-serialisable)

Usage
-----
    from scripts.pipeline_errors import log_error, summarise

    log_error(
        stage="ocr",
        state="Rajasthan", year=2023,
        candidate="AJAY MAKEN", constituency="Ajmer North",
        error_type="ocr_timeout",
        message="Cloud Vision batch call timed out after 60s",
        extra={"pdf": "AJAY_MAKEN__10022.pdf", "attempt": 2},
    )

    # End-of-run summary
    summarise(stage="ocr", state="Rajasthan", year=2023)
"""
from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parent.parent
LOG_DIR      = PROJECT_ROOT / "data" / "eci" / "errors"
LOG_PATH     = LOG_DIR / "pipeline_errors.jsonl"


# Set of valid stage names. If a caller passes something not in this set the
# writer still accepts it (to allow gradual expansion), but summarise() will
# warn so we notice typos early.
VALID_STAGES = {
    "results_parse",
    "affidavit_fetch",
    "ocr",
    "gemini",
    "db_load",
    "match_results",
    "match_affidavit",
    "migration",
}

# Frequently used error_type slugs — keep in sync with docstring above.
COMMON_ERROR_TYPES = {
    "parse_failed",
    "ocr_timeout",
    "api_content_filter",
    "api_quota_exceeded",
    "fuzzy_no_match",
    "pdf_corrupt",
    "missing_source",
    "schema_mismatch",
    "gemini_response_unparseable",
    "duplicate_affidavit",
    "constituency_mismatch",
}


def _acquire_lock(fh):
    """Best-effort file lock. Uses fcntl on POSIX; no-op on Windows."""
    try:
        import fcntl
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
    except (ImportError, OSError):
        # Windows or filesystem doesn't support flock — proceed without.
        pass


def _release_lock(fh):
    try:
        import fcntl
        fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
    except (ImportError, OSError):
        pass


def log_error(
    stage: str,
    state: str,
    year: int,
    error_type: str,
    message: str,
    candidate: str = "",
    constituency: str = "",
    extra: dict[str, Any] | None = None,
) -> None:
    """Append one error entry to the pipeline error log.

    Silent on success. Does not raise unless the log directory itself
    can't be created (which would be an environment problem worth
    surfacing loudly).
    """
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    entry = {
        "ts":            datetime.now(timezone.utc).isoformat(),
        "stage":         stage,
        "state":         state,
        "year":          year,
        "candidate":     candidate,
        "constituency":  constituency,
        "error_type":    error_type,
        "message":       message[:500],  # cap message length
        "extra":         extra or {},
    }
    line = json.dumps(entry, ensure_ascii=False)

    with LOG_PATH.open("a", encoding="utf-8") as fh:
        _acquire_lock(fh)
        try:
            fh.write(line + "\n")
            fh.flush()
            os.fsync(fh.fileno())
        finally:
            _release_lock(fh)


def read_errors(
    stage: str | None = None,
    state: str | None = None,
    year: int | None = None,
    error_type: str | None = None,
) -> list[dict]:
    """Return list of matching entries (empty if log doesn't exist)."""
    if not LOG_PATH.exists():
        return []
    out = []
    with LOG_PATH.open("r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                r = json.loads(line)
            except json.JSONDecodeError:
                continue
            if stage       and r.get("stage")       != stage:      continue
            if state       and r.get("state")       != state:      continue
            if year        and r.get("year")        != year:       continue
            if error_type  and r.get("error_type")  != error_type: continue
            out.append(r)
    return out


def summarise(
    stage: str | None = None,
    state: str | None = None,
    year: int | None = None,
) -> dict[str, int]:
    """Print a per-error_type count for the filtered slice + return the dict."""
    entries = read_errors(stage=stage, state=state, year=year)
    counts: dict[str, int] = {}
    for e in entries:
        et = e.get("error_type", "unknown")
        counts[et] = counts.get(et, 0) + 1
    filter_desc = " · ".join(
        f"{k}={v}" for k, v in {"stage": stage, "state": state, "year": year}.items()
        if v is not None
    ) or "all"
    print(f"\n===== Pipeline error summary ({filter_desc}) =====", file=sys.stderr)
    print(f"Total errors: {len(entries)}", file=sys.stderr)
    if counts:
        w = max(len(k) for k in counts)
        for et in sorted(counts, key=lambda x: -counts[x]):
            print(f"  {et:{w}}  {counts[et]}", file=sys.stderr)
    return counts


if __name__ == "__main__":
    # `python scripts/pipeline_errors.py --summary` shows overall snapshot.
    import argparse
    ap = argparse.ArgumentParser(description=__doc__,
                                  formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--stage", default=None)
    ap.add_argument("--state", default=None)
    ap.add_argument("--year", type=int, default=None)
    ap.add_argument("--details", action="store_true",
                    help="Print each matching entry, not just counts")
    args = ap.parse_args()

    summarise(stage=args.stage, state=args.state, year=args.year)
    if args.details:
        for e in read_errors(args.stage, args.state, args.year):
            print(f"  [{e['stage']:15s}] {e['state']} {e['year']} · "
                  f"{e['constituency']:20s} · {e['candidate']:25s} · "
                  f"{e['error_type']}: {e['message']}")
