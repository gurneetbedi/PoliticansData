"""
Map Indian Penal Code (IPC) sections, special-act references and common
abbreviations to human-readable case types. Used on the detail page's
Legal Disclosure card so users see a quick label like "Cheating" or
"Attempt to Murder" alongside the raw IPC badges.

The list isn't exhaustive — it focuses on sections that show up frequently
in Indian political affidavits. Unknown sections fall through to "Other criminal".
"""
import re

# Order matters: more specific patterns first. Each entry maps a regex
# pattern (matched against any IPC section string the politician declared)
# to a human-readable label.
IPC_TO_TYPE: list[tuple[str, str]] = [
    # Crimes against the person
    (r"\b302\b",                   "Murder"),
    (r"\b307\b",                   "Attempt to murder"),
    (r"\b304\b",                   "Culpable homicide"),
    (r"\b323|324|325\b",           "Causing hurt"),
    (r"\b354\b",                   "Outraging modesty"),
    (r"\b376\b",                   "Sexual assault"),
    (r"\b498A?\b",                 "Domestic cruelty"),
    (r"\b506\b",                   "Criminal intimidation"),
    (r"\b504\b",                   "Insult / breach of peace"),
    # Crimes against property
    (r"\b378|379|380\b",           "Theft"),
    (r"\b420\b",                   "Cheating"),
    (r"\b406|407|408|409\b",       "Criminal breach of trust"),
    (r"\b403|404\b",               "Misappropriation"),
    (r"\b467|468|471\b",           "Forgery"),
    (r"\b454|457\b",               "House-breaking"),
    # Public order
    (r"\b143|144|147|148|149\b",   "Rioting / unlawful assembly"),
    (r"\b188\b",                   "Disobedience to public servant"),
    (r"\b341|342\b",               "Wrongful restraint"),
    (r"\b153A|153B\b",             "Promoting enmity"),
    (r"\b124A\b",                  "Sedition"),
    # Conspiracy / abetment
    (r"\b120B\b",                  "Criminal conspiracy"),
    (r"\b34\b",                    "Common intention"),
    (r"\b109\b",                   "Abetment"),
    # Election-related
    (r"\b171[A-Z]?\b",             "Election bribery / expenses"),
    (r"\bRPA|Representation of People",  "Election Code violation"),
    # Anti-corruption
    (r"PC\s*Act|Prevention of Corruption",   "Corruption"),
    # Arms / drugs / SC-ST
    (r"\bArms Act\b",              "Arms Act violation"),
    (r"\bNDPS\b",                  "Narcotics / NDPS"),
    (r"\bSC[/\s]?ST\b",            "SC/ST Act"),
]


def case_type_for_ipc(ipc_text: str | None) -> str:
    """Return a human-readable primary case type derived from IPC text.
    Returns 'Other criminal' when no mapping matches a non-empty input.
    Returns '' when input is empty/None."""
    if not ipc_text:
        return ""
    text = str(ipc_text).strip()
    if not text:
        return ""
    for pattern, label in IPC_TO_TYPE:
        if re.search(pattern, text, re.I):
            return label
    return "Other criminal"


def all_case_types(ipc_text: str | None) -> list[str]:
    """Return ALL distinct case types matching a given IPC string.
    A single case can fall under multiple categories (e.g. IPC 302, 120B
    = Murder + Criminal conspiracy)."""
    if not ipc_text:
        return []
    text = str(ipc_text).strip()
    if not text:
        return []
    found = []
    for pattern, label in IPC_TO_TYPE:
        if re.search(pattern, text, re.I) and label not in found:
            found.append(label)
    return found if found else ["Other criminal"]
