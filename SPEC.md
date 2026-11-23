# Listing Tool — Spec

## Distribution

### Overview

The tool is distributed to teammates as a zip (`ListingTool-vX.zip`) via a OneDrive shared link. A first-time setup script (`setup.bat`) downloads a self-contained Python 3.11.9 embeddable runtime at install time so no system Python is required and the user's machine is not modified.

### Zip contents

```
ListingTool-vX.zip
├── setup.bat              ← run once (installs Python, deps, creates shortcut)
├── start.bat              ← daily launcher (double-click shortcut → this runs)
├── app.py
├── scrape_listing.py
├── requirements.txt
├── README.txt
└── templates/
    └── (all .json template files)
```

The `python/` folder is NOT included — setup.bat downloads and extracts it on first run.
The `listings/` folder is NOT included — it is created at runtime.

### Ship Checklist (verify before every release)

Every file below MUST be in the zip. `setup.bat` (pre-flight) and `start.bat` now
run this same check at launch and abort with `Missing file: <name>` if any are
absent — this guards against an incomplete download or a partial extract.

- [ ] `setup.bat`
- [ ] `start.bat`
- [ ] `app.py`
- [ ] `scrape_listing.py`
- [ ] `requirements.txt`
- [ ] `README.txt`
- [ ] `templates/` folder, including `templates_meta.json` and every referenced `.json`
- [ ] No stray test/draft templates in `templates/` (review oddly-named files, e.g. `tes.json`)
- [ ] Zip is < 5 MB (confirms `python/` and `listings/` were excluded)

### Importing sibling modules under the embeddable runtime

The embeddable Python build is configured via `python311._pth`, which takes full
control of `sys.path` and does **not** add the running script's own directory.
`from scrape_listing import ...` in `app.py` therefore raises
`ModuleNotFoundError: No module named 'scrape_listing'` even though the file sits
right next to `app.py`. The fix is at the top of `app.py`:

    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

This must run before any sibling-module import. Do not remove it.

### What setup.bat does (in order)

1. Prints a welcome banner and logs everything to `setup.log`.
2. **Pre-flight:** verifies all required files are present (see Ship Checklist); aborts with `Missing file: <name>` if any are missing.
3. Checks three standard Chrome install paths; if not found, opens the Chrome download page and exits.
4. Downloads `python-3.11.9-embed-amd64.zip` from python.org and extracts it to `.\python\` (skipped on re-run if `.\python\python.exe` already exists).
5. Edits `python311._pth` to uncomment `import site` (enables site-packages / pip). This is idempotent.
6. Downloads `get-pip.py` and bootstraps pip into the embeddable Python (skipped if `pip.exe` already exists).
7. Runs `pip install -r requirements.txt` (always runs; idempotent on re-run — fast when already satisfied).
8. Smoke-tests the install: `import flask, requests, bs4, PIL, undetected_chromedriver`.
9. Creates a `Listing Tool` desktop shortcut pointing to `start.bat`, using the Chrome icon.
10. Launches the app; `app.py` auto-opens `http://localhost:5000` in the default browser.

### Python runtime

- **Version**: 3.11.9 embeddable amd64 — pinned to avoid the distutils removal in 3.12 which breaks `undetected-chromedriver`.
- **Location**: `.\python\` (relative to the tool folder) — fully isolated from any system Python.
- **start.bat** calls `.\python\python.exe app.py` directly to ensure the bundled runtime is always used.

### Idempotency

Running `setup.bat` a second time is safe and fast (< 30 s):
- Python download and pip bootstrap are skipped (files already present).
- `pip install` runs but reports "already satisfied" and exits immediately.
- The desktop shortcut is overwritten (no error).

### Edge cases handled

| Scenario | Behaviour |
|---|---|
| Chrome not installed | Prints instructions, opens `https://google.com/chrome`, exits |
| Network blocks Python download | Prints "contact NC" message, exits; error detail in `setup.log` |
| Antivirus removes extracted file | Missing `python.exe` detected after extraction; clear error + exit |
| setup.bat run twice | Fully idempotent — skips already-done steps |
| Path with spaces | All paths use `%TOOL_DIR%` (`%~dp0`) with consistent quoting |

---

## Team Deployment (hosted webapp)

### Overview

As an alternative to the per-machine zip install above, the app can run as a
single shared instance on a Linux VPS via Docker Compose, so teammates just
open a URL instead of installing anything. Two services:

- **`listingtool`** — the Flask app + Chrome + Xvfb, built from the repo's `Dockerfile`.
- **`caddy`** — reverse proxy in front of it that gets free, auto-renewing HTTPS for a real domain.

Access is gated by a single shared password (`APP_PASSWORD`) — see the Auth
section in app.py. When `APP_PASSWORD` is unset (the default for the local
zip install), the login screen is skipped entirely — that path is untouched.

### Files involved

```
Dockerfile              ← builds the app image (Python + Chrome + Xvfb + gunicorn)
docker-entrypoint.sh    ← starts Xvfb, seeds templates/ on first run, launches gunicorn
requirements-server.txt ← requirements.txt + gunicorn (Linux-only; not used by the zip install)
docker-compose.yml      ← the two services + persistent volumes
Caddyfile                ← reverse proxy + HTTPS config — put your domain here
.env.example             ← copy to .env and fill in APP_PASSWORD / SECRET_KEY
.dockerignore
```

### One-time VPS setup

1. **Provision a VPS.** Ubuntu 22.04+, 2 GB RAM minimum (Chrome is the heavy part; scrapes are serialized — see below — so one Chrome instance at a time, but give it room). DigitalOcean, Hetzner, etc. — any provider works.
2. **Point DNS at it** (optional but recommended): an A record for your chosen subdomain (e.g. `listings.yourteam.com`) → the VPS's IP. Skip this and use the Caddyfile's IP-only fallback if you don't have a domain yet.
3. **Install Docker** on the VPS:
   ```
   curl -fsSL https://get.docker.com | sh
   ```
4. **Copy the project to the VPS** (from your machine):
   ```
   rsync -av --exclude python --exclude listings --exclude debug --exclude __pycache__ --exclude "*.zip" . user@your-vps-ip:/opt/listing-tool/
   ```
5. **On the VPS**, in `/opt/listing-tool/`:
   ```
   cp .env.example .env
   nano .env              # set APP_PASSWORD and SECRET_KEY (see comments in the file)
   nano Caddyfile         # replace your-domain.com with your real domain (or switch to the :80 block if none yet)
   docker compose up -d --build
   ```
6. Visit `https://your-domain.com` (or `http://<vps-ip>` if no domain), sign in with the shared password.

### Updating

```
rsync -av --exclude python --exclude listings --exclude debug --exclude __pycache__ --exclude "*.zip" . user@your-vps-ip:/opt/listing-tool/
ssh user@your-vps-ip "cd /opt/listing-tool && docker compose up -d --build"
```

### Data persistence

`./data/listings` and `./data/templates` on the VPS host are bind-mounted
into the container and hold everything (scraped listings, photos, agency
templates) — back these up periodically. `templates/` is seeded from the
image's baked-in starter templates on first run only (see
`docker-entrypoint.sh`); after that the host directory is the source of truth
and image rebuilds won't touch it.

### Scrape concurrency

`SCRAPE_LOCK` in `app.py` serializes scrapes — if two teammates click "Get
Listing" at the same moment, the second waits for the first's Chrome instance
to finish rather than launching a second one. This keeps a small VPS from
running out of RAM under concurrent use; template/listing CRUD endpoints stay
responsive throughout since only the actual browser-scrape step is locked.

### Shared-IP scraping risk — CONFIRMED, requires a residential proxy

All scrapes come from one VPS IP instead of each teammate's home IP, and
KASADA blocks it outright: confirmed by direct testing that even
`https://www.realestate.com.au/` (the bare homepage, no listing) never clears
the JS challenge from a DigitalOcean IP, across a 40s+ poll window, regardless
of browser fingerprint quality (WebGL support was also broken in the
container at one point — fixed — and made no difference). This is IP
reputation, not something a scraper-code tweak fixes.

**Fix: `SCRAPE_PROXY` env var** (see `.env.example`) routes Chrome through a
residential proxy — format `http://username:password@host:port`. nodriver
runs a small local auth-forwarder so Chrome never shows a proxy-auth popup.
When set, `scrape_listing.py` also disables image loading in the browser
(`--blink-settings=imagesEnabled=false`) to cut proxy bandwidth cost — photo
URLs come from inline JS/DOM text and the actual photo bytes are downloaded
separately over plain (non-proxied) `requests`, so images never need to
render in the browser itself.

Leave `SCRAPE_PROXY` unset for local zip installs — each teammate's own
home/office IP is fine as-is, no proxy needed there.

---

## Current Status

Sessions 1, 1.5, and 2 of the template overhaul are complete (folders/tags, custom placeholders, authoring UX with autocomplete + highlighting + warnings). Next: Session 3 — main page insertion UX (template picker, shortcut codes, tab-through placeholders).

Single-file Flask app (`app.py`) that scrapes realestate.com.au listings, lets you select/download photos, and generates Instagram-style captions using per-agency templates.

---

## Architecture

- **Backend**: Flask, single file (`app.py`). No database — all state lives in JSON files on disk.
- **Frontend**: HTML/CSS/JS embedded as a string in `app.py`. No build step, no external JS libraries.
- **Python deps**: `flask`, `requests`, `pillow` (via `scrape_listing.py`).

---

## Directory layout

```
listing-tool/
  app.py                     # entire app
  scrape_listing.py          # scraping helpers
  listings/
    <listing_id>/
      listing.json           # scraped details
      photos/                # downloaded photos (01.jpg … NN.jpg)
      selected/              # user-selected photos (populated by finalize.py)
  templates/
    <agency_slug>.json       # one file per agency template
    templates_meta.json      # folder names + known tags (created on first use)
```

---

## listing.json schema

```json
{
  "url":              "https://www.realestate.com.au/...",
  "address":          "93 Paradise Avenue, Avalon Beach",
  "property_type":    "House",
  "bedrooms":         3,
  "bathrooms":        2,
  "car_spaces":       2,
  "land_size":        "1,368m²",
  "price":            "$5,750,000 - $6,250,000",
  "auction":          null,
  "sale_status":      "For Sale",
  "headline":         "Right on the waterfront at Paradise Beach",
  "description":      "Prose marketing paragraphs only (no headline, bullets, or disclaimer).",
  "features":         ["First bullet text", "Second bullet text"],
  "description_full": "Full body text: prose + bullets + disclaimer + key details.",
  "agency_name":      "McGrath Pittwater - Avalon Beach",
  "agents": [
    {"name": "James Baker", "phone": "0421272692", "agency": "McGrath Pittwater - Avalon Beach"}
  ]
}
```

### Field notes

- **address**: State code and 4-digit postcode are stripped from the end (e.g. `, NSW 2107` removed). Street + suburb only.
- **sale_status**: `"For Sale"` | `"Auction"` | `"Contact Agent"` | `"Under Offer"` | `"Sold"`. Derived from the `auction` field (present → Auction), `price` string (contains "Contact Agent"), and page title/meta description (contains "sold" / "under offer"). Defaults to `"For Sale"`.
- **headline**: The bold/large property tagline above the description (e.g. "The Gridhouse"). Extracted from the `<h2>` inside the `PropertyDescription` element. `null` if not present.
- **description**: Prose marketing paragraphs only — content before the first bullet point line. Does not include headline, address repeat, bullet points, DISCLAIMER, KEY DETAILS, or "Read more".
- **features**: List of bullet point strings with the leading `- ` or `•` stripped. Empty list `[]` if no bullets.
- **description_full**: Full body text from `<p class="property-description__content">` — includes prose paragraphs, bullet points, DISCLAIMER, and KEY DETAILS. Does not include the headline or subtitle (address + date). Use this when you need access to the raw marketing content.
- **agency_name**: Extracted from `<img class="branding__image">` alt attribute (primary), JSON-LD `SaleEvent.organizer.name` (auction fallback), then enquiry-form heuristic (last resort).
- **price**: Trailing period stripped. `"Contact Agent"` is preserved as a string value when that is the listed price.

---

## Template JSON schema

```json
{
  "agency_name":      "McGrath Pittwater",
  "caption_template": "Just Listed | {address}\n\n...",
  "default_agents":   [{"name": "Jane Smith", "phone": "0400 000 000"}],
  "notes":            "Optional free-text notes",
  "default_values":   {"custom_ph": "pre-filled value"},
  "tags":             ["mcgrath", "house", "premium"],
  "folder":           "Active Clients",
  "custom_placeholders": {
    "cta":            "DM us to book a private inspection",
    "hashtags":       "#mcgrathestate #avalonbeach",
    "open_house_time": ""
  }
}
```

- `tags` and `folder` are optional; missing fields default to `[]` / `null`. Existing templates without these fields continue to work without modification.
- `folder` is metadata only — templates are never moved into filesystem subdirectories.
- `custom_placeholders` is optional; defaults to `{}` if missing. Existing templates continue to work without modification.

### custom_placeholders rules

- Keys are placeholder names without curly braces (e.g. `"cta"`, not `"{cta}"`).
- Keys must match `/^[a-z][a-z0-9_]*$/` — lowercase, alphanumeric + underscore, starting with a letter.
- Values are strings; empty string `""` is allowed.
- **Non-empty value**: auto-filled at render time with the saved value.
- **Empty value**: the literal `{placeholder_name}` is preserved in the rendered caption so the user can fill it manually after copying.
- If a custom placeholder name collides with a built-in (e.g. `"address"`), the built-in wins. A warning dialog is shown on save but the save is allowed.
- Validation is enforced on both frontend (before save) and backend (400 response if invalid key).

---

## templates_meta.json schema

```json
{
  "folders": ["Active Clients", "Archive"],
  "tags":    ["mcgrath", "house", "premium"]
}
```

- Created automatically when a folder is first added or tags are first saved.
- `folders` drives the sidebar UI and allows empty folders to be displayed.
- `tags` accumulates all tags ever saved (used for future autocomplete — not yet implemented).

---

## Caption template placeholders

### Auto-filled from scraped data
| Placeholder | Source |
|---|---|
| `{address}` | Listing address (street + suburb only, state/postcode stripped) |
| `{beds}` / `{baths}` / `{cars}` | Bedroom / bathroom / car space count |
| `{land_size}` | Land size string |
| `{property_type}` | e.g. "House" |
| `{features_line}` | "3 Bed \| 2 Bath \| 1 Car \| 450m²" |
| `{description_first_paragraph}` | First prose paragraph of description |
| `{description_full}` | Full description body (prose + bullets + disclaimer + key details) |
| `{price}` | Listed price (trailing period stripped) |
| `{auction_date}` | Auction date if present |
| `{short_url}` | User-supplied short URL |
| `{agency_name}` | Full agency name e.g. "McGrath Pittwater - Avalon Beach" |
| `{agent_1_name}` / `{agent_1_phone}` | First agent (scraped or from `default_agents`) |
| `{agent_2_name}` / `{agent_2_phone}` | Second agent |

### Custom placeholders — render priority

For each `{placeholder}` in the template text, resolution order is:

1. **User session fill** (`customValues`) — user highlighted or typed a value in the CF panel this session. Highest priority, overrides everything.
2. **Built-in** — placeholder is in the standard list above. Uses scraped value. Wins over template-saved custom values (collision warning on save).
3. **Template-saved custom placeholder, non-empty value** — auto-filled with the saved value from `custom_placeholders`.
4. **Template-saved custom placeholder, empty value** — the literal `{placeholder_name}` is preserved in the rendered output so the user can fill it after copying.
5. **Unknown** (not in any category above) — the entire line is stripped from the output.

### Custom Fields panel (main page)

All non-standard placeholders found in the template text appear in the Custom Fields panel regardless of whether they are in `custom_placeholders`. This makes overrides discoverable.

- If a template-saved value is auto-filling a field, the CF textarea is pre-populated with that value and labelled **"from template"** in blue.
- If the user types in the CF textarea, their value takes priority (removes the badge).
- Clicking **clear** removes the user override and reverts to the template-saved value (or empty).

---

## API

### Listings

| Method | Path | Description |
|---|---|---|
| POST | `/api/scrape` | Scrape a listing URL. Body: `{url}`. Returns listing details, matched template, photo count. |
| POST | `/api/download/<listing_id>` | Download selected photos as ZIP. Body: `{keep: [1,3,5,...]}`. |
| GET | `/photos/<listing_id>/<filename>` | Serve a scraped photo. |

**`/api/scrape` error responses** — always JSON, never an HTML error page:

| Status | Body | When |
|---|---|---|
| 400 | `{error}` | Missing URL, or not a parseable realestate.com.au listing. |
| 500 | `{error: "Failed to load page: …"}` | Page fetch (Chrome / network) failed. |
| 500 | `{error: "Scrape failed", details, hint}` | Any error during extraction / parsing. `details` is the exception message; the full traceback is logged to the terminal via `app.logger`. |

The entire scrape body is wrapped in `try/except`, so an extraction bug returns a useful JSON error instead of Flask's default HTML 500 page (which the frontend would otherwise mis-report as "Cannot reach server. Make sure app is running."). The frontend parses the response and shows `error` + `details` + `hint`, falling back to a generic "Server error (HTTP nnn)" message if the body isn't JSON. The scraper creates all output directories before writing and writes **no** debug / scratch files, so it is safe on a fresh install.

### Templates

| Method | Path | Description |
|---|---|---|
| GET | `/api/templates` | List all templates. Optional query params: `?folder=<name>` and `?tag=<name>` for filtering. Returns `id`, `agency_name`, `tags`, `folder` for each. |
| POST | `/api/templates` | Create template. Body: `{agency_name, caption_template, default_agents, notes, tags, folder}`. |
| GET | `/api/templates/<id>` | Get full template data. |
| PUT | `/api/templates/<id>` | Update template. Same body shape as POST. |
| DELETE | `/api/templates/<id>` | Delete template file. |
| GET | `/api/templates/meta` | Return `{folders, tags}` from `templates_meta.json`. |
| POST | `/api/templates/folders` | Add a folder. Body: `{name}`. |
| DELETE | `/api/templates/folders/<name>` | Remove a folder from meta and set `folder=null` on any templates that referenced it. |

---

## UI — main page

- **Topbar**: URL input + "Get Listing" button, auto-detect template dropdown, "Agency Templates" drawer button.
- **States**: idle / loading / error / result.
- **Result view**:
  - Address + pills (beds, baths, cars, land size, price, auction).
  - Photo grid with checkbox selection and order badges.
  - Two-column layout: scraped content (left) + custom fields + caption (right).
  - Highlight-to-assign: select text in the scraped content panel → assign to any placeholder.
  - Caption textarea with short URL input, copy button, "Save to Template" button.
  - Download bar for zipping selected photos.

---

## UI — Agency Templates drawer

Opens as a right-side slide-in panel (520px wide).

### List view (two columns)

```
┌─ Agency Templates ──────────────────────── [×] ─┐
│                                                  │
│ ┌── Folders ────┐  ┌── Templates ─────────────┐  │
│ │ All (4)       │  │ [Agency Name]             │  │
│ │ Active Cl.(1) │  │   tag1  tag2  +1 more     │  │
│ │ Archive   (0) │  │                     [Edit]│  │
│ │               │  │ [Another Agency]          │  │
│ │ + New Folder  │  │                     [Edit]│  │
│ └───────────────┘  └──────────────────────────┘  │
│                                                  │
│ [+ New Template]                                 │
└──────────────────────────────────────────────────┘
```

- "All Templates" always shown first, not deletable.
- User folders show count of templates assigned to them. Empty folders display with count 0.
- Hover a folder to reveal a `×` delete button. Clicking it shows a confirm dialog before deleting.
- Deleting a folder nulls out `folder` on affected templates (templates are not deleted).
- Tag chips show up to 3 tags sorted alphabetically; overflow shown as "+N more".
- Selecting a folder filters the right-side list. If folder is empty: "No templates in this folder yet."

### Edit view

Fields:
- Agency Name (required)
- Caption Template (textarea, monospace)
- Default Agents (name + phone pairs, add/remove)
- Notes (optional)
- Folder (dropdown from meta + "None"; gracefully falls back to "None" if saved folder was deleted)
- Tags (comma-separated text input; lowercased on save)
- Live Preview (uses current listing if one is loaded)

---

## Session history

| Session | Scope |
|---|---|
| 1 (complete) | Template data model (`tags`, `folder`), `templates_meta.json`, folder/tag API endpoints, drawer two-column layout with folder sidebar, tag chips in template list, folder/tags fields in edit form |
| 1.5 (complete) | `custom_placeholders` field: per-template static values, render priority logic, CF panel badge + pre-population, drawer CP section (add/edit/remove rows), live preview integration, name validation, collision warning |
| 2 (complete) | Scraper fixes: `agency_name` via `branding__image`, `headline`, clean `description`/`features`/`description_full` split, address trim, price rstrip, `sale_status`, backwards-compatible `{description_full}` in app.py |
| 3 (complete) | Authoring UX: placeholder autocomplete dropdown, textarea highlight overlay, unknown-placeholder warnings, live preview `renderPreviewHTML` with red-strikethrough for unknowns, ↺ Refresh button |
| 4a (complete) | Template picker (Ctrl+/): centered modal overlay, live search with tier-ranked results, keyboard navigation, `loadAndApplyTemplate` shared function, `captionDirty` flag, "Create your first template" empty state |
| 4b (complete) | Shortcut codes (`;code` style): type `;jl` in caption to apply the template with `shortcut_code: "jl"`; dropdown hint shows near cursor; Arrow/Enter/Tab/Escape keyboard nav; click to apply; dirty-caption guard; duplicate-code warning on save |
| 4c (complete) | Tab-through fillable placeholders: yellow highlight overlay on empty-value custom placeholders; "Tab to fill N placeholders" hint above textarea; Tab/Shift+Tab selects next/previous fillable; no-loop when no gap in that direction; hint hidden when all filled; `getFillablePlaceholders()` + `updateCaptionHighlights()` driven by `updateCaption()` and debounced input |
| 5 (complete) | Scraper engine replaced: switched from `undetected_chromedriver` to `nodriver` (CDP-native, no chromedriver binary) to bypass KASADA bot-detection that blocked listing pages as of June 2026. Fixed address extraction for new `"… - Property Details"` title format. Fixed price extraction to not pick up dollar amounts from KEY DETAILS/rates section. Fixed JSON-LD address to use only street + suburb. New dependency: `nodriver>=0.50` replaces `undetected-chromedriver`. |

---

### KASADA bot-detection — June 2026

realestate.com.au tightened their KASADA bot-protection in approximately June 2026. The previous scraper engine (`undetected_chromedriver`) stopped working because it leaves a chromedriver binary fingerprint (`$cdc_...` variable) that KASADA now detects.

**Fix**: replaced with `nodriver` (by the same author), which connects to Chrome directly via CDP without a chromedriver binary. This eliminates the main detection vectors. If the scraper stops extracting details in future:

1. Check if KASADA is being served: the HTML from a scrape run will be very small (~773 bytes) and contain `KPSDK`.
2. Update `nodriver`: `pip install -U nodriver` (both in the venv and in `requirements.txt`).
3. Selectors to re-audit if content is missing: `[data-testid="PropertyDescription"]` (description), `img.branding__image` (agency name), `[aria-label~="bedroom"]` ul (beds/baths/cars), `<title>` tag (address), `<meta name="description">` (price), `[data-testid="enquiry-form"]` (agents).

---

## Authoring UX — template editor (Session 3)

### Built-in placeholder list (`BUILTIN_PH_LIST`)

Single source of truth in JS. `STANDARD_PH` (the Set used for validation everywhere) is derived from it.

```
address, beds, baths, cars, land_size, property_type, features_line,
description_first_paragraph, description_full, price, auction_date,
short_url, agency_name, agent_1_name, agent_1_phone, agent_2_name, agent_2_phone
```

17 placeholders. These match exactly the keys of the `subs` object in `renderTemplate`. The JSON field names `bedrooms`/`bathrooms`/`car_spaces` are **not** template placeholders — the template uses `beds`/`baths`/`cars`.

### Placeholder autocomplete dropdown

- Triggered when the user types `{` in the Caption Template textarea.
- Filters as the user types more characters (e.g. `{add` → only `{address}`).
- Match is case-insensitive prefix on placeholder name.
- **Groups**: built-ins first, visual separator, then this template's custom placeholders. Separator is omitted when only one group has matches.
- **Recency**: within each group, recently-inserted placeholders float to the top. Tracked in `recentPH` array, session-scoped — resets when the drawer closes or a new template is opened.
- **Keyboard**: ArrowDown/Up navigates; Enter or Tab inserts the selected placeholder and closing `}`; Escape closes without inserting.
- **Click** on an item also inserts.
- If the character immediately after the cursor is `}` (e.g. cursor is inside `{addres}`), `insertPH` consumes it so the result is not double-braced.
- Max 8 items visible; scrollable. ArrowDown/Up calls `scrollIntoView({block:'nearest'})` so the highlighted item is always in view.
- Dropdown anchors near the cursor using the `getCaretCoords` utility (mirror-div technique; see below).
- Closes on blur, Escape, or clicking outside the textarea.

### Textarea syntax-highlight overlay

The "mirror div behind the textarea" technique:

**Structure:**
```html
<div class="tpl-body-wrap">           <!-- position:relative; contains everything -->
  <div id="tpl-body-backdrop">        <!-- position:absolute; inset:0; overflow:hidden; pointer-events:none -->
    <div id="tpl-body-highlights">    <!-- same font/padding as textarea; color:transparent -->
    </div>
  </div>
  <textarea id="tpl-body" ...>        <!-- background:transparent; z-index:1 -->
  </textarea>
  <div id="ph-autocomplete">...</div>
</div>
```

**How it works:**
- `#tpl-body-highlights` has identical typography (font-family, font-size, line-height, padding, border-width) to `#tpl-body` so text positions align.
- Its `color: transparent` hides all text; only `<mark class="ph-hl">` spans (background `#dbeafe`) are visible.
- The textarea has `background: transparent` so the blue marks show through.
- `updateHighlights()` HTML-escapes the textarea value, wraps every `{placeholder}` match in `<mark class="ph-hl">`, and sets `#tpl-body-highlights.innerHTML`.
- Scroll sync: the `scroll` event on the textarea updates `hl.style.marginTop = -ta.scrollTop + 'px'`. This shifts the highlights content within the `overflow:hidden` backdrop — the canonical approach for non-scrollable overlay divs.
- The `#tpl-body-backdrop` uses `position:absolute; inset:0` and the wrapper is a normal-flow block, so drag-resizing the textarea automatically adjusts the wrapper and backdrop size.

**Known limitation:** when the textarea content overflows and a scrollbar appears, the scrollbar reduces the textarea's content width by ~15px. The highlights div doesn't have a scrollbar, so long lines may wrap at slightly different positions. This is a known limitation of the overlay technique and is minor in practice.

### Unknown-placeholder warnings

- `#ph-warnings` div appears between the textarea wrap and the `.ph-ref` hint block.
- Empty (`innerHTML = ''`) when all `{placeholder}` names are known (built-in or in this template's `custom_placeholders`).
- Shows `⚠ Unknown placeholders: {name1}, {name2}` when typos exist. Each name is a clickable `.ph-warn-item` span.
- Debounced 300 ms via `scheduleWarnings()` — fires on textarea input, custom-placeholder name blur, and custom-placeholder row removal.
- **Click on an unknown name**: calls `jumpToUnknown(name)` which finds the first `{name` occurrence in the textarea, places the cursor right after the name (inside the `{name}` token), and calls `maybeShowAC()` — the dropdown opens pre-filtered so the user can pick the correct placeholder and press Enter to replace.

### `getCaretCoords` utility

Used to anchor the autocomplete dropdown near the cursor:

1. Create a hidden `div` with the same `fontFamily`, `fontSize`, `lineHeight`, `padding`, `border`, `boxSizing`, and `width` as the textarea.
2. Fill it with `document.createTextNode(ta.value.substring(0, ta.selectionStart))`.
3. Append a `<span>` containing U+200B (zero-width space).
4. Measure `span.getBoundingClientRect()` relative to `ta.getBoundingClientRect()`.
5. Add `ta.scrollTop`/`scrollLeft` to account for scroll offset.
6. Remove the mirror div immediately.

Returns `{x, y}` in coordinates relative to the textarea's top-left (= the `.tpl-body-wrap` top-left since the wrapper has no padding/border). The dropdown is then absolutely positioned inside `.tpl-body-wrap`.

### Live preview (`renderPreviewHTML`)

A preview-specific variant of `renderTemplate` that:
- HTML-escapes the template text first (using `escHtml`).
- HTML-escapes each substitution value.
- Applies the same priority order: session fills → built-ins → non-empty custom values.
- Does **not** strip lines (all lines including unknown-placeholder lines remain visible in the preview).
- Empty custom placeholders (value = `""`) are preserved as literal `{name}` (same as `renderTemplate`), with a guard to avoid giving them the red-strikethrough treatment.
- Wraps truly unknown `{name}` tokens in `<span class="ph-unknown">` (CSS: `color:#dc2626; text-decoration:line-through`) so the user can see which placeholders won't resolve.
- `refreshPreview()` uses `box.innerHTML` (not `textContent`) for this function. The `.preview-box` retains `white-space: pre-wrap` which preserves newlines in text nodes.
- A ↺ Refresh button is added to the preview label row for manual re-render.

---

## Template Picker — Session 4a

### Trigger

`Ctrl+/` anywhere on the main page. Toggles: opens if closed, closes if open.

### Visual

Centered modal overlay, `width:480px`, `max-height:60vh`, `border-radius:10px`. Semi-opaque dark backdrop with `backdrop-filter:blur(2px)`, `z-index:200` (above drawer at 101, below `customConfirm` dialogs at 300). Click outside the modal closes the picker.

### Keyboard behaviour

| Key | Action |
|---|---|
| Ctrl+/ | Open / close |
| Type | Live-filter results; first match auto-highlighted |
| ArrowDown / ArrowUp | Move highlight (wraps top↔bottom) |
| Enter | Load highlighted template, close picker |
| Tab | Prevent default (no focus leak); first item already highlighted |
| Escape | Close without loading |

### Search / filter algorithm

Case-insensitive substring match against `agency_name`, `folder`, and `tags`. Results grouped into three tiers, each sorted alphabetically by `agency_name`:

1. `agency_name` **starts with** query
2. `agency_name` **contains** query (but doesn't start with it)
3. **folder or any tag** contains query (agency name has no match)

### Auto-highlight behaviour

`pickerFilter()` always sets `pickerActive = pickerItems.length ? 0 : -1`. On open (no query) the first item is highlighted; on each keystroke the first matching item is highlighted. Enter works immediately — no Arrow Down required.

### List item layout

```
┌──────────────────────────────────────────┐
│ Agency Name                    Ctrl+/   │  ← active item (.pk-active)
│ Folder name  tag1  tag2                  │
└──────────────────────────────────────────┘
```

`Ctrl+/` hint appears only on the `.pk-active` item (via CSS `display:none` / `display:block`).

### Empty state

- **Results exist but none match**: "No templates match" (no create button).
- **Zero templates in system**: "No templates match" + "Create your first template" ghost button → calls `closePicker(); openDrawerNew()`.

### Template loading — shared function

`loadAndApplyTemplate(id)` is extracted from the former inline `onTemplateSelect()` body. Both the topbar `<select>` (`onTemplateSelect`) and the picker (`pickerSelect`) call the same function. It:

1. Fetches `/api/templates/<id>` for full template data.
2. Sets `preSelectedTemplate`, `tplId`, `tplData`.
3. Syncs the topbar `<select>` to the chosen id.
4. If a listing is loaded: updates `#tpl-label`, hides the no-template banner, resets `customValues` from `default_values`, calls `renderCustomFields()` and `updateCaption()`.
5. If no listing is loaded: state is stored and used when the user scrapes next (via `preSelectedTemplate` check in `renderResult`).

### Dirty-caption guard

`captionDirty` flag: set to `true` on `input` event on `#caption-text`; reset to `false` inside `updateCaption()` after writing the rendered value. `pickerSelect(id)` checks: if `listing && captionDirty`, shows `customConfirm('…replace your current caption?…', 'Replace', 'Keep my edits')`. "Keep my edits" returns early (picker stays open); "Replace" proceeds with `closePicker()` then `loadAndApplyTemplate(id)`.

---

## Shortcode hint — Session 4b

### Template field

`shortcut_code` (string | null) — stored in the template JSON alongside `agency_name`, `tags`, etc. `null` when not set. Must match `/^[a-z][a-z0-9]*$/` after lowercasing and stripping non-alphanumeric characters.

The backend's `all_templates()` returns it in the list response. POST and PUT endpoints accept and save it.

### Drawer edit form

"Shortcut Code" input (`#tpl-shortcut`) added between Tags and Live Preview. Monospace, max-width 120px. Cleared on new template; loaded on edit.

On save: the raw value is lowercased and stripped of non-`[a-z0-9]` chars. If the resulting code collides with another template's code (checked against `allTplList`), a `customConfirm` dialog warns before saving.

### Caption textarea — detection

`checkShortcode()` runs on every `input` event on `#caption-text` (only when `listing` is set). It reads the text before the cursor and checks for `/;([a-z0-9]+)$/`. If found, it filters `scodeMap` for entries where `code.startsWith(typed)` and calls `showScodeHint(matches)`. If no match, `closeScodeHint()`.

`scodeMap` is built by `buildScodeMap(tpls)` which is called at the end of `loadTemplateSelect()` (and therefore also after `loadList()` → `loadTemplateSelect()`).

### Shortcode dropdown (`#sc-hint`)

- `position:absolute` inside `.caption-wrap` (`position:relative` wrapper around `#caption-text`)
- Positioned at the cursor using `getCaretCoords(ta)` — same utility as the drawer's placeholder autocomplete
- Renders `.sc-item` rows: `{code}` chip (blue monospace) + agency name + `↵` hint
- Active item highlighted with `.sc-active`

### Keyboard behaviour

| Key | Action |
|---|---|
| ArrowDown / ArrowUp | Move active item (wraps) |
| Enter or Tab | Apply active shortcode |
| Escape | Close hint without applying |

### Mouse

Click on a `.sc-item` → `mousedown` (with `e.preventDefault()` to keep textarea focus) → `applyShortcode(id)`.

Blur on `#caption-text` → `setTimeout(closeScodeHint, 150)` — delay allows clicks on the dropdown to register first.

### Applying a shortcode

`applyShortcode(id)`:
1. If `listing && captionDirty` → shows dirty-caption guard (same dialog as picker).
2. If confirmed (or no dirty caption): `closeScodeHint()` then `loadAndApplyTemplate(id)`.
   `loadAndApplyTemplate` calls `updateCaption()` which completely replaces the caption, so the `;code` text is overwritten automatically.

---

## Caption fill overlay — Session 4c

### Visual highlighting

The caption textarea (`#caption-text`) uses the same mirror-overlay technique as the template editor's `#tpl-body-highlights` (Session 3):

- `.caption-wrap` is `position:relative` (already existed from Session 4b)
- `#cap-backdrop` — `position:absolute; inset:0; overflow:hidden; pointer-events:none`
- `#cap-highlights` — matches `#caption-text` typography exactly: `font-family:inherit`, `.88rem`, `1.65` line-height, `12px 14px` padding, `1px solid transparent` border, `pre-wrap`, `color:transparent`
- `#caption-text` — `background:transparent; position:relative; z-index:1` (so backdrop shows through)
- `.cap-fillable { background:#fef3c7 }` — light amber, one per unfilled custom placeholder

### Fillable set — `getFillablePlaceholders()`

Returns a `Set<string>` of custom placeholder names where `value === ''` in `tplData.custom_placeholders`. Returns empty Set if no template loaded. Only empty-value custom placeholders are fillable — built-ins and non-empty custom values are never highlighted.

### `updateCaptionHighlights()`

1. Gets `fillable` set.
2. If empty: clears highlights, hides `#fill-hint`, returns.
3. `escHtml(ta.value)` → regex `/\{([a-z][a-z0-9_]*)\}/g` → wraps only names in `fillable` with `<mark class="cap-fillable">`. Counts matches.
4. Sets `hl.innerHTML = marked`.
5. Syncs scroll: `hl.style.marginTop = -ta.scrollTop + 'px'`.
6. Updates `#fill-hint` — "Tab to fill N placeholder(s)" when `count > 0`, hidden when `count === 0`.

Called directly from `updateCaption()` and via `scheduleHighlights()` (100 ms debounce) on every caption `input` event. Caption `scroll` event syncs marginTop only.

### `#fill-hint`

Sits between `.url-row` and `.caption-wrap` (above the textarea). `display:none` when no fillable gaps remain. Text: `"Tab to fill N placeholders"` (singular `"placeholder"` when N = 1).

### Tab / Shift+Tab behaviour

Keydown handler priority:
1. If shortcode hint is open (`scodeItems.length > 0`): Arrow/Enter/Tab/Escape handled by shortcode logic, then `return`.
2. If `e.key === 'Tab'` (no Ctrl/Alt/Meta modifier):
   - `fillable.size === 0` → `return` (default Tab — focus moves to next element, e.g. Copy button)
   - `fillable.size > 0` → `e.preventDefault()` always
   - Forward (no Shift): scan text for first fillable `{name}` where `match.index >= selectionEnd` → select it
   - Backward (Shift): scan text for last fillable `{name}` where `match.index + match[0].length <= selectionStart` → select it
   - If no match found: do nothing — cursor stays, no loop, no Tab-out (the `preventDefault` already fired)

Selecting a `{name}` token means `ta.selectionStart = match.index; ta.selectionEnd = match.index + match[0].length`. The user then types to replace the selection — standard browser behaviour. The `input` event fires and `scheduleHighlights()` re-renders with the updated count.

---

## Known issues

- **`property_type` null in old listing.json files**: Listings scraped before `property_type` was added to the return dict will have `"property_type": null`. Re-scraping the listing fixes it. No data migration is needed — the field has been in the scraper since current code, only stale JSON files on disk are affected.
