"""Scrape Chief Minister tenure history from Wikipedia + upsert to DB.

For each state in WIKIPEDIA_CM_LIST_URLS:
  1. Fetch the "List of chief ministers of X" article
  2. Extract the main tenure table (wikitable with Name + tenure dates + party)
  3. Pass structured rows to Gemini to normalize dates + parties
  4. Upsert into chief_minister_terms with change tracking

Uses Gemini rather than pure pypdf/BS4 parsing because Wikipedia's CM
tables have wildly varying formats across states (merged cells, multi-line
dates, footnote markers, acting-CM rows). Gemini's schema-constrained
output is ~5x more reliable than regex parsing for this shape of data.

Cost: ~$0.005 per state × 31 states = ~$0.16 total.

Usage:
    source secrets/.env

    # All 31 states
    python scripts/gpi_ingest_cm_history.py

    # Single state (useful for debugging one URL)
    python scripts/gpi_ingest_cm_history.py --state Punjab

    # Dry run — extraction only, no DB writes
    python scripts/gpi_ingest_cm_history.py --dry-run

    # Re-fetch cached HTML (default: use cache)
    python scripts/gpi_ingest_cm_history.py --force
"""
from __future__ import annotations
import argparse
import json
import os
import re
import sys
import time
from datetime import date, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from scripts.gpi_cm_history_urls import WIKIPEDIA_CM_LIST_URLS

MODEL_NAME = "gemini-2.5-flash"
RAW_HTML_DIR = ROOT / "data" / "cm_history" / "html"
RAW_JSON_DIR = ROOT / "data" / "cm_history" / "extractions"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (LokvaniGPI bot; +https://github.com/gurneetbedi/PoliticansData) "
        "Wikipedia CM tenure scraper"
    ),
}


PROMPT = """You are analyzing a Wikipedia article about the Chief Ministers
of an Indian state. The article contains one or more tables listing CM
tenures (name, party, term dates, election year).

Extract EVERY chief minister since 1990 (skip pre-1990 history entirely).

RETURN FORMAT — plain text, one CM per line, pipe-delimited:

    name | party | sworn_in_date | end_date | election_year | notes

Rules for each field:
  • name           — Full name, no titles, no birth-death dates, no
                       parentheticals.
  • party          — Common abbreviation only (BJP, INC, AAP, DMK, AIADMK,
                       TMC, BJD, SP, BSP, JD(U), SHS, NCP, JMM, TDP, YSRCP,
                       BRS, TVK, JD(S), PDP, JKN, NPP, NPF, NDPP, SKM, SDF,
                       MGP, AINRC, RJD, INLD, JJP, MNF, ZPM, etc.). If a
                       coalition, use the leading party. Do NOT include
                       parentheses beyond what's shown here.
  • sworn_in_date  — YYYY-MM-DD. Use "01" for day if only month/year known.
  • end_date       — YYYY-MM-DD, OR the literal word "incumbent" for the
                       currently-serving CM, OR "" (empty) if unknown.
  • election_year  — 4-digit year of the general election that gave the CM
                       power (e.g. 2022 for Punjab Bhagwant Mann). Use ""
                       if the CM took over mid-term.
  • notes          — Optional single word/phrase like "acting", "resigned",
                       "died in office". Otherwise "".

STRICT RULES:
  1. One line per tenure. If a CM held office in multiple non-consecutive
     terms, emit one line per term.
  2. Skip "President's Rule" rows entirely.
  3. Skip rows before 1990.
  4. Order oldest-to-newest.
  5. First line of output is a header: name | party | sworn_in_date | end_date | election_year | notes
  6. No commentary, no code fences, no JSON. Plain text only. Every line
     after the header is a CM record.
"""

# Not needed for plain-text mode, but kept for structural clarity if we ever
# revert to structured output.
RESPONSE_SCHEMA = None


def fetch_html(url: str, force: bool = False) -> str:
    import requests
    RAW_HTML_DIR.mkdir(parents=True, exist_ok=True)
    slug = re.sub(r"\W+", "_", url.split("/")[-1])[:100]
    cache = RAW_HTML_DIR / f"{slug}.html"
    if cache.exists() and not force and cache.stat().st_size > 5000:
        return cache.read_text(encoding="utf-8")

    r = requests.get(url, headers=HEADERS, timeout=30)
    r.raise_for_status()
    cache.write_text(r.text, encoding="utf-8")
    time.sleep(1)   # be nice to Wikipedia
    return r.text


def extract_tenure_text(html: str) -> str:
    """Reduce the Wikipedia page to just the wikitable(s) that contain CM tenure.

    Two-pass matching to handle both article formats:
      1. "List of chief ministers of X" — usually one canonical tenure table
         with headers like "Name | Portrait | Term | Party | Election"
      2. "Chief Minister of X" — often has multiple tables (era-by-era,
         with headers that may span rows via colspan). We scan ALL tables
         and keep any that look tenure-y based on cell CONTENT patterns
         (date strings, party abbreviations) rather than header text alone.

    Also caps output at ~40KB to keep Gemini calls within token budget.
    """
    from bs4 import BeautifulSoup
    soup = BeautifulSoup(html, "html.parser")
    parts: list[str] = []
    seen_row_signatures: set[str] = set()

    for tbl in soup.select("table.wikitable"):
        # Serialize the table as plain text first
        table_rows: list[str] = []
        for row in tbl.select("tr"):
            cells = [c.get_text(" ", strip=True) for c in row.select("th, td")]
            if cells and any(cells):
                table_rows.append(" | ".join(cells))
        if len(table_rows) < 3:
            continue    # not a real roster

        table_text = "\n".join(table_rows).lower()

        # Heuristic: keep the table if EITHER
        #  (a) header mentions name/CM AND party/term/tenure/election, OR
        #  (b) content shows date patterns (YYYY) + party abbreviations
        #      AND references CMs / Indian political terminology
        header_pass = (
            ("name" in table_rows[0].lower() or "chief minister" in table_rows[0].lower())
            and any(w in table_rows[0].lower()
                     for w in ["party", "tenure", "term", "election", "office"])
        )
        # Content pass: any wikitable with (a) multiple 4-digit years AND
        # (b) either party language OR "chief minister" mentions. Broadened
        # from the earlier party-name allowlist which missed state-specific
        # parties (AINRC for Puducherry, NPP for Meghalaya, MNF for Mizoram,
        # SKM for Sikkim, PDP for J&K etc.).
        n_years = len(re.findall(r"\b(19|20)\d{2}\b", table_text))
        has_party_hint = (
            "party" in table_text
            or " inc " in table_text or " bjp" in table_text
            or " tdp" in table_text or "congress" in table_text
            or "sena" in table_text or "front" in table_text
            or "aiadmk" in table_text or "dmk" in table_text
            or re.search(r"\b(bjp|inc|tdp|nc|ncp|jd|sp|bsp|ainrc|npp|nff|mnf|zpm|skm|sdf|pdp|jkn|shiv|rjd|jmm|biju|bjd|tmc)\b", table_text)
        )
        has_office_hint = ("minister" in table_text or "chief" in table_text
                            or "sworn" in table_text or "office" in table_text
                            or "tenure" in table_text or "term" in table_text
                            or "elected" in table_text)
        content_pass = n_years >= 4 and has_party_hint and has_office_hint
        if not (header_pass or content_pass):
            continue

        for r in table_rows:
            sig = r[:120]
            if sig in seen_row_signatures:
                continue
            seen_row_signatures.add(sig)
            parts.append(r)
        parts.append("")   # blank line between tables

        if sum(len(p) for p in parts) > 40_000:
            break    # cap the input to keep Gemini fast + cheap

    return "\n".join(parts)


def call_gemini(tenure_text: str, state_name: str) -> dict:
    try:
        from google import genai
        from google.genai import types
    except ImportError:
        raise SystemExit("pip install google-genai")

    project = os.environ.get("GCP_PROJECT")
    if not project:
        raise SystemExit("GCP_PROJECT not set — source secrets/.env")

    client = genai.Client(vertexai=True, project=project, location="us-central1")

    prompt = (
        f"State: {state_name}\n\n"
        f"Wikipedia tenure table text:\n\n{tenure_text}\n\n"
        f"---\n{PROMPT}"
    )
    # Retry with exponential backoff on 429 rate limits.
    last_err = None
    for attempt in range(4):
        try:
            resp = client.models.generate_content(
                model=MODEL_NAME,
                contents=[prompt],
                config=types.GenerateContentConfig(
                    temperature=0.0,
                    max_output_tokens=32768,
                ),
            )
            # Parse pipe-delimited plain text into a list of term dicts.
            terms = _parse_pipe_response(resp.text or "")
            return {"terms": terms}
        except Exception as e:
            last_err = e
            msg = str(e).lower()
            if "429" in msg or "resource_exhausted" in msg or "quota" in msg:
                sleep_s = 5 * (2 ** attempt)   # 5, 10, 20, 40
                print(f"    ⏳ rate limited, retrying in {sleep_s}s ...", flush=True)
                time.sleep(sleep_s)
                continue
            raise
    raise last_err


def _parse_pipe_response(text: str) -> list[dict]:
    """Parse Gemini's plain-text output into structured records.

    Expected format (first line is a header, subsequent lines are CMs):
        name | party | sworn_in_date | end_date | election_year | notes
        Bhagwant Mann | AAP | 2022-03-16 | incumbent | 2022 |
        ...

    Robust to leading/trailing whitespace, missing fields, "incumbent",
    and "None"/"null" placeholder values.
    """
    out: list[dict] = []
    lines = [ln.strip() for ln in text.strip().splitlines() if ln.strip()]
    if not lines:
        return out

    # Skip the header if it's obviously a header (contains "sworn_in" or "name")
    if any(w in lines[0].lower() for w in ["sworn_in", "election_year"]):
        lines = lines[1:]

    for line in lines:
        parts = [p.strip() for p in line.split("|")]
        if len(parts) < 3:
            continue

        def _get(i, default=""):
            return parts[i] if i < len(parts) else default

        name  = _get(0)
        party = _get(1)
        sworn = _get(2)
        end   = _get(3)
        elyr  = _get(4)
        notes = _get(5)

        if end.lower() in ("incumbent", "present", "current", "null", "none", "-"):
            end = ""
        if elyr.lower() in ("null", "none", "-", ""):
            elyr = None
        else:
            try:
                elyr = int(elyr)
            except ValueError:
                elyr = None

        if not name or len(name) < 3:
            continue

        out.append({
            "name":          name,
            "party":         party or None,
            "sworn_in_date": sworn,
            "end_date":      end,
            "election_year": elyr,
            "notes":         notes,
        })
    return out


def _parse_date(s: str | None) -> date | None:
    if not s or s.strip() == "":
        return None
    try:
        return datetime.strptime(s.strip(), "%Y-%m-%d").date()
    except ValueError:
        return None


def upsert_cm_terms(session, state, terms: list[dict], source_url: str,
                      dry_run: bool) -> dict:
    """Wipe + reinsert this state's CM history. Simpler than diff-tracking
    since Wikipedia is the source of truth and full re-loads are cheap."""
    from app.gpi_models import ChiefMinisterTerm

    counts = {"deleted": 0, "inserted": 0, "skipped": 0}

    # Delete existing rows for this state so re-runs replace history cleanly
    deleted = (
        session.query(ChiefMinisterTerm)
        .filter_by(state_id=state.id)
        .delete(synchronize_session=False)
    )
    counts["deleted"] = deleted or 0

    for t in terms:
        sworn = _parse_date(t.get("sworn_in_date"))
        if sworn is None:
            counts["skipped"] += 1
            continue
        session.add(ChiefMinisterTerm(
            state_id      = state.id,
            name          = t.get("name", "").strip(),
            party         = (t.get("party") or "").strip() or None,
            sworn_in_date = sworn,
            end_date      = _parse_date(t.get("end_date")),
            election_year = t.get("election_year"),
            source_url    = source_url,
            source_type   = "wikipedia",
            notes         = (t.get("notes") or "").strip() or None,
        ))
        counts["inserted"] += 1

    if not dry_run:
        session.commit()
    else:
        session.rollback()

    return counts


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                  formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--state", help="Only scrape this state (default: all)")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--force", action="store_true",
                    help="Re-fetch HTML + re-call Gemini even if cached")
    args = ap.parse_args()

    try:
        import requests   # noqa
        from bs4 import BeautifulSoup   # noqa
    except ImportError:
        raise SystemExit("pip install requests beautifulsoup4")

    RAW_JSON_DIR.mkdir(parents=True, exist_ok=True)

    from app.database import SessionLocal
    from app.models import State
    session = SessionLocal()

    grand = {"deleted": 0, "inserted": 0, "skipped": 0}
    processed = 0

    for state_name, url in WIKIPEDIA_CM_LIST_URLS.items():
        if args.state and state_name != args.state:
            continue

        st = session.query(State).filter_by(name=state_name).one_or_none()
        if not st:
            print(f"  ✗ {state_name}: state not in DB, skipping")
            continue

        print(f"→ {state_name}  {url}")
        try:
            html = fetch_html(url, force=args.force)
        except Exception as e:
            print(f"  ✗ fetch error: {type(e).__name__}: {e}")
            continue

        tenure_text = extract_tenure_text(html)
        if not tenure_text or len(tenure_text) < 200:
            print(f"  ✗ no tenure table found — inspect the page manually")
            continue

        cache_path = RAW_JSON_DIR / f"{state_name.lower().replace(' ', '_')}.gemini.json"
        if cache_path.exists() and not args.force:
            extraction = json.loads(cache_path.read_text())
            print(f"  ✓ cached extraction: {cache_path.relative_to(ROOT)}")
        else:
            try:
                t0 = time.time()
                extraction = call_gemini(tenure_text, state_name)
                cache_path.write_text(json.dumps(extraction, indent=2, ensure_ascii=False))
                print(f"  ✓ Gemini extracted {len(extraction.get('terms', []))} terms in {time.time()-t0:.0f}s")
            except Exception as e:
                print(f"  ✗ Gemini error: {type(e).__name__}: {e}")
                continue

        counts = upsert_cm_terms(session, st, extraction.get("terms", []),
                                    url, args.dry_run)
        print(f"    → inserted={counts['inserted']} deleted={counts['deleted']} "
              f"skipped={counts['skipped']}")
        for k in grand:
            grand[k] += counts[k]
        processed += 1

    session.close()
    print()
    print("═══════════════════════════════════════════════════════")
    print(f"States processed: {processed}")
    print(f"  Inserted: {grand['inserted']}  Deleted: {grand['deleted']}  Skipped: {grand['skipped']}")
    if args.dry_run:
        print("\n(dry run — no writes)")


if __name__ == "__main__":
    main()
