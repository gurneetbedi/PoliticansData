"""
Scrapers for the small-state batch (Puducherry, Mizoram, Manipur, Meghalaya,
Nagaland, Tripura, Arunachal Pradesh, Himachal Pradesh, Uttarakhand).

Single file rather than nine near-identical 30-line modules because every one
of these states uses the same myneta table layout — the only thing that varies
is the cycle list, which already lives in app/states.py. Each state exposes
three public names that mirror the existing per-state convention used by
goa.py / sikkim.py / delhi.py:

    scrape_all_<state>()            → dict[year] → list of WinnerRow
    scrape_all_<state>_candidates() → dict[year] → list of WinnerRow
    <STATE>_CYCLES                  → list of cycle dicts from app.states

So ingest.py can import them with the same syntax as the older state modules
and nothing else in the codebase has to change.
"""
import logging
from app.scrapers.punjab import scrape_winners, scrape_all_candidates, WinnerRow  # noqa: F401
from app.states import ALL_STATES

log = logging.getLogger(__name__)


def _winners_factory(state_key: str):
    cycles = ALL_STATES[state_key].assembly_cycles
    def _scrape():
        return {c["year"]: scrape_winners(c["slug"]) for c in cycles}
    return _scrape


def _all_candidates_factory(state_key: str):
    cycles = ALL_STATES[state_key].assembly_cycles
    def _scrape():
        return {c["year"]: scrape_all_candidates(c["slug"]) for c in cycles}
    return _scrape


# Generate the per-state public functions and CYCLES constants. This is what
# the rest of the codebase imports — same shape as goa.py / sikkim.py / delhi.py.
for _key in (
    "puducherry", "mizoram", "manipur", "meghalaya",
    "nagaland", "tripura", "arunachal", "himachal", "uttarakhand",
    # Next-smallest tier (81-90 seats)
    "jharkhand", "haryana", "chhattisgarh",
    # Zone-balancing batch (90-126 seats: J&K North, Telangana South, Assam NE)
    "jk", "telangana", "assam",
):
    globals()[f"scrape_all_{_key}"]            = _winners_factory(_key)
    globals()[f"scrape_all_{_key}_candidates"] = _all_candidates_factory(_key)
    globals()[f"{_key.upper()}_CYCLES"]        = ALL_STATES[_key].assembly_cycles
