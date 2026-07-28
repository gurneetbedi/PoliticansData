"""For a given state cycle, list every allowlist candidate that has no
wealth data in LOCAL SQLite, and diagnose WHY:

Categorizes each gap into one of:
  no_pdf              — PDF file doesn't exist on disk
  corrupt_pdf         — PDF exists but is <1KB or has bad magic bytes
  no_cloud_vision     — PDF present, but no preprocessed JSON
  empty_ocr           — Cloud Vision ran but returned <200 chars of text
  no_gemini           — OCR text present, but no Gemini JSON
  gemini_raw          — Gemini stored _raw (max-token truncation)
  gemini_empty        — Gemini extraction has empty name+const
  gemini_ok_but_apply — Gemini extraction is complete but not in DB
                        (apply matcher failed to find the appearance row)

Writes: data/reports/gaps_<slug>_<year>.csv

Usage:
    python scripts/diagnose_state_gaps_sqlite.py --state "Rajasthan" --year 2023
"""
from __future__ import annotations
import argparse
import csv
import json
import re
import sqlite3
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DB_PATH = ROOT / "lokvani.db"


def _norm(s):
    """Name normalizer."""
    return re.sub(r"[^a-z0-9]+", "", (s or "").lower())


def _norm_const(name: str) -> str:
    """Constituency normalizer — must match apply_llm_extraction.py's
    _normalize_constituency so we agree on what's a gap."""
    if not name:
        return ""
    s = name.upper().strip()
    for suf in ("(SC)", "(ST)", " SC", " ST"):
        if s.endswith(suf):
            s = s[: -len(suf)].strip()
    s = re.sub(r"[^A-Z0-9]+", "", s)
    _ALIASES = {"NARELA": "NERELA"}
    return _ALIASES.get(s, s).lower()


def find_allowlist(slug_year: str):
    d = ROOT / "data/allowlists"
    p = d / f"{slug_year}.txt"
    if p.exists():
        return p
    m = sorted(d.glob(f"{slug_year}_top*.txt"))
    return m[0] if m else None


def check_pdf(pdf_path: Path) -> str:
    """Return '' if OK, else a failure code."""
    if not pdf_path.exists():
        return "no_pdf"
    if pdf_path.stat().st_size < 1024:
        return "corrupt_pdf"
    with pdf_path.open("rb") as f:
        if not f.read(8).startswith(b"%PDF-"):
            return "corrupt_pdf"
    return ""


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--state", required=True)
    ap.add_argument("--year", type=int, required=True)
    args = ap.parse_args()

    # Slug for cycle-dir lookup. Some states use short forms in their
    # folder name (Jammu and Kashmir → "jk"). Map explicitly.
    SLUG_OVERRIDES = {
        "jammu and kashmir": "jk",
        "jammu & kashmir":   "jk",
    }
    state_lower = args.state.lower()
    slug = SLUG_OVERRIDES.get(state_lower, state_lower.replace(" ", ""))
    slug_year = f"{slug}_{args.year}"

    # Locate paths
    raw_root = ROOT / "data/eci/raw_pdfs"
    cycle_dir = None
    for d in sorted(raw_root.iterdir()):
        if d.name.startswith(slug) and d.name.endswith(str(args.year)):
            cycle_dir = d
            break
    if not cycle_dir:
        raise SystemExit(f"No cycle dir for {args.state} {args.year}")
    raw_dir = cycle_dir / "raw_pdfs" if (cycle_dir / "raw_pdfs").exists() else cycle_dir
    pp_dir = ROOT / "data/eci/for_ai" / f"preprocessed_{slug_year}"
    lx_dir = ROOT / "data/eci/for_ai/llm_extracted" / slug_year

    # Allowlist
    allow_path = find_allowlist(slug_year)
    if not allow_path:
        raise SystemExit(f"No allowlist for {slug_year}")
    allow = [ln.strip() for ln in allow_path.read_text().splitlines()
             if ln.strip() and not ln.startswith("#")]

    # Manifest — for looking up candidate name/const per PDF basename
    mf_by_name = {}
    for line in (cycle_dir / "manifest.jsonl").read_text().splitlines():
        try:
            r = json.loads(line)
        except:
            continue
        p = r.get("pdf_path") or ""
        if p:
            mf_by_name[Path(p).name] = r

    # SQLite: which allowlist candidates have wealth?
    con = sqlite3.connect(str(DB_PATH))
    cur = con.cursor()
    cur.execute("""
        SELECT p.name, c.name, ea.total_assets_inr
        FROM election_appearances ea
        JOIN politicians p ON ea.politician_id = p.id
        JOIN elections e ON ea.election_id = e.id
        JOIN constituencies c ON ea.constituency_id = c.id
        JOIN states s ON c.state_id = s.id
        WHERE s.name = ? AND e.year = ?
    """, (args.state, args.year))
    db_wealth: dict[tuple[str, str], bool] = {}
    for row in cur.fetchall():
        key = (_norm(row[0]), _norm_const(row[1]))
        db_wealth[key] = row[2] is not None

    # Diagnose each allowlist entry that lacks wealth
    gaps = []
    for name in allow:
        row = mf_by_name.get(name)
        if not row:
            gaps.append({
                "pdf": name, "candidate": "", "constituency": "", "party": "",
                "failure": "not_in_manifest",
            })
            continue
        cand_name = row.get("name", "")
        cand_const = row.get("constituency", "")
        cand_party = row.get("party", "")
        key = (_norm(cand_name), _norm_const(cand_const))
        if db_wealth.get(key):
            continue   # has wealth, not a gap
        # Diagnose the failure
        stem = name[:-4]  # strip .pdf
        pdf_path = raw_dir / name
        pp_path  = pp_dir / (stem + ".json")
        lx_path  = lx_dir / (stem + ".json")

        pdf_status = check_pdf(pdf_path)
        if pdf_status:
            failure = pdf_status
        elif not pp_path.exists():
            failure = "no_cloud_vision"
        else:
            try:
                pp_data = json.loads(pp_path.read_text())
                # Cloud Vision marks PDFs it refuses to process with
                # `corrupt: true` in the JSON. Treat these as corrupt PDF
                # (needs refetch), not empty OCR. This catches internal-
                # corruption cases where the PDF passes magic-bytes /
                # size checks but has damaged xref tables etc.
                if pp_data.get("corrupt") or pp_data.get("skipped_corrupt"):
                    failure = "corrupt_pdf"
                    gaps.append({
                        "pdf": name, "candidate": cand_name, "constituency": cand_const,
                        "party": cand_party, "failure": failure,
                    })
                    continue
                text = "\n".join(p.get("text","") for p in pp_data.get("pages",[]))
                ocr_len = len("".join(text.split()))
            except:
                ocr_len = 0
            if ocr_len < 200:
                failure = "empty_ocr"
            elif not lx_path.exists():
                failure = "no_gemini"
            else:
                try:
                    lx_data = json.loads(lx_path.read_text())
                    ext = lx_data.get("extraction") or {}
                    if "_raw" in ext:
                        failure = "gemini_raw"
                    else:
                        n = (ext.get("identity")  or {}).get("name_in_english") or ""
                        c = (ext.get("political") or {}).get("constituency_name") or ""
                        if not n.strip() or not c.strip():
                            failure = "gemini_empty"
                        else:
                            # Check if identity was backfilled (Gemini failed
                            # but our backfill script cosmetically filled
                            # name/const/party from manifest). Also check
                            # if wealth extraction is completely null —
                            # both signal that Gemini didn't actually
                            # extract useful data from the PDF.
                            ident = ext.get("identity") or {}
                            pol   = ext.get("political") or {}
                            am    = ext.get("assets_movable") or {}
                            ai    = ext.get("assets_immovable") or {}
                            li    = ext.get("liabilities") or {}
                            is_backfilled = (
                                ident.get("_name_source") == "manifest_backfill"
                                or pol.get("_const_source") == "manifest_backfill"
                                or pol.get("_party_source") == "manifest_backfill"
                            )
                            no_wealth = all(
                                v in (None, 0) for v in (
                                    am.get("total_movable_assets_inr"),
                                    ai.get("total_immovable_assets_inr"),
                                    li.get("total_liabilities_inr"),
                                )
                            )
                            if is_backfilled and no_wealth:
                                # Gemini never actually extracted for this
                                # file. Need re-OCR (with lang hint) +
                                # re-Gemini.
                                failure = "gemini_no_wealth"
                            else:
                                failure = "gemini_ok_but_apply"
                except:
                    failure = "gemini_unreadable"

        gaps.append({
            "pdf": name, "candidate": cand_name, "constituency": cand_const,
            "party": cand_party, "failure": failure,
        })

    # Summary
    print(f"State: {args.state} {args.year}")
    print(f"Allowlist size: {len(allow)}")
    print(f"Gaps (no wealth in DB): {len(gaps)}\n")
    print("Failure breakdown:")
    for code, n in Counter(g["failure"] for g in gaps).most_common():
        print(f"  {n:>4d}  {code}")

    # Write CSV
    out = ROOT / f"data/reports/gaps_{slug_year}.csv"
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["pdf","candidate","constituency","party","failure"])
        w.writeheader()
        w.writerows(gaps)
    print(f"\nCSV: {out.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
