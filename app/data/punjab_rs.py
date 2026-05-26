"""
Current Rajya Sabha members from Punjab.

Punjab has 7 RS seats. Members are not directly elected — they're chosen by
the state legislative assembly (Punjab Vidhan Sabha). Terms are 6 years.
Roughly one-third of seats come up for re-election every 2 years.

This list is hand-maintained. When members change (term expiry, resignation,
nomination), edit this file and restart uvicorn. The homepage will pick up
the change immediately.

Last updated: May 2026.
Sources to verify against:
  - https://sansad.in/rs/members
  - https://eci.gov.in/general-election/bye-election/bypolls
  - https://en.wikipedia.org/wiki/List_of_members_of_the_Rajya_Sabha
"""

# Each member is a small dict the homepage can render directly.
# `term_end` is the expected end of the current 6-year term.
PUNJAB_RS_MEMBERS: list[dict] = [
    {
        "name": "Raghav Chadha",
        "party": "AAP",
        "elected": 2022,
        "term_end": 2028,
        "notes": "AAP General Secretary; Punjab Affairs in-charge.",
    },
    {
        "name": "Sandeep Kumar Pathak",
        "party": "AAP",
        "elected": 2022,
        "term_end": 2028,
        "notes": "AAP National General Secretary (Organisation).",
    },
    {
        "name": "Sanjeev Arora",
        "party": "AAP",
        "elected": 2022,
        "term_end": 2028,
        "notes": "Businessman; subsequently elected MLA from Ludhiana West (2025).",
    },
    {
        "name": "Ashok Mittal",
        "party": "AAP",
        "elected": 2022,
        "term_end": 2028,
        "notes": "Chancellor, Lovely Professional University.",
    },
    {
        "name": "Harbhajan Singh",
        "party": "AAP",
        "elected": 2022,
        "term_end": 2028,
        "notes": "Former cricketer.",
    },
    {
        "name": "Vikramjit Singh Sahney",
        "party": "AAP",
        "elected": 2022,
        "term_end": 2028,
        "notes": "Industrialist; replaced earlier vacancy.",
    },
    {
        "name": "Balbir Singh Seechewal",
        "party": "AAP",
        "elected": 2022,
        "term_end": 2028,
        "notes": "Environmentalist; known for reviving the Kali Bein river.",
    },
]
