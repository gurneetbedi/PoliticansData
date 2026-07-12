# Party Symbols

Election Commission of India party symbols used by the heatmap
constituency tiles and other UI surfaces.

## Populating

Run the downloader once (idempotent):

```bash
python scripts/download_party_symbols.py
```

Files land here as `<SHORT_NAME>.svg` or `<SHORT_NAME>.png`. Parenthetical
names like `SS(UBT)` are saved as `SS_UBT.png` (parens replaced with `_`).

## How the frontend consumes them

The `renderCt()` function in `app/templates/heatmap.html` tries to load
`/static/party_symbols/<safe_short_name>.png` (falling back to `.svg`,
then to a text pill if neither exists). Missing files are non-blocking.

## Adding a new party

Edit `PARTY_SVG_URLS` in `scripts/download_party_symbols.py` and re-run
the downloader.
