"""
Parse an ECI Form-26 affidavit PDF into a structured JSON record.

Pipeline:
  PDF -> pdftoppm (PNG per page) -> Tesseract OCR -> regex extraction

Form 26 has two key sections (per Conduct of Election Rules 1961):
  PART A — full disclosures (5+ pages of tables: cases, assets, liabilities, etc.)
  PART B — "ABSTRACT" summary on a single page with the headline numbers
           we actually display on the site.

The parser anchors on PART B for the headline fields (total pending cases,
movable assets total, immovable assets total, etc.) and only falls back to
PART A's detail tables when a value can't be recovered from the abstract.

Output JSON shape mirrors the existing election_appearances + assets +
criminal_cases tables so downstream ingestion is straightforward.

Usage:
    python scripts/parse_eci_affidavit.py Affadivit/Affidavit-1780926334.pdf
    python scripts/parse_eci_affidavit.py Affadivit/Affidavit-1780926334.pdf --keep-images
"""
from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass, asdict, field
from pathlib import Path


# ---------------------------------------------------------------------------
# System-dependency check — pdftoppm + tesseract MUST be on PATH
# ---------------------------------------------------------------------------

class MissingDependencyError(RuntimeError):
    """Raised when an OS-level tool the parser depends on isn't installed.

    We raise this loudly instead of letting subprocess.run fail with a
    FileNotFoundError, because callers (like reconcile_eci_vs_db.py and
    dedup_affidavits.py) catch generic exceptions and produce silent
    no-op output — leading to the 'all 126 PDFs parse_fail' result we
    saw on the first Delhi 2025 reconciliation run.
    """


_REQUIRED_TOOLS = ("pdftoppm", "tesseract")
_DEPS_VERIFIED = False


def _check_dependencies() -> None:
    """Verify pdftoppm + tesseract are on PATH. Cached after first call."""
    global _DEPS_VERIFIED
    if _DEPS_VERIFIED:
        return
    missing = [t for t in _REQUIRED_TOOLS if shutil.which(t) is None]
    if missing:
        raise MissingDependencyError(
            f"Required tool(s) not on PATH: {', '.join(missing)}.\n"
            f"  Install with:\n"
            f"    macOS:  brew install poppler tesseract\n"
            f"    Ubuntu: sudo apt-get install poppler-utils tesseract-ocr\n"
            f"  Then re-run."
        )
    _DEPS_VERIFIED = True


# ---------------------------------------------------------------------------
# OCR pipeline
# ---------------------------------------------------------------------------

def pdf_to_pages(pdf_path: Path, work_dir: Path, dpi: int = 300) -> list[Path]:
    """Render every page of the PDF to a PNG via pdftoppm. Returns sorted list."""
    _check_dependencies()
    out_prefix = work_dir / "page"
    subprocess.run(
        ["pdftoppm", "-r", str(dpi), str(pdf_path), str(out_prefix), "-png"],
        check=True, capture_output=True,
    )
    return sorted(work_dir.glob("page-*.png"))


def ocr_page(png_path: Path, lang: str = "eng", psm: int = 6) -> str:
    """Run Tesseract on a PNG and return raw text."""
    _check_dependencies()
    res = subprocess.run(
        ["tesseract", str(png_path), "stdout", "-l", lang, "--psm", str(psm)],
        check=True, capture_output=True, text=True,
    )
    return res.stdout


# ---------------------------------------------------------------------------
# Field extraction
# ---------------------------------------------------------------------------

# Match Indian-format rupee values, optionally with comma grouping.
# Catches: "Rs. 46,849/-"  "Rs.2,81,404/-"  "Rs. 8,00,000"  "1,00,89,655/-"
RUPEE_RE = re.compile(
    r"(?:Rs\.?\s*)?([\d,]+(?:\.\d+)?)\s*/?-?",
    re.IGNORECASE,
)

# Normalise an Indian-formatted number to int paise-free
def parse_indian_int(s: str) -> int | None:
    if not s:
        return None
    # Strip thousand separators and Indian lakh/crore commas
    cleaned = re.sub(r"[^\d.]", "", s.replace(",", ""))
    if not cleaned:
        return None
    try:
        return int(float(cleaned))
    except ValueError:
        return None


@dataclass
class CriminalCase:
    fir_no: str = ""
    case_no: str = ""
    court: str = ""
    ipc_sections: str = ""
    description: str = ""
    charges_framed: bool | None = None
    charges_framed_date: str = ""


@dataclass
class ParsedAffidavit:
    # Identity (Part A header)
    name: str = ""
    father_name: str = ""
    age: int | None = None
    address: str = ""
    party: str = ""
    constituency: str = ""
    state: str = ""
    phone: str = ""
    email: str = ""

    # Part B Abstract — the headline numbers we display on the site
    total_pending_criminal_cases: int | None = None
    total_convictions: int | None = None
    movable_assets_self_inr: int | None = None
    movable_assets_spouse_inr: int | None = None
    immovable_assets_self_inr: int | None = None
    immovable_assets_spouse_inr: int | None = None

    # Detail rows from Part A (only filled if Part A parsing succeeds)
    pending_criminal_cases: list[CriminalCase] = field(default_factory=list)

    # Provenance + confidence
    source_pdf: str = ""
    pages_ocrd: int = 0
    part_a_page_index: int | None = None
    part_b_page_index: int | None = None
    confidence: dict = field(default_factory=dict)
    raw_text_snippets: dict = field(default_factory=dict)


# ---------------------- Part A: identity header -----------------------------
# These regexes are forgiving of common Tesseract artifacts on Form-26 scans:
#   - "I, NAME" / "L, NAME" (Tesseract confuses I and L)
#   - "setupby" / "set up by" (variable spacing)
#   - "son/daughterAvife" (OCR renders "/wife" as "Avife")
#   - "AC-40,.NEW_DELHI" (extra periods, underscores instead of spaces)
#   - "PARTA" instead of "PART A" (lost spacing)

PART_A_PARTY_RE = re.compile(
    # Allow optional spaces in "set up by" since OCR sometimes drops them.
    r"set\s*up\s*by\s+([A-Z][A-Z &\.\-/]+?)(?:\s*\(|\*+|$|\n)",
    re.MULTILINE,
)
PART_A_NAME_RE = re.compile(
    # Captures "<NAME> son/daughter/wife of <FATHER>". Accepts OCR variants:
    #   - "daughter/wife"      (clean OCR)
    #   - "daughterAvife"      (slash misread as A)
    #   - "daughter\\wife"     (backslash artifact)
    #   - "daughtervife"       (slash dropped entirely)
    # The leading I/L ambiguity is fixed in post-processing.
    r"\b([A-Z][A-Z\s\.,]{2,40})\s+\*+\s*son/?\s*daughter[/\\Av]*\s*[wWv]?ife\s+of\s+([A-Z][A-Z\s\.]{3,40})",
)
PART_A_AGE_RE = re.compile(
    # Tesseract often misreads "5" as "§" in this Devanagari-adjacent context.
    r"[Aa]ged\s+([\d§]{1,3})\s+years",
)
PART_A_CONSTITUENCY_RE = re.compile(
    # OCR sometimes inserts ".", "_", or commas between AC code and constituency
    # name (e.g. "AC-40,.NEW_DELHI"). Accept all printable separators.
    r"FROM\s+([A-Z0-9\-,\._\s]+?)\s+CONSTITUENCY",
)
PART_A_ADDRESS_RE = re.compile(
    r"resident\s+of\s+(.+?)(?:\(mention|\n\n|$)",
    re.IGNORECASE | re.DOTALL,
)
PART_A_PHONE_RE = re.compile(r"telephone\s+number\(s\)\s+is/?are\s+([\d ,/\-+]+?)\s+and")
PART_A_EMAIL_RE = re.compile(r"e-?mail\s+id[^:]*?is\s+([\w\.\-+@]+@[\w\.\-]+)")


def find_part_a_page(pages_text: list[str]) -> int | None:
    """Find the Part A identity-header page.
    Accepts both 'PART A' / 'PARTA' (OCR-glued) variants. Skips eStamp paper
    pages that mention 'Affidavit' in cover-page metadata — those have
    'Description of Document' but not the actual Form 26 header text.
    """
    for i, txt in enumerate(pages_text):
        up = txt.upper().replace(" ", "")
        # eStamp paper cover pages have "Description of Document: ... Affidavit"
        # but not the Form 26 header. Reject those.
        if "DESCRIPTIONOFDOCUMENT" in up:
            continue
        if "PARTA" in up or "AFFIDAVITTOBEFILED" in up:
            return i
    return None


def parse_part_a(page_text: str, parsed: ParsedAffidavit) -> dict:
    """Pull name, party, constituency, etc. from the Part A header page."""
    conf = {}

    if m := PART_A_PARTY_RE.search(page_text):
        # Strip the trailing parenthetical / asterisk junk from OCR
        party = m.group(1).strip(" .,-/")
        # Sometimes OCR captures the asterisk note as part of the name
        party = re.split(r"\s{2,}|\*\*", party)[0].strip()
        parsed.party = party
        conf["party"] = "high"
    if m := PART_A_NAME_RE.search(page_text):
        # The regex captures "<name> son/daughter/wife of <father>".
        # OCR contaminants seen in real samples:
        #   - "PARTA\nLARVIND KEJRIWAL"   (Part A header leaked into capture)
        #   - "I, ARVIND KEJRIWAL"        (leading "I, ")
        #   - "LARVIND KEJRIWAL"          (OCR confused I→L and dropped comma)
        name = m.group(1).strip()
        # Drop everything before the last newline (kills "PARTA\n" etc.)
        if "\n" in name:
            name = name.split("\n")[-1].strip()
        # Strip a leading "I, " / "L, " / single I/L glued to first capital
        name = re.sub(r"^[IL][,\s]+", "", name)
        if len(name) > 2 and name[0] in "IL" and name[1:].lstrip()[0].isupper() \
           and not name.startswith(("INDIRA", "ISHWAR", "ILA", "INDU", "LATA", "LAL")):
            # Heuristic: if it starts with I/L followed by another capital and
            # the rest doesn't look like a known I/L name, drop the first char.
            name = name[1:].lstrip()
        parsed.name = name

        # Father name sometimes picks up a trailing artifact letter ("\nA")
        father = m.group(2).strip()
        father = re.sub(r"\s*\n\s*[A-Z]\s*$", "", father)
        parsed.father_name = father.strip()
        conf["name"] = "high"
    if m := PART_A_AGE_RE.search(page_text):
        # Normalise OCR artifacts: § often represents 5
        digit_str = m.group(1).replace("§", "5")
        try:
            parsed.age = int(digit_str)
            conf["age"] = "high"
        except ValueError:
            pass
    if m := PART_A_CONSTITUENCY_RE.search(page_text):
        # OCR contaminants: ".NEW_DELHI" → "NEW DELHI"
        cons = m.group(1).strip(" .,")
        cons = cons.replace("_", " ").replace(".", " ")
        cons = re.sub(r"\s{2,}", " ", cons)
        parsed.constituency = cons.strip()
        conf["constituency"] = "high"
    if m := PART_A_ADDRESS_RE.search(page_text):
        addr = m.group(1).replace("\n", " ").strip(" ,")
        parsed.address = re.sub(r"\s{2,}", " ", addr)
        conf["address"] = "medium"
    if m := PART_A_PHONE_RE.search(page_text):
        parsed.phone = m.group(1).strip()
        conf["phone"] = "medium"
    if m := PART_A_EMAIL_RE.search(page_text):
        parsed.email = m.group(1).strip()
        conf["email"] = "high"

    return conf


# ---------------------- Part B: abstract / summary --------------------------

PART_B_HEADER_RE = re.compile(r"PART\s*-?\s*B", re.IGNORECASE)
PART_B_ABSTRACT_RE = re.compile(r"ABSTRACT", re.IGNORECASE)

# In Part B, "Total number of pending criminal cases" is in a table cell.
# Tesseract often linearises the cell so the digit appears IMMEDIATELY after
# "criminal" with "cases" trailing. Also accept curly-brace / pipe artifacts
# from cell boundaries that Tesseract drops in.
PB_PENDING_RE = re.compile(
    # OCR sometimes garbles the bridge between "criminal" and the number with
    # square brackets, colons, parens, or other punctuation. Accept any
    # non-digit non-newline noise (including the word "cases" if it appears
    # before the count).
    r"[Tt]otal\s+number\s+of\s+pending\s+criminal[^\d\n]{0,40}?(\d+)"
    # Optional parenthetical word form (SEVEN) used for validation.
    r"\s*(?:\(([A-Z]+)\))?",
)
# OCR linearises this table the same way: the count sits BEFORE "convicted".
PB_CONVICTIONS_RE = re.compile(
    r"[Tt]otal\s+[Nn]umber\s+of\s+cases\s+in\s+which\s+"
    r"([\d]+|NIL|IL|NL)\s+convicted",
)
# Asset totals — the Movable row reads in OCR as:
#   "f Moveable IRs. IRs. INOT INOT OT NOT
#    Assets (Total 46,849/- |1,00,89,655/JAPPLICABLE ..."
# Look for "Moveable...Assets (Total" followed by two ₹ values.
#
# IMPORTANT: at lower DPI (and on some scans even at 300 DPI), Tesseract
# inserts whitespace INSIDE the number cell — "4979105" gets rendered as
# "4979 105" or even "4 979 105". The number group below allows internal
# whitespace AFTER an initial multi-digit anchor; we strip whitespace via
# _normalise_rs_number() before parsing. The "at-least-4-digits anchor"
# is important — without it the regex matches sequence numbers like "1 1".
#
# Pattern: "1234[ 5][ 67]..." — one chunk of 4+ digits, then optional
# whitespace-separated additional digit chunks.
PB_NUMBER_GROUP = r"(\d{4,}(?:[,\s]+\d+)*)"
PB_MOVABLE_RE = re.compile(
    r"M[oa]vea?ble[^A-Za-z]+(?:Assets\s*\(Total)?"
    r".*?" + PB_NUMBER_GROUP + r"\s*/?-?"
    r"\s*[|\\/]?\s*" + PB_NUMBER_GROUP,
    re.IGNORECASE | re.DOTALL,
)
PB_IMMOVABLE_PURCHASE_RE = re.compile(
    # Fallback only — Part B's "Purchase Price of self-acquired" row.
    # Used when no current-market-value row is found.
    r"Purchase\s+Price\s+of\b.*?self-acquired"
    r".*?" + PB_NUMBER_GROUP + r"\s*/?-?\s*\{?\s*" + PB_NUMBER_GROUP,
    re.IGNORECASE | re.DOTALL,
)

# Found on the immovable-assets detail page (typically ~page 16), not Part B.
# This is the row ADR uses for headline totals — matches their numbers within
# ~0.05% per the Kejriwal reconciliation.
#
# IMPORTANT: in OCR output, the table linearises so the VALUES appear ABOVE
# the row label, not after it. Match Rs.<num> Rs.<num> on one line, then a
# line break, then "(vi) Total of Current market" on the next.
# Tesseract sometimes OCRs "Current" as "Surrent" / "Cunent".
IMMOVABLE_MARKET_VALUE_RE = re.compile(
    r"(?:Rs\.?\s*)?" + PB_NUMBER_GROUP + r"\s*/?-?\s*"
    r"\|?\s*(?:Rs\.?\s*)?" + PB_NUMBER_GROUP + r"\s*/?-?"
    r"[^\n]*\n[^\n]*?Total\s+of\s+(?:[CScG]u[rn]rent)\s+market",
    re.IGNORECASE,
)


def _normalise_rs_number(raw: str) -> str:
    """Strip OCR-injected whitespace from inside an Indian-format number.
    '4979 105' or '4 979 105' or '1,00, 89,655' → all collapse to clean digits."""
    if raw is None:
        return ""
    # Keep digits, commas, periods; drop all whitespace inside the run.
    return re.sub(r"\s+", "", raw).strip(" ,.")


def find_part_b_page(pages_text: list[str]) -> int | None:
    """Find the page whose text contains the Part B abstract."""
    for i, txt in enumerate(pages_text):
        # Need both markers — Part B begins with "PART-B" and "ABSTRACT" follows
        if PART_B_HEADER_RE.search(txt) and PART_B_ABSTRACT_RE.search(txt):
            return i
    # Fallback: just look for the abstract header alone
    for i, txt in enumerate(pages_text):
        if PART_B_ABSTRACT_RE.search(txt):
            return i
    return None


def parse_part_b(page_text: str, parsed: ParsedAffidavit) -> dict:
    """Pull the headline numbers from the Part B Abstract page."""
    conf = {}

    if m := PB_PENDING_RE.search(page_text):
        numeric = int(m.group(1))
        # OCR sometimes reads "07" as "97" — cross-check with the spelled-out
        # word in parentheses if present. "(SEVEN)" wins over digit 97.
        word = (m.group(2) or "").upper()
        word_map = {
            "ZERO": 0, "NIL": 0, "ONE": 1, "TWO": 2, "THREE": 3, "FOUR": 4,
            "FIVE": 5, "SIX": 6, "SEVEN": 7, "EIGHT": 8, "NINE": 9, "TEN": 10,
            "ELEVEN": 11, "TWELVE": 12, "THIRTEEN": 13, "FOURTEEN": 14,
            "FIFTEEN": 15, "SIXTEEN": 16, "SEVENTEEN": 17, "EIGHTEEN": 18,
            "NINETEEN": 19, "TWENTY": 20,
        }
        if word in word_map and word_map[word] != numeric:
            # The word form is the ground truth — used by the candidate
            # who wrote the affidavit; the digit is OCR-derived.
            parsed.total_pending_criminal_cases = word_map[word]
            conf["pending_cases"] = "medium"  # word wins, digit was wrong
        else:
            parsed.total_pending_criminal_cases = numeric
            conf["pending_cases"] = "high"
    if m := PB_CONVICTIONS_RE.search(page_text):
        raw = m.group(1)
        # OCR confuses "NIL" with "IL" and "NL"
        parsed.total_convictions = 0 if raw.upper() in {"NIL", "IL", "NL"} else int(raw)
        conf["convictions"] = "high" if raw.isdigit() else "medium"
    if m := PB_MOVABLE_RE.search(page_text):
        parsed.movable_assets_self_inr = parse_indian_int(_normalise_rs_number(m.group(1)))
        parsed.movable_assets_spouse_inr = parse_indian_int(_normalise_rs_number(m.group(2)))
        conf["movable"] = "high"
    # Part B immovable row only has PURCHASE PRICE. We'll override with the
    # current-market-value row by scanning all pages — see scan_all_pages_*().
    if m := PB_IMMOVABLE_PURCHASE_RE.search(page_text):
        parsed.immovable_assets_self_inr = parse_indian_int(_normalise_rs_number(m.group(1)))
        parsed.immovable_assets_spouse_inr = parse_indian_int(_normalise_rs_number(m.group(2)))
        conf["immovable"] = "low"   # purchase price — replaced if market value found

    return conf


def scan_all_pages_for_market_value(pages_text: list[str], parsed: ParsedAffidavit) -> dict:
    """
    Scan every page for the "Total of Current market value" row. When found,
    this OVERRIDES the Part B purchase-price values because that's the column
    ADR (and journalism) cite as the headline asset figure.
    """
    conf = {}
    for txt in pages_text:
        if m := IMMOVABLE_MARKET_VALUE_RE.search(txt):
            self_val = parse_indian_int(_normalise_rs_number(m.group(1)))
            spouse_val = parse_indian_int(_normalise_rs_number(m.group(2)))
            # Sanity check: market values are typically 5x+ purchase prices
            # for real estate. Skip if the values look like noise (< Rs 10k).
            if self_val and self_val >= 10_000:
                parsed.immovable_assets_self_inr = self_val
            if spouse_val and spouse_val >= 10_000:
                parsed.immovable_assets_spouse_inr = spouse_val
            conf["immovable_market"] = "high"
            break
    return conf


# ---------------------- Driver ----------------------------------------------

def parse_pdf(pdf_path: Path, work_dir: Path | None = None,
              keep_images: bool = False) -> ParsedAffidavit:
    """Full pipeline. Returns a ParsedAffidavit.

    Strategy: render every page once at 300 DPI, OCR with PSM 6 (uniform
    block) by default. If we can't locate Part A or Part B on the PSM 6
    output, re-OCR with PSM 4 (single column) on the candidate pages —
    table-heavy abstracts sometimes linearise better in single-column mode.
    """
    with tempfile.TemporaryDirectory() as tmp:
        wd = Path(work_dir) if work_dir else Path(tmp)
        wd.mkdir(parents=True, exist_ok=True)

        pages = pdf_to_pages(pdf_path, wd)
        pages_text = [ocr_page(p) for p in pages]

        result = ParsedAffidavit(
            source_pdf=str(pdf_path),
            pages_ocrd=len(pages_text),
        )

        # Identity from Part A — try PSM 6 first, fall back to PSM 4 if no hit.
        idx_a = find_part_a_page(pages_text)
        if idx_a is None and pages:
            # Re-OCR the front pages (1-5) with PSM 4 and retry.
            for p_idx in range(min(5, len(pages))):
                alt = ocr_page(pages[p_idx], psm=4)
                if find_part_a_page([alt]) is not None:
                    pages_text[p_idx] = alt
            idx_a = find_part_a_page(pages_text)
        if idx_a is not None:
            result.part_a_page_index = idx_a
            ca = parse_part_a(pages_text[idx_a], result)
            result.confidence.update(ca)
            result.raw_text_snippets["part_a"] = pages_text[idx_a][:800]

        # Headline numbers from Part B — same PSM-fallback pattern.
        idx_b = find_part_b_page(pages_text)
        if idx_b is None and pages:
            # Part B usually sits in the back half; re-OCR every page with
            # PSM 4 only if we couldn't find it at all.
            for p_idx in range(len(pages)):
                alt = ocr_page(pages[p_idx], psm=4)
                if find_part_b_page([alt]) is not None:
                    pages_text[p_idx] = alt
                    break
            idx_b = find_part_b_page(pages_text)
        if idx_b is not None:
            result.part_b_page_index = idx_b
            cb = parse_part_b(pages_text[idx_b], result)
            result.confidence.update(cb)
            result.raw_text_snippets["part_b"] = pages_text[idx_b][:1500]

        # Override immovable assets with current market value if available.
        # Anchored on a row found on the detail page (~page 16), not Part B.
        cm = scan_all_pages_for_market_value(pages_text, result)
        result.confidence.update(cm)

        if keep_images:
            print(f"  (pages kept under {wd})", file=sys.stderr)

        return result


def to_dict(parsed: ParsedAffidavit) -> dict:
    """Convert dataclass to dict, handling nested dataclasses."""
    return asdict(parsed)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("pdf", help="Path to the ECI affidavit PDF")
    ap.add_argument("--keep-images", action="store_true",
                    help="Keep intermediate PNG renders for debugging")
    ap.add_argument("--out", help="Path to write JSON output (defaults to stdout)")
    args = ap.parse_args()

    pdf_path = Path(args.pdf).resolve()
    if not pdf_path.exists():
        sys.exit(f"PDF not found: {pdf_path}")

    work_dir = Path(tempfile.mkdtemp(prefix="eci_")) if args.keep_images else None
    parsed = parse_pdf(pdf_path, work_dir=work_dir, keep_images=args.keep_images)
    output = json.dumps(to_dict(parsed), indent=2, ensure_ascii=False)

    if args.out:
        Path(args.out).write_text(output)
        print(f"Wrote {args.out}", file=sys.stderr)
    else:
        print(output)


if __name__ == "__main__":
    main()
