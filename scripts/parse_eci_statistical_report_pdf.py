"""
Convert an ECI Statistical Report "Detailed Results" PDF (Form 20/21)
directly into the ECI-results JSON schema, skipping the fragile
PDF-to-Excel conversion step.

Use this when the ECI page only gives you a PDF, or when a PDF-to-Excel
export lost data. Same output schema as scripts/parse_eci_statistical_report.py,
so downstream loaders (load_eci_results.py, build_top_n_allowlist.py)
work identically.

Requires: pip install pdfplumber
    (pdfplumber uses pdfminer.six under the hood — pure Python, no
     external native deps, works cross-platform).

Usage:
    python scripts/parse_eci_statistical_report_pdf.py \\
        --pdf data/Results/10-Detailed-Results-Gujarat_2022.pdf \\
        --state "Gujarat" \\
        --year 2022 \\
        --state-code S06 \\
        --out data/eci/results/gujarat_2022_eci_results.json
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path


# The Statistical Report tables have these columns (11 total):
#   0 STATE/UT NAME | 1 AC NO. | 2 AC NAME | 3 CANDIDATE NAME
#   4 GENDER        | 5 AGE    | 6 CATEGORY | 7 PARTY
#   8 SYMBOL        | 9 GENERAL | 10 POSTAL | 11 TOTAL
#  12 % VOTES POLLED (over valid) | 13 OVER TOTAL ELECTORS | 14 TOTAL ELECTORS
# Row layout matches parse_eci_statistical_report.py's column indices.
COL_STATE   = 0
COL_AC_NO   = 1
COL_AC_NAME = 2
COL_CAND    = 3
COL_PARTY   = 7
COL_EVM     = 9
COL_POSTAL  = 10
COL_TOTAL   = 11
COL_PCT     = 12


def _norm_name(raw: str | None) -> str:
    if raw is None:
        return ""
    s = str(raw).strip()
    s = re.sub(r"^\s*\d+\s+", "", s)
    return s.strip().upper()


def _to_int(v) -> int | None:
    if v is None or v == "":
        return None
    if isinstance(v, str):
        v = v.strip().replace(",", "")
    try:
        return int(v)
    except (TypeError, ValueError):
        try:
            return int(float(v))
        except (TypeError, ValueError):
            return None


def _to_float(v) -> float | None:
    if v is None or v == "":
        return None
    if isinstance(v, str):
        v = v.strip().replace(",", "").rstrip("%")
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


# Common ECI party abbreviations. If the party+symbol chunk begins with
# one of these, we take it as the whole party string and the rest is symbol.
# Also common full-name party heads for multi-word matching.
KNOWN_PARTY_TOKENS = {
    "BJP", "INC", "AAP", "AAAP", "BSP", "CPI", "CPM", "CPI(M)", "CPI(ML)",
    "NCP", "NCP(SP)", "SP", "SAD", "SAD(B)", "SS", "SHS", "SHS(UBT)",
    "AITC", "TMC", "BJD", "RJD", "JDU", "JD(U)", "JDS", "JD(S)", "LJP",
    "DMK", "AIADMK", "ADMK", "IUML", "MDMK", "DMDK", "PMK", "VCK",
    "TDP", "YSRCP", "YSRC", "JSP", "BRS", "TRS", "AIMIM", "TVK",
    "JKN", "JKNC", "JKPDP", "MNS", "RLD", "NPP", "NPF", "NDPP",
    "SKM", "SDF", "MGP", "AGP", "AIUDF", "IND", "NOTA", "OTH",
    "AISF", "AJUP", "KEC", "KC(M)",
}


def _split_party_symbol(tokens: list[str]) -> tuple[str, str]:
    """Split a `party + symbol` token list into two strings.

    Heuristic:
      1. If the first token is a known ECI party code (BJP, INC, IND, etc.),
         take it as party and the rest as symbol.
      2. Otherwise take the last 1-2 tokens as symbol and the rest as party.
      3. Single-token edge case: it's the party (unknown symbol).
    """
    if not tokens:
        return "", ""
    if len(tokens) == 1:
        return tokens[0], ""

    first = tokens[0].upper()
    if first in KNOWN_PARTY_TOKENS:
        return tokens[0], " ".join(tokens[1:])

    # 2-token case: always split as party+symbol (never 0-token party).
    # Even if the last token looks like a compound-symbol suffix ("book",
    # "farm", etc.), a single party token before it is the only sane split.
    if len(tokens) == 2:
        return tokens[0], tokens[1]

    # Otherwise assume symbol is last 1-2 words, party is the rest.
    # Symbol length: 2 words if any of the last 2 tokens is a known symbol
    # word ("farm", "cart", "box", "pump" etc.), else 1.
    LAST_TWO_WORDS = {"farm", "cart", "box", "pump", "food", "sprayer",
                       "engine", "gas", "cylinder", "cane", "juice", "shop",
                       "seller", "tractor", "truck", "rickshaw", "cycle",
                       "bat", "table", "chair", "kite", "boat", "bell",
                       "camera", "phone", "book"}
    if tokens[-1].lower() in LAST_TWO_WORDS or tokens[-2].lower() in LAST_TWO_WORDS:
        return " ".join(tokens[:-2]), " ".join(tokens[-2:])
    return " ".join(tokens[:-1]), tokens[-1]


def parse_pdf(path: Path) -> list[dict]:
    """Extract candidate rows from a Statistical Report PDF using text-line
    parsing (works even when the PDF has no bordered tables).

    Detects:
      • Constituency headers: `Constituency <N> - <NAME> TOTAL ELECTORS <count>`
      • Candidate data lines: contain `MALE|FEMALE|OTHERS` + 3-4 trailing
        numbers (general votes, postal votes, total votes, %).
      • Multi-line name wrapping: candidate names sometimes span 2-3 lines
        with only "SEX AGE CATEGORY PARTY SYMBOL VOTES..." on the data line
        itself. We collect the preceding text lines and merge them.
      • NOTA rows: recognised by literal "NOTA" token.
    """
    try:
        import pdfplumber
    except ImportError:
        sys.exit("pip install pdfplumber")

    constituencies: dict[int, dict] = {}
    current_ac: int | None = None
    current_ac_name: str = ""
    # Buffer to collect "name continuation" lines BEFORE a data line
    pending_name_parts: list[str] = []
    # Number of post-name lines still allowed to attach to the LAST added
    # candidate. Names sometimes wrap so the surname is on the line AFTER
    # the data line (e.g. "1 Kuldeep / Singh MALE... / Pathania"). We cap
    # this to 1 so runaway symbol/footer text isn't mistaken for name.
    post_name_slots: int = 0

    # Regex to detect a constituency header line
    RE_CONST = re.compile(
        r"Constituency\s+(\d+)\s*-\s*(.+?)\s+TOTAL\s+ELECTORS", re.IGNORECASE)

    # Regex to detect a candidate data line — end-anchored on the 4-number
    # signature (general votes, postal votes, total votes, %). Party and
    # symbol are captured together and split heuristically afterward.
    #
    # `pre` (leading name text) can be empty — a candidate whose name
    # entirely wraps to the line above will have MALE/FEMALE at the start
    # of their data line.
    RE_DATA = re.compile(
        r"^\s*(?P<pre>.*?)"
        r"(?:^|\s)(?P<sex>MALE|FEMALE|OTHERS|NAN)\s+"
        r"(?P<age>\d+)\s+"
        r"(?P<category>[A-Z\-—]+)\s+"
        r"(?P<partysym>.+?)\s+"
        r"(?P<general>[\d,]+)\s+"
        r"(?P<postal>[\d,]+)\s+"
        r"(?P<total>[\d,]+)\s+"
        r"(?P<pct>[\d.]+)\s*$",
        re.IGNORECASE
    )

    # NOTA line: much simpler — "Nota NOTA NOTA <gen> <postal> <total> <pct>"
    RE_NOTA = re.compile(
        r"^\s*(\d+)\s+Nota\s+NOTA\s+NOTA\s+"
        r"(?P<general>[\d,]+)\s+"
        r"(?P<postal>[\d,]+)\s+"
        r"(?P<total>[\d,]+)\s+"
        r"(?P<pct>[\d.]+)\s*$",
        re.IGNORECASE
    )

    # Known-party symbol contamination — a few parties have multi-word
    # symbols that wrap into the candidate name column during PDF text
    # extraction, and cannot be perfectly untangled without positional
    # parsing. Strip these trailing tokens when they leak into the name.
    SYMBOL_TAILS = {
        "AITC":  ["FLOWERS", "AND GRASS", "FLOWERS AND GRASS"],
        "JD(S)": ["A LADY", "FARMER", "CARRYING", "PADDY", "HER HEAD"],
        "UPJP":  ["AUTO-", "RICKSHAW", "AUTO- RICKSHAW", "AUTO"],
    }

    def _strip_symbol_contamination(name: str, party: str) -> str:
        tails = SYMBOL_TAILS.get(party.upper(), [])
        changed = True
        while changed:
            changed = False
            for t in tails:
                suffix = " " + t
                if name.endswith(suffix):
                    name = name[:-len(suffix)].strip()
                    changed = True
        return name

    def _add_candidate(name: str, party: str, evm, postal, total, pct,
                       gender: str = "", age: int | None = None,
                       category: str = ""):
        if current_ac is None:
            return
        name = _strip_symbol_contamination(name, party)
        c = constituencies.setdefault(current_ac, {
            "number": current_ac,
            "name": current_ac_name.upper(),
            "candidates": [],
        })
        c["candidates"].append({
            "name":         name,
            "party":        party,
            "gender":       gender.upper() if gender else "",
            "age":          age,
            "category":     category.upper() if category else "",
            "evm_votes":    evm,
            "postal_votes": postal,
            "total_votes":  total,
            "vote_pct":     pct,
        })

    with pdfplumber.open(path) as pdf:
        for page_num, page in enumerate(pdf.pages, 1):
            text = page.extract_text() or ""
            for raw_line in text.splitlines():
                line = raw_line.strip()
                if not line:
                    continue

                # Constituency header?
                m = RE_CONST.search(line)
                if m:
                    current_ac = int(m.group(1))
                    current_ac_name = m.group(2).strip()
                    pending_name_parts = []
                    post_name_slots = 0
                    continue

                if current_ac is None:
                    continue

                # NOTA line?
                m = RE_NOTA.match(line)
                if m:
                    pending_name_parts = []
                    post_name_slots = 0  # NOTA has no name to continue
                    _add_candidate(
                        name="NOTA", party="NOTA",
                        evm=_to_int(m.group("general")),
                        postal=_to_int(m.group("postal")),
                        total=_to_int(m.group("total")),
                        pct=_to_float(m.group("pct")),
                    )
                    continue

                # Candidate data line?
                m = RE_DATA.match(line)
                if m:
                    # Compose full name from any pending name-continuation
                    # lines + the "pre" part of the data line (text before
                    # the MALE/FEMALE token).
                    pre = m.group("pre").strip()
                    # Strip leading rank number "1 Jadeja..."
                    pre = re.sub(r"^\s*\d+\s+", "", pre)
                    name_parts = pending_name_parts + ([pre] if pre else [])
                    full_name = " ".join(p.strip() for p in name_parts if p.strip())
                    full_name = full_name.upper() or "UNKNOWN"

                    # Split party+symbol. ECI parties are typically short
                    # abbreviations (BJP, INC, IND, AAAP, CPI(M)) or
                    # 2-3 word full names ("Bahujan Samaj Party"). The
                    # symbol is 1-2 words (Lotus, Hand, Elephant, Coconut
                    # farm). Heuristic: known-party codes at start take
                    # priority; otherwise everything except the last 1-2
                    # tokens is party.
                    partysym = m.group("partysym").strip()
                    tokens = partysym.split()
                    party_str, symbol_str = _split_party_symbol(tokens)

                    _add_candidate(
                        name=full_name,
                        party=party_str.upper(),
                        gender=m.group("sex") or "",
                        age=_to_int(m.group("age")),
                        category=m.group("category") or "",
                        evm=_to_int(m.group("general")),
                        postal=_to_int(m.group("postal")),
                        total=_to_int(m.group("total")),
                        pct=_to_float(m.group("pct")),
                    )
                    pending_name_parts = []
                    # After adding a candidate, allow ONE post-name line
                    # (surname wrap like "Pathania" after "Singh MALE ...").
                    post_name_slots = 1
                    continue

                # Otherwise: this is a plain text line. Three cases:
                #  (a) starts with "N " (rank marker) → head of NEXT candidate's
                #      multi-line name → put in pending, reset post-slot.
                #  (b) doesn't start with a digit, first char UPPER, we still
                #      have a post-name slot → surname wrap of just-added
                #      candidate → append to last candidate's name.
                #  (c) doesn't start with a digit AND pending is non-empty →
                #      continuation of the next candidate's build-up →
                #      append to pending.
                #  (d) anything else (lowercase joiner like "and grass",
                #      or free-floating symbol tail) → DISCARD.

                # Case (a): rank-marker line
                m_rank = re.match(r"^\s*\d+\s+(.+)$", line)
                if m_rank:
                    rest = m_rank.group(1).strip()
                    if rest and not re.search(r"\d", rest) and len(rest) < 60:
                        pending_name_parts.append(rest)
                        post_name_slots = 0  # new candidate starts, close prev
                    continue

                # From here on: line does NOT start with a digit.
                if not line or re.search(r"\d", line) or len(line) >= 60:
                    continue

                # Real Indian names have ALL words starting with an uppercase
                # letter ("Pathania", "Singh Jaryal", "Shylla"). Symbol tails
                # like "A lady", "and grass", "farmer" fail this check:
                # either a word starts with lowercase, or a single-letter
                # word ("A") is present. Reject anything that fails.
                words = line.split()
                looks_like_name = (
                    bool(words)
                    and all(w[0].isupper() for w in words)
                    and all(len(w) > 1 for w in words)           # rejects "A lady"
                    and all(not w.endswith("-") for w in words)  # rejects "Auto-" wrap
                )
                if not looks_like_name:
                    continue

                # Case (b): post-name append to last candidate
                if (post_name_slots > 0
                        and current_ac in constituencies
                        and constituencies[current_ac]["candidates"]):
                    last = constituencies[current_ac]["candidates"][-1]
                    # Skip if the last candidate was NOTA
                    if last["name"] != "NOTA":
                        last["name"] = (last["name"] + " " + line.upper()).strip()
                        post_name_slots -= 1
                        continue

                # Case (c): continuation of a pending pre-name build-up
                if pending_name_parts:
                    pending_name_parts.append(line)
                    continue

                # Case (d): discard silently (symbol tails, headers, etc.)

            if page_num % 25 == 0:
                print(f"  ...page {page_num}/{len(pdf.pages)}: "
                      f"{len(constituencies)} constituencies so far",
                      file=sys.stderr)

    # Rank & mark winner
    out = []
    for num in sorted(constituencies):
        c = constituencies[num]
        c["candidates"].sort(key=lambda x: -(x["total_votes"] or 0))
        for i, cand in enumerate(c["candidates"], 1):
            cand["rank"] = i
            cand["won"] = (i == 1)
        out.append(c)
    return out


def _normalize_state(name: str) -> str:
    _SPECIAL = {
        "jammu and kashmir": "Jammu and Kashmir", "jammuandkashmir": "Jammu and Kashmir",
        "jk": "Jammu and Kashmir",
        "andhra pradesh": "Andhra Pradesh", "andhrapradesh": "Andhra Pradesh",
        "arunachal pradesh": "Arunachal Pradesh", "arunachalpradesh": "Arunachal Pradesh",
        "himachal pradesh": "Himachal Pradesh", "himachalpradesh": "Himachal Pradesh",
        "madhya pradesh": "Madhya Pradesh", "madhyapradesh": "Madhya Pradesh",
        "tamil nadu": "Tamil Nadu", "tamilnadu": "Tamil Nadu",
        "uttar pradesh": "Uttar Pradesh", "uttarpradesh": "Uttar Pradesh",
        "west bengal": "West Bengal", "westbengal": "West Bengal",
    }
    lc = name.strip().lower()
    return _SPECIAL.get(lc, name.strip().title())


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                  formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--pdf", required=True,
                    help="Path to the '10-Detailed-Results.pdf' from eci.gov.in")
    ap.add_argument("--state", required=True,
                    help='State name, e.g. "Gujarat" or "gujarat"')
    ap.add_argument("--year", type=int, required=True)
    ap.add_argument("--state-code", required=True,
                    help="ECI state code (S06 = Gujarat, S12 = MP, etc.)")
    ap.add_argument("--out", required=True,
                    help="Output JSON path")
    args = ap.parse_args()

    args.state = _normalize_state(args.state)

    pdf_path = Path(args.pdf)
    if not pdf_path.exists():
        sys.exit(f"PDF not found: {pdf_path}")

    print(f"→ Parsing {pdf_path.name} ...", file=sys.stderr)
    constituencies = parse_pdf(pdf_path)
    total_cands = sum(len(c["candidates"]) for c in constituencies)
    print(f"  {len(constituencies)} constituencies, {total_cands} candidates",
          file=sys.stderr)

    if not constituencies:
        sys.exit("No constituencies parsed — check PDF format. Some scans need OCR first.")

    payload = {
        "state":         args.state,
        "year":          args.year,
        "state_code":    args.state_code,
        "source":        f"ECI Statistical Report - Detailed Results ({pdf_path.name})",
        "assembly_size": len(constituencies),
        "constituencies": constituencies,
    }

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False))
    print(f"→ Wrote {out_path}", file=sys.stderr)


if __name__ == "__main__":
    main()
