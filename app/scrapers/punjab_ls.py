"""
Punjab Lok Sabha MP scraper.

Pulls winners for each LS cycle on myneta and filters to Punjab's 13 LS PCs.
The page structure mirrors the assembly-cycle winners list, so we reuse the
same row parser (parse_inr, parse_cases, extract_candidate_id).

LS cycles on myneta:
  - LokSabha2024 (current)
  - LokSabha2019
  - ls2014
  - ls2009
"""
import logging
import re
from dataclasses import dataclass
from typing import Optional

from bs4 import BeautifulSoup

from app.scrapers.myneta_client import fetch
from app.scrapers.punjab import parse_inr, parse_cases, extract_candidate_id

log = logging.getLogger(__name__)

LS_CYCLES = [
    {"year": 2024, "slug": "LokSabha2024"},
    {"year": 2019, "slug": "LokSabha2019"},
    {"year": 2014, "slug": "ls2014"},
    {"year": 2009, "slug": "ls2009"},
]

# Punjab's 13 Lok Sabha parliamentary constituencies. Used to filter the
# all-India winners list to just our state. Matching is case-insensitive
# and ignores common suffixes like (SC).
PUNJAB_LS_PCS = {
    "GURDASPUR", "AMRITSAR", "KHADOOR SAHIB", "KHADUR SAHIB",
    "JALANDHAR", "HOSHIARPUR", "ANANDPUR SAHIB", "LUDHIANA",
    "FATEHGARH SAHIB", "FARIDKOT", "FIROZPUR", "FEROZEPUR",
    "BATHINDA", "SANGRUR", "PATIALA",
}

BASE = "https://myneta.info"


@dataclass
class LSWinnerRow:
    candidate_id: int
    name: str
    constituency: str   # LS parliamentary constituency name
    party: str
    criminal_cases: int
    education: str
    total_assets_inr: int
    total_liabilities_inr: int
    election_slug: str
    detail_url: str


def _normalize_pc(name: str) -> str:
    return re.sub(r"\(SC\)|\(ST\)", "", (name or "")).upper().strip()


def scrape_punjab_ls_winners(election_slug: str) -> list[LSWinnerRow]:
    """Fetch one LS cycle's winners list and filter to Punjab PCs."""
    url = f"{BASE}/{election_slug}/index.php?action=show_winners&sort=default"
    html = fetch(url)
    soup = BeautifulSoup(html, "lxml")

    winners: list[LSWinnerRow] = []

    for table in soup.find_all("table"):
        rows = table.find_all("tr")
        if not rows:
            continue
        header = rows[0].get_text(" ", strip=True).lower()
        if "candidate" not in header or "constituency" not in header:
            continue

        for tr in rows[1:]:
            cells = tr.find_all("td")
            if len(cells) < 8:
                continue
            try:
                # See punjab.py for why we don't just pick the first anchor.
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
                name = name_link.get_text(strip=True) or cells[1].get_text(" ", strip=True)
                cand_id = extract_candidate_id(name_link.get("href", ""))
                if not cand_id or not name:
                    continue
                pc_raw = cells[2].get_text(strip=True)
                pc_norm = _normalize_pc(pc_raw)

                # Skip if this constituency isn't one of Punjab's 13 PCs.
                # Match is permissive: PC name must START with one of ours
                # (handles "AMRITSAR" or "AMRITSAR SAHIB" if myneta varies it).
                matched = any(pc_norm.startswith(p) or p.startswith(pc_norm)
                              for p in PUNJAB_LS_PCS)
                if not matched:
                    continue

                winners.append(LSWinnerRow(
                    candidate_id=cand_id,
                    name=name,
                    constituency=pc_raw,
                    party=cells[3].get_text(strip=True),
                    criminal_cases=parse_cases(cells[4].get_text(strip=True)),
                    education=cells[5].get_text(strip=True),
                    total_assets_inr=parse_inr(cells[6].get_text(" ", strip=True)),
                    total_liabilities_inr=parse_inr(cells[7].get_text(" ", strip=True)),
                    election_slug=election_slug,
                    detail_url=f"{BASE}/{election_slug}/candidate.php?candidate_id={cand_id}",
                ))
            except Exception as e:
                log.warning("Skipping LS row in %s: %s", election_slug, e)
                continue

    log.info("Parsed %d Punjab LS winners from %s", len(winners), election_slug)
    return winners


def scrape_all_punjab_ls() -> dict[int, list[LSWinnerRow]]:
    return {
        cycle["year"]: scrape_punjab_ls_winners(cycle["slug"])
        for cycle in LS_CYCLES
    }
