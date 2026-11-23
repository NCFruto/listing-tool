# Listing Tool — Notes

**Date:** 2026-05-05

## Current status

Sessions 1, 1.5, 2, 3a, 3b, 3c all complete. Template overhaul DONE.

Daily caption flow: scrape → `;code` to apply template → Tab through gaps → copy.

## Ship checklist — creating a release zip

### Required files (MUST be in the zip — the app will not run without all of them)

- `setup.bat`
- `start.bat`
- `app.py`
- `scrape_listing.py`
- `requirements.txt`
- `README.txt`
- `templates/` folder (with `templates_meta.json` and ALL referenced `.json` files)

`setup.bat` (pre-flight) and `start.bat` now verify these at launch and abort with
`Missing file: <name>` if any are missing — this catches incomplete downloads and
partial extracts, the failure mode hit in v1.0.

### Files to include

```
app.py
scrape_listing.py
setup.bat
start.bat
requirements.txt
README.txt
templates/              ← entire folder including templates_meta.json
```

### Files to EXCLUDE

```
python/                 ← downloaded at runtime by setup.bat; too large (~30 MB) and unnecessary to ship
listings/               ← user's scraped listing data; personal/not part of the app
setup.log               ← debugging artifact; not relevant to a fresh install
finalize.py             ← internal dev script; not part of the end-user distribution
debug.png               ← scraper debugging artifact
debug_page.html         ← scraper debugging artifact
*.pyc / __pycache__/    ← Python bytecode; not needed
.claude/                ← AI session files; not for distribution
```

### Zip procedure

1. Confirm `templates/` contains only real templates. Review oddly-named files before zipping — v1.0 shipped `tes.json` (a "Gaby McEwan" template under a stray filename); decide whether to rename it properly or delete it.
2. Select: `app.py`, `scrape_listing.py`, `setup.bat`, `start.bat`, `requirements.txt`, `README.txt`, and the entire `templates/` folder.
3. Right-click → Send to → Compressed (zipped) folder (or use 7-Zip).
4. Rename the zip to `ListingTool-v<N>.zip` (increment the version number).
5. Upload to the OneDrive shared folder and update the shared link if needed.
6. Verify zip size is under 5 MB (Python downloads at runtime).

### Version history

| Version | Date | Notes |
|---|---|---|
| v1.0 | 2026-05-09 | Initial distribution — template overhaul complete |
| v1.1 | 2026-06-08 | Fix: `app.py` adds its own folder to `sys.path` so the embeddable Python can import `scrape_listing` (root cause of the `ModuleNotFoundError` a teammate reported). Added required-file checks to `setup.bat` (pre-flight) and `start.bat`. |
| v1.2 | 2026-06-08 | Fix: removed leftover debug-file writes in `scrape_listing.py` (`debug/page_source.html`, `debug/enq_form.html`) that crashed scraping on fresh installs with `FileNotFoundError: debug\enq_form.html` (Curt's report — one write was unguarded). `/api/scrape` now wraps the whole scrape in try/except and returns JSON `{error, details, hint}` + HTTP 500 on failure instead of an HTML error page; the frontend parses and displays it (was mis-reporting every crash as "Cannot reach server"). |

---

## Known bugs / gotchas

- **`property_type` null in old listing.json files**: Listings scraped before `property_type` was added have `"property_type": null`. Re-scraping fixes it — no migration needed.
- **Textarea highlight overlay width mismatch**: When the textarea grows wide enough to show a scrollbar, the scrollbar narrows the textarea content width by ~15px. The highlights overlay div has no scrollbar, so long lines may wrap at slightly different positions. Minor in practice; known limitation of the mirror-div technique.
- **Scraper session numbering**: SPEC.md session history labels differ from the shorthand used here — "Session 2" here corresponds to "Sessions 2 + 3" in SPEC.md (scraper fixes merged into the authoring UX session).
- **Embeddable Python omits the script dir from `sys.path`**: The bundled `python311._pth` fully controls `sys.path` and does NOT add `app.py`'s own folder, so sibling imports (`from scrape_listing import ...`) fail with `ModuleNotFoundError` even though the file is present. Fixed in v1.1 by `sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))` at the top of `app.py`. Do not remove that line. This is also why the file-presence checks alone don't fix it — the file was there; the path was wrong.
- **No debug-file writes in the scraper (fresh-install safety)**: `scrape_listing.py` used to do `Path("debug/...").write_text(...)` for dev dumps. Those assumed a `debug/` folder that exists on the dev machine but NOT on fresh installs → `FileNotFoundError` (the `enq_form.html` one was unguarded and crashed the whole scrape; the `page_source.html` one was wrapped in `try/except pass`). Both removed in v1.2. Do NOT add `Path("debug/...")` writes back — if you genuinely need a dev dump, do `Path("debug").mkdir(exist_ok=True)` first AND guard the write, or keep it on a local-only branch.
- **Scrape failures surface as JSON, not HTML**: `/api/scrape` wraps the whole body in try/except → returns `{error, details, hint}` with HTTP 500, and logs the full traceback to the terminal via `app.logger`. If you add new scrape steps, keep them inside that try so failures stay debuggable in the browser. The frontend (`startScrape` + `showError`) shows `details`/`hint` and uses the `NL` constant (= `String.fromCharCode(10)`) for line breaks because `HTML_PAGE` is a plain triple-quoted string where `\n` would become a real newline.
