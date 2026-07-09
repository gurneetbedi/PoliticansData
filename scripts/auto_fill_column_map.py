"""
Auto-fill the Wikipedia results column-map in a state loader script.

Uses Gemini 2.5 Flash to identify the correct table + column offsets from
the loader's `--dump-tables` diagnostic output. Replaces the empty
`<STATE>_<YEAR>_COLS = {"table_index": None, ...}` block with the correct
values in-place.

Removes the "user pastes dump-tables output, human fills column map"
step of the ingestion pipeline. Cost: ~$0.001 per state via Gemini
(negligible; well under 1000 input tokens).

Usage:
    python scripts/auto_fill_column_map.py --state Chhattisgarh --year 2023

The script:
    1. Runs `python scripts/load_<state>_election_results.py --year <year> --dump-tables`
    2. Sends the output to Gemini with a targeted prompt
    3. Parses Gemini's JSON response
    4. Edits the loader file in-place to fill the column map
    5. Verifies by running `--dry-run --refetch` and checking parse count
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parent.parent

GEMINI_PROMPT = """You are analyzing Wikipedia's per-constituency results table for an Indian state election. The diagnostic dump below lists every table on the Wikipedia page with its header row and first 3 sample data rows.

Your task: identify the MASTER RESULTS TABLE (the one containing winner + runner-up name, party, votes, and %  per constituency) and return the column offsets to extract from it.

The master results table has these characteristics:
- 60-250 rows (matches assembly size)
- Header row 0 typically says: 'Constituency' or ['District', 'Constituency', 'Winner', 'Runner-up', 'Margin']
- Header row 1 has sub-columns like: ['#' or 'No.', 'Name', 'Candidate', 'Party', 'Votes', '%', 'Candidate', 'Party', 'Votes', '%', ...]
- Data rows are typically 13 or 14 cells (the party has an empty color-box cell before each party name)

Common layout patterns:
- NE/Hill pattern: 13-cell subsequent rows, columns: [#, ConstName, WinnerName, '', WinnerParty, WinnerVotes, WinnerPct, RunnerName, '', RunnerParty, RunnerVotes, RunnerPct, Margin]
- Haryana/Sikkim2019 pattern: 14-cell rows with Turnout%: [#, ConstName, Turnout, WinnerName, '', WinnerParty, WinnerVotes, WinnerPct, RunnerName, '', RunnerParty, RunnerVotes, RunnerPct, Margin]
- Goa pattern: 14-cell rows, no Turnout, margin has votes+%: [#, ConstName, WinnerName, '', WinnerParty, WinnerVotes, WinnerPct, RunnerName, '', RunnerParty, RunnerVotes, RunnerPct, MarginVotes, MarginPct]

Use NEGATIVE indices so both 13-cell subsequent rows and 14-cell first-of-district rows align.

Return ONLY a JSON object with this exact shape (no markdown, no prose):
{
  "table_index": <int>,
  "header_rows": 2,
  "cols": {
    "constituency": <int, negative>,
    "winner_name": <int, negative>,
    "winner_party": <int, negative>,
    "winner_votes": <int, negative>,
    "winner_pct": <int, negative>,
    "runner_name": <int, negative>,
    "runner_party": <int, negative>,
    "runner_votes": <int, negative>,
    "runner_pct": <int, negative>
  }
}

Diagnostic dump follows:
=====
{dump_output}
=====
"""


def run_dump_tables(loader_path: Path, year: int) -> str:
    """Run the loader's --dump-tables and capture stderr output."""
    result = subprocess.run(
        [sys.executable, str(loader_path), "--year", str(year), "--dump-tables"],
        capture_output=True, text=True, cwd=PROJECT_ROOT,
    )
    return result.stderr + result.stdout


def call_gemini_for_map(dump_output: str, state: str, year: int) -> dict:
    """Ask Gemini to identify the column map from dump-tables output."""
    from google import genai
    from google.genai import types

    # Read project ID from env credentials
    key_path = os.environ.get("GOOGLE_APPLICATION_CREDENTIALS")
    if not key_path:
        sys.exit("GOOGLE_APPLICATION_CREDENTIALS not set")
    key = json.load(open(key_path))
    project = key["project_id"]

    client = genai.Client(vertexai=True, project=project, location="us-central1")

    prompt = GEMINI_PROMPT.replace("{dump_output}", dump_output[:20000])

    # Gemini 2.5 Flash uses "thinking" tokens by default which can eat the
    # entire output budget before any JSON is emitted. Disable thinking
    # (this task doesn't need reasoning) and give plenty of output headroom.
    gen_config_kwargs = dict(
        temperature=0.0,
        response_mime_type="application/json",
        max_output_tokens=8192,
    )
    try:
        gen_config_kwargs["thinking_config"] = types.ThinkingConfig(thinking_budget=0)
    except AttributeError:
        pass  # older SDK without ThinkingConfig — the higher token limit alone should be enough

    resp = client.models.generate_content(
        model="gemini-2.5-flash",
        contents=prompt,
        config=types.GenerateContentConfig(**gen_config_kwargs),
    )

    text = (resp.text or "").strip()

    # Strip markdown fences if Gemini added them
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.MULTILINE)
        text = text.strip()

    # Extract the first balanced {...} block — Gemini sometimes adds prose
    # before/after even when told not to.
    first_brace = text.find("{")
    if first_brace >= 0:
        depth = 0
        for i in range(first_brace, len(text)):
            if text[i] == "{":
                depth += 1
            elif text[i] == "}":
                depth -= 1
                if depth == 0:
                    text = text[first_brace:i + 1]
                    break

    # Remove trailing commas which Gemini occasionally emits
    text = re.sub(r",(\s*[}\]])", r"\1", text)

    try:
        return json.loads(text)
    except json.JSONDecodeError as e:
        print("\n--- RAW GEMINI RESPONSE ---")
        print(resp.text)
        print("--- END RAW RESPONSE ---\n")
        print(f"Cleaned text that failed to parse:\n{text}\n")
        raise SystemExit(f"Gemini returned non-JSON output. Error: {e}")


def find_col_block(loader_source: str, state: str, year: int) -> tuple[int, int]:
    """Find the byte range of the <STATE>_<YEAR>_COLS = {...} block.

    Matches any UPPER_SNAKE identifier followed by _<year>_COLS to tolerate
    naming variations like CHHATTISGARH vs JAMMU_AND_KASHMIR vs the state
    variable not being renamed after copying from a template.
    """
    m = re.search(rf"^[A-Z_]+_{year}_COLS\s*=\s*\{{", loader_source, re.MULTILINE)
    if not m:
        return -1, -1

    # Find matching closing brace, respecting nesting
    start = m.start()
    depth = 0
    i = m.end() - 1  # position of first {
    while i < len(loader_source):
        c = loader_source[i]
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return start, i + 1
        i += 1
    return -1, -1


def patch_loader(loader_path: Path, state: str, year: int, col_map: dict) -> None:
    """Rewrite the <STATE>_<YEAR>_COLS block in the loader file."""
    src = loader_path.read_text()
    start, end = find_col_block(src, state, year)
    if start < 0:
        sys.exit(f"Could not find <STATE>_{year}_COLS block in {loader_path}")

    # Extract the variable name (same as we matched)
    var_name = src[start:end].split("=")[0].strip()

    # Build the replacement block
    cols_pretty = ",\n        ".join(
        f'"{k}": {v}' for k, v in col_map.get("cols", {}).items()
    )
    replacement = (
        f"{var_name} = {{\n"
        f"    # Filled in by auto_fill_column_map.py via Gemini.\n"
        f"    \"table_index\": {col_map['table_index']},\n"
        f"    \"header_rows\": {col_map.get('header_rows', 2)},\n"
        f"    \"cols\": {{\n"
        f"        {cols_pretty}\n"
        f"    }},\n"
        f"}}"
    )
    new_src = src[:start] + replacement + src[end:]
    loader_path.write_text(new_src)


def verify_parse(loader_path: Path, year: int) -> int:
    """Run --dry-run --refetch to see how many constituencies get parsed.

    This is a PARSE test only — it doesn't require constituencies to
    exist in the DB. It measures whether the column map correctly
    extracts constituency names + candidate info from the Wikipedia table.
    """
    result = subprocess.run(
        [sys.executable, str(loader_path), "--year", str(year),
         "--dry-run", "--refetch"],
        capture_output=True, text=True, cwd=PROJECT_ROOT,
    )
    out = result.stderr + result.stdout
    m = re.search(r"Parsed (\d+) constituencies", out)
    if m:
        return int(m.group(1))
    return 0


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                  formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--state", required=True,
                    help="Loader script prefix, e.g. 'chhattisgarh' or 'jk'")
    ap.add_argument("--year", type=int, required=True)
    ap.add_argument("--loader", default="",
                    help="Path to loader script. Defaults to "
                         "scripts/load_<state>_election_results.py")
    args = ap.parse_args()

    if args.loader:
        loader_path = Path(args.loader)
    else:
        loader_path = PROJECT_ROOT / "scripts" / f"load_{args.state.lower()}_election_results.py"

    if not loader_path.exists():
        sys.exit(f"Loader not found: {loader_path}")

    # Refuse to run if the template still has lowercase source-state name.
    # This catches the common "cp X to Y, forgot the third sed case" bug
    # where OUTPUT_JSON or _wiki_ cache paths silently write to the wrong file.
    src = loader_path.read_text()
    tmpl_leaks = []
    for tmpl in ("chhattisgarh", "goa", "punjab", "delhi", "manipur",
                 "tripura", "meghalaya", "uttarakhand", "jharkhand"):
        if tmpl != args.state.lower() and tmpl in src.lower():
            # Only flag if it appears in a file-path context (case-sensitive lowercase)
            if tmpl in src:
                tmpl_leaks.append(tmpl)
    if tmpl_leaks:
        print(f"⚠  {loader_path.name} still contains template state name(s): {tmpl_leaks}")
        print(f"   Run: sed -i '' 's/{tmpl_leaks[0]}/{args.state.lower()}/g' {loader_path}")
        sys.exit(1)

    print(f"→ Running dump-tables for {args.state} {args.year} ...")
    dump = run_dump_tables(loader_path, args.year)
    if not dump.strip():
        sys.exit("Dump-tables produced no output. Check the loader manually.")
    print(f"  ({len(dump.splitlines())} lines of diagnostic output)")

    print(f"→ Asking Gemini to identify the results table + column offsets ...")
    col_map = call_gemini_for_map(dump, args.state, args.year)
    print(f"  Table index:   {col_map['table_index']}")
    print(f"  Column count:  {len(col_map.get('cols', {}))}")
    print(f"  Constituency:  {col_map['cols'].get('constituency')}")

    print(f"→ Patching {loader_path.name} ...")
    patch_loader(loader_path, args.state, args.year, col_map)

    print(f"→ Verifying by running --dry-run --refetch ...")
    n = verify_parse(loader_path, args.year)

    # If parse produced 0 rows, the constituency offset is almost always
    # off-by-one (Gemini can confuse -11 with -12 when a color-box cell
    # separates name from party). Try shifting the constituency offset
    # left by 1 as a rescue attempt.
    if n == 0:
        print(f"  ⚠ Parsed 0. Trying constituency offset shifted by -1 ...")
        rescue = dict(col_map)
        rescue["cols"] = dict(col_map["cols"])
        rescue["cols"]["constituency"] = col_map["cols"]["constituency"] - 1
        patch_loader(loader_path, args.state, args.year, rescue)
        n = verify_parse(loader_path, args.year)
        if n > 30:
            print(f"  ✓ Rescued: parsed {n} constituencies with constituency={rescue['cols']['constituency']}.")
            col_map = rescue
        else:
            # revert to original attempt
            patch_loader(loader_path, args.state, args.year, col_map)

    if n > 30:
        print(f"  ✓ Parsed {n} constituencies. Column map is likely correct.")
    elif n > 0:
        print(f"  ⚠ Only parsed {n} constituencies. Column map may need review.")
    else:
        print(f"  ✗ Parsed 0 even after rescue. Column map is wrong — check manually with --dump-tables.")


if __name__ == "__main__":
    main()
