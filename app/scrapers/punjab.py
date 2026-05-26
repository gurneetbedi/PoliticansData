"""
Punjab MLA scraper.

Pulls winners (current sitting MLAs and historical) from all available
Punjab assembly election cycles on myneta.info:
  - punjab2022, punjab2017, pb2012, pb2007

Also handles bye-election winners listed on the same page.

Parsing approach: myneta uses simple HTML tables. We locate the winners
table by structure (8 columns: Sno, Candidate, Constituency, Party,
Criminal Cases, Education, Total Assets, Liabilities) and parse row by row.
The candidate link gives us the stable myneta candidate_id.
"""
import logging
import re
from dataclasses import dataclass, field
from typing import Optional

from bs4 import BeautifulSoup

from app.scrapers.myneta_client import fetch

log = logging.getLogger(__name__)

# Punjab assembly election cycles with their myneta URL slugs
PUNJAB_CYCLES = [
    {"year": 2022, "slug": "punjab2022"},
    {"year": 2017, "slug": "punjab2017"},
    {"year": 2012, "slug": "pb2012"},
    {"year": 2007, "slug": "pb2007"},
]

BASE = "https://myneta.info"


@dataclass
class WinnerRow:
    """One row from the winners list — the minimal data shown in the table."""
    candidate_id: int
    name: str
    constituency: str
    party: str
    criminal_cases: int
    education: str
    total_assets_inr: int
    total_liabilities_inr: int
    election_slug: str
    detail_url: str
    is_bye_election: bool = False
    bye_election_date: Optional[str] = None


def parse_inr(text: str) -> int:
    """
    Parse a money string like 'Rs 27,28,01,726   ~ 27 Crore+' into rupees as int.
    Falls back to 0 if unparseable. Handles 'Nil' and empty strings.
    """
    if not text:
        return 0
    # Strip everything after the ~ (the human-readable approximation)
    raw = text.split("~")[0]
    # Remove "Rs", spaces, commas
    digits = re.sub(r"[^\d]", "", raw)
    return int(digits) if digits else 0


def parse_cases(text: str) -> int:
    """Criminal case count cell — either '0' or a bolded number like '**3**'."""
    if not text:
        return 0
    m = re.search(r"\d+", text)
    return int(m.group()) if m else 0


def extract_candidate_id(href: str) -> Optional[int]:
    """Pull candidate_id from a URL like '.../candidate.php?candidate_id=274'."""
    m = re.search(r"candidate_id=(\d+)", href or "")
    return int(m.group(1)) if m else None


def scrape_winners(election_slug: str) -> list[WinnerRow]:
    """Fetch the winners list for one Punjab election cycle and parse all rows."""
    url = f"{BASE}/{election_slug}/index.php?action=show_winners&sort=default"
    html = fetch(url)
    soup = BeautifulSoup(html, "lxml")

    winners: list[WinnerRow] = []
    is_bye_section = False

    # myneta has two tables on this page: main winners list, then bye-election winners.
    # We iterate tables and parse rows that match the expected 8-column structure.
    for table in soup.find_all("table"):
        rows = table.find_all("tr")
        if not rows:
            continue
        # Identify by header
        header = rows[0].get_text(" ", strip=True).lower()
        if "candidate" not in header or "constituency" not in header:
            continue

        # Detect "Bye-Elections" section by looking at the preceding heading
        prev = table.find_previous(string=re.compile(r"Bye[- ]?Election", re.I))
        is_bye_section = bool(prev)

        for tr in rows[1:]:
            cells = tr.find_all("td")
            if len(cells) < 8:
                continue
            try:
                # myneta wraps the candidate cell in two anchors: an empty
                # image wrapper followed by the real name link. We must pick
                # the one with non-empty text — picking the first gets an
                # empty name. (Bug fix for missing-name issue.)
                candidate_links = [
                    a for a in cells[1].find_all("a")
                    if "candidate_id=" in (a.get("href", "") or "")
                ]
                if not candidate_links:
                    continue
                name_link = next(
                    (a for a in candidate_links if a.get_text(strip=True)),
                    candidate_links[0],
                )
                name = name_link.get_text(strip=True)
                cand_id = extract_candidate_id(name_link.get("href", ""))
                if not cand_id:
                    continue
                if not name:
                    # Last-ditch: take any non-empty text in the cell
                    name = cells[1].get_text(" ", strip=True)
                if not name:
                    continue  # truly nothing to use

                constituency_text = cells[2].get_text(strip=True)
                bye_date = None
                is_bye = is_bye_section or ":" in constituency_text
                if is_bye and ":" in constituency_text:
                    parts = constituency_text.split(":", 1)
                    constituency_text = parts[0].strip()
                    # "BYE ELECTION ON 13-11-2024"
                    m = re.search(r"(\d{2}-\d{2}-\d{4})", parts[1])
                    bye_date = m.group(1) if m else None

                winners.append(WinnerRow(
                    candidate_id=cand_id,
                    name=name,
                    constituency=constituency_text,
                    party=cells[3].get_text(strip=True),
                    criminal_cases=parse_cases(cells[4].get_text(strip=True)),
                    education=cells[5].get_text(strip=True),
                    total_assets_inr=parse_inr(cells[6].get_text(" ", strip=True)),
                    total_liabilities_inr=parse_inr(cells[7].get_text(" ", strip=True)),
                    election_slug=election_slug,
                    detail_url=f"{BASE}/{election_slug}/candidate.php?candidate_id={cand_id}",
                    is_bye_election=is_bye,
                    bye_election_date=bye_date,
                ))
            except Exception as e:
                log.warning("Skipping row in %s: %s", election_slug, e)
                continue

    log.info("Parsed %d winners from %s", len(winners), election_slug)
    return winners


def scrape_all_punjab() -> dict[int, list[WinnerRow]]:
    """Scrape every available Punjab assembly cycle. Returns {year: [winners]}."""
    return {
        cycle["year"]: scrape_winners(cycle["slug"])
        for cycle in PUNJAB_CYCLES
    }


# ---- Detail page enrichment ---------------------------------------------------

@dataclass
class CandidateDetail:
    """Richer data from an individual candidate affidavit page."""
    candidate_id: int
    age: Optional[int] = None
    profession: Optional[str] = None
    movable_total_inr: int = 0
    immovable_total_inr: int = 0
    serious_cases: int = 0
    cases: list[dict] = field(default_factory=list)
    assets: list[dict] = field(default_factory=list)
    liabilities: list[dict] = field(default_factory=list)


def scrape_candidate_detail(detail_url: str, candidate_id: int) -> CandidateDetail:
    """
    Pull the detailed affidavit page for one candidate.
    myneta's detail pages have several tables — assets, liabilities, cases,
    education, profession. This parser is intentionally permissive — it
    extracts what it can and leaves missing fields empty rather than failing.
    """
    html = fetch(detail_url)
    soup = BeautifulSoup(html, "lxml")

    detail = CandidateDetail(candidate_id=candidate_id)
    text = soup.get_text(" ", strip=True)

    # ---- Age / Profession --------------------------------------------------
    m = re.search(r"(?:Self\s+)?Age\s*:?\s*(\d+)", text)
    if m:
        detail.age = int(m.group(1))

    m = re.search(r"(?:Self\s+)?Profession[s]?\s*:?\s*([^|\n]{2,200}?)(?:Self|Spouse|$)", text)
    if m:
        prof = m.group(1).strip()
        if prof and len(prof) < 200:
            detail.profession = prof[:255]

    # ---- Serious cases count ----------------------------------------------
    m = re.search(r"(\d+)\s*Number of Serious IPC", text, re.I)
    if m:
        detail.serious_cases = int(m.group(1))

    # ---- Asset / Liability totals ----------------------------------------
    # Same overflow guard as pick_value: any number above ₹10,000 Cr is
    # almost certainly a non-money identifier (survey number, plot ID).
    SAFE_TOTAL_CAP = 10_000_000_000_000

    def _parse_total(pattern: str) -> int:
        m = re.search(pattern, text)
        if not m:
            return 0
        v = int(m.group(1).replace(",", ""))
        return v if v <= SAFE_TOTAL_CAP else 0

    detail.movable_total_inr   = _parse_total(r"Total Movable Assets.*?Rs\s*([\d,]+)")
    detail.immovable_total_inr = _parse_total(r"Total Immovable Assets.*?Rs\s*([\d,]+)")

    # ---- Cases ------------------------------------------------------------
    # Cases on the detail page typically appear in their own section. We look
    # for tables whose header mentions IPC / Criminal / Section and capture rows.
    for table in soup.find_all("table"):
        rows = table.find_all("tr")
        if len(rows) < 2:
            continue
        header_text = rows[0].get_text(" ", strip=True).lower()
        if not any(k in header_text for k in ("ipc", "section", "charge")):
            continue
        for tr in rows[1:]:
            cells = [c.get_text(" ", strip=True) for c in tr.find_all("td")]
            if not cells:
                continue
            # Try to identify common columns
            sections = next((c for c in cells if re.search(r"\bIPC\b|\bsection\b", c, re.I)), "")
            charges = next((c for c in cells if len(c) > 20), cells[-1] if cells else "")
            detail.cases.append({
                "ipc_sections": sections.strip()[:255],
                "description":  charges.strip()[:500],
                "status":       "pending",
            })

    # ---- Asset breakdown by category --------------------------------------
    # myneta affidavit asset tables typically have this shape:
    #   [Sr No] [Description]                [Self Value] [Spouse Value] [HUF/Dep] [Total]
    # We need to:
    #   1. Skip header rows ("Sr No", "Description", ...)
    #   2. Skip total/summary rows ("Totals", "Gross Total Value")
    #   3. Treat short alpha codes ("i", "ii", "(a)", "1") as row-number cells
    #      and shift to the next column for the actual description
    #   4. Scan trailing cells for the value (often there's a Total column at end)

    ROW_NUMBER = re.compile(r"^(?:\(?[a-z]\)?|[ivxlcdm]+\.?|\d+\.?)$", re.I)
    HEADER_KEYWORDS = {"sr no", "description", "self", "spouse", "huf",
                       "dependent", "value", "remarks", "self/spouse"}
    SUMMARY_KEYWORDS = {"total", "totals", "gross total", "sum of",
                        "calculated as sum", "grand total"}

    def is_header_row(cells: list[str]) -> bool:
        joined = " ".join(cells).lower()
        return any(k in joined for k in HEADER_KEYWORDS) and len(joined) < 200

    def is_summary_row(text: str) -> bool:
        low = text.lower().strip()
        return any(k in low for k in SUMMARY_KEYWORDS)

    # Maximum sensible rupee value — anything beyond ₹10,000 Cr (10^14 paise/rupees)
    # is almost certainly a survey number, plot ID, or similar non-money identifier
    # that leaked into the value column. SQLite's signed 64-bit INTEGER caps at
    # ~9.2 × 10^18, so we stay well within that.
    MAX_RUPEE_VALUE = 10_000_000_000_000  # ₹10,000 Cr

    def pick_value(cells: list[str], start_idx: int) -> int:
        """
        Find the largest sensible rupee value in the cells from start_idx.
        Prefers numbers preceded by 'Rs'; falls back to fully-numeric cells.
        Discards anything above MAX_RUPEE_VALUE (likely a non-money identifier).
        """
        best = 0
        for c in cells[start_idx:]:
            # Pattern A: "Rs 12,34,567" — most reliable signal of a money value
            candidates = re.findall(r"Rs\.?\s*([\d,]+)", c, re.I)
            # Pattern B: cell is purely digits + commas + whitespace (e.g. "12,34,567")
            if not candidates:
                stripped = c.strip()
                if stripped and re.fullmatch(r"[\d,\s]+", stripped):
                    candidates = [stripped]
            for raw in candidates:
                v = int(raw.replace(",", "").replace(" ", ""))
                if v > MAX_RUPEE_VALUE:
                    continue  # bogus value (survey/account/plot number)
                if v > best:
                    best = v
        return best

    for heading_text, category in [("Movable", "movable"), ("Immovable", "immovable")]:
        heading = soup.find(string=re.compile(rf"\b{heading_text}\s+Assets\b", re.I))
        if not heading:
            continue
        table = heading.find_next("table")
        if not table:
            continue

        for tr in table.find_all("tr"):
            cells = [c.get_text(" ", strip=True) for c in tr.find_all("td")]
            if len(cells) < 2:
                continue

            # Skip header rows
            if is_header_row(cells):
                continue

            first = cells[0].strip()

            # Decide which cell holds the description
            if ROW_NUMBER.match(first) and len(cells) >= 3:
                subcat = cells[1].strip()
                value_start = 2
            elif len(first) <= 2 and len(cells) >= 3:
                # Single-char or 2-char cells like "i" or "1." that the regex missed
                subcat = cells[1].strip()
                value_start = 2
            else:
                subcat = first
                value_start = 1

            # Skip empty / garbage / summary descriptions
            if not subcat or len(subcat) <= 3 or subcat.isdigit() or is_summary_row(subcat):
                continue
            # Skip header-like descriptions (e.g. literal "Description")
            if subcat.lower() in HEADER_KEYWORDS:
                continue

            value = pick_value(cells, value_start)
            if value == 0:
                continue
            # Cap pathological subcat lengths
            if len(subcat) > 120:
                subcat = subcat[:120]

            detail.assets.append({
                "category": category,
                "subcategory": subcat,
                "value_inr": value,
            })

    return detail
