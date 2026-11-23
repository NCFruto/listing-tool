import os
import sys

# The bundled Python (embeddable build) uses a python311._pth file that takes
# full control of sys.path and does NOT add this script's own folder to it.
# Without the next line, "from scrape_listing import ..." below fails with
# ModuleNotFoundError even though scrape_listing.py sits right next to this
# file. Must run before that import. See SPEC.md > Distribution.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import hmac
import io
import json
import re
import shutil
import threading
import time
import zipfile
from pathlib import Path

import requests as req_lib
from flask import (
    Flask, make_response, request, jsonify, send_from_directory,
    session, redirect, url_for,
)

from scrape_listing import (
    download_as_jpeg,
    extract_details,
    extract_listing_id,
    extract_photo_urls,
    fetch_page_html,
    _first_paragraph,
)

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "dev-only-insecure-key-do-not-use-in-production")
LISTINGS_DIR = Path("listings")
TEMPLATES_DIR = Path("templates")
LISTINGS_DIR.mkdir(exist_ok=True)
TEMPLATES_DIR.mkdir(exist_ok=True)
TEMPLATES_META = TEMPLATES_DIR / "templates_meta.json"

# Shared-password auth for the hosted (multi-teammate) deployment. Unset
# APP_PASSWORD (the default for the local single-user zip install) disables
# auth entirely so existing local installs keep working with no login screen.
APP_PASSWORD = os.environ.get("APP_PASSWORD")

# nodriver launches a real Chrome instance per scrape. Serializing scrapes
# avoids piling up multiple concurrent Chrome processes on a small shared
# server when several teammates click "Get Listing" at the same time.
SCRAPE_LOCK = threading.Lock()

# Distributed-scrape mode: realestate.com.au's KASADA protection hard-blocks
# this server's IP (confirmed — datacenter IP outright, and even a paid
# residential proxy pool, both still blocked; see SPEC.md). Setting
# RELAY_TOKEN switches the frontend to fetch pages via a small local relay
# helper on each teammate's own machine (their IP isn't blocked) instead of
# here — see /api/ingest-listing and relay.py. Unset (default): scrape
# happens on this server as normal, unchanged from the original local
# single-machine design.
RELAY_TOKEN = os.environ.get("RELAY_TOKEN")
RELAY_LOCAL_PORT = os.environ.get("RELAY_LOCAL_PORT", "5051")

IMAGE_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

DEFAULT_TEMPLATE = (
    "Just Listed | {address}\n\n"
    "{description_first_paragraph}\n\n"
    "{features_line}\n\n"
    "See more: {short_url}\n\n"
    "{agent_1_name} | {agent_1_phone}"
)


# ── Template helpers ───────────────────────────────────────────────────────────

def load_meta():
    if TEMPLATES_META.exists():
        try:
            return json.loads(TEMPLATES_META.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {"folders": [], "tags": []}

def save_meta(meta):
    TEMPLATES_META.write_text(json.dumps(meta, indent=2, ensure_ascii=False), encoding="utf-8")

def normalize_agency(name):
    return re.sub(r"[^a-z0-9]", "", (name or "").lower())

def agency_to_id(name):
    return re.sub(r"[^a-z0-9]+", "_", (name or "").lower()).strip("_")

def find_template(agency_name):
    target = normalize_agency(agency_name)
    if not target:
        return None, None
    for path in TEMPLATES_DIR.glob("*.json"):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if normalize_agency(data.get("agency_name", "")) == target:
                return path.stem, data
        except Exception:
            continue
    return None, None

def load_template(template_id):
    path = TEMPLATES_DIR / f"{template_id}.json"
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    return None

def save_template(template_id, data):
    path = TEMPLATES_DIR / f"{template_id}.json"
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")

def all_templates(folder_filter=None, tag_filter=None):
    result = []
    for path in sorted(TEMPLATES_DIR.glob("*.json")):
        if path.name == "templates_meta.json":
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            tags   = data.get("tags") or []
            folder = data.get("folder") or None
            if folder_filter and folder != folder_filter:
                continue
            if tag_filter and tag_filter not in tags:
                continue
            result.append({
                "id":            path.stem,
                "agency_name":   data.get("agency_name", ""),
                "tags":          tags,
                "folder":        folder,
                "shortcut_code": data.get("shortcut_code") or None,
            })
        except Exception:
            pass
    return result


# ── Auth (only active when APP_PASSWORD is set — see above) ────────────────────

LOGIN_HTML = '''<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Listing Tool — Sign in</title>
<style>
*,*::before,*::after{box-sizing:border-box;margin:0;padding:0}
body{font-family:system-ui,-apple-system,sans-serif;background:#f3f4f6;color:#111;min-height:100vh;display:flex;align-items:center;justify-content:center}
.box{background:#fff;border-radius:10px;box-shadow:0 1px 3px rgba(0,0,0,.12);padding:32px 30px;width:100%;max-width:340px}
.box h1{font-size:1.05rem;font-weight:700;margin-bottom:20px;text-align:center}
.box input{width:100%;padding:10px 13px;border:1px solid #d1d5db;border-radius:6px;font-size:.9rem;font-family:inherit;margin-bottom:12px}
.box input:focus{outline:none;border-color:#2563eb;box-shadow:0 0 0 3px rgba(37,99,235,.12)}
.box button{width:100%;padding:10px;background:#2563eb;color:#fff;border:none;border-radius:6px;font-size:.9rem;font-weight:600;cursor:pointer;font-family:inherit}
.box button:hover{background:#1d4ed8}
.err{background:#fef2f2;border:1px solid #fecaca;color:#991b1b;padding:9px 12px;border-radius:6px;font-size:.83rem;margin-bottom:12px}
</style>
</head>
<body>
<form class="box" method="post" action="/login">
<h1>Listing Tool</h1>
{error_block}
<input type="password" name="password" placeholder="Team password" autofocus required>
<button type="submit">Sign in</button>
</form>
</body>
</html>'''


@app.route("/login", methods=["GET", "POST"])
def login():
    if not APP_PASSWORD:
        return redirect(url_for("index"))
    error = ""
    if request.method == "POST":
        submitted = request.form.get("password", "")
        if hmac.compare_digest(submitted, APP_PASSWORD):
            session["authed"] = True
            session.permanent = True
            return redirect(url_for("index"))
        error = '<div class="err">Wrong password.</div>'
    resp = make_response(LOGIN_HTML.replace("{error_block}", error))
    resp.headers["Cache-Control"] = "no-store"
    return resp


@app.route("/logout", methods=["POST"])
def logout():
    session.clear()
    return redirect(url_for("index"))


@app.before_request
def require_login():
    if not APP_PASSWORD:
        return  # auth disabled — local single-user install, unchanged behaviour
    if request.path in ("/login", "/logout", "/api/ingest-listing") or request.path.startswith("/static/"):
        return  # ingest-listing has its own RELAY_TOKEN check — see the route
    if session.get("authed"):
        return
    if request.path.startswith("/api/"):
        return jsonify({"error": "Session expired. Refresh the page and sign in again."}), 401
    return redirect(url_for("login"))


# ── Routes ─────────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    logout_block = (
        '<form method="post" action="/logout" style="margin:0">'
        '<button type="submit" class="btn-logout">Sign out</button></form>'
        if APP_PASSWORD else ""
    )
    relay_url = f"http://localhost:{RELAY_LOCAL_PORT}" if RELAY_TOKEN else ""
    page = HTML_PAGE.replace("{logout_block}", logout_block).replace("{relay_url}", relay_url)
    resp = make_response(page)
    resp.headers["Cache-Control"] = "no-store"
    return resp

def _save_listing_and_build_response(url, details, photo_urls):
    """Persist a scraped listing + download its photos, shared by /api/scrape
    (fetched locally) and /api/ingest-listing (fetched by a remote relay)."""
    listing_id = extract_listing_id(url)
    listing_dir = LISTINGS_DIR / listing_id
    photos_dir  = listing_dir / "photos"

    if listing_dir.exists():
        shutil.rmtree(listing_dir)
    photos_dir.mkdir(parents=True)

    (listing_dir / "listing.json").write_text(
        json.dumps(details, indent=2, ensure_ascii=False), encoding="utf-8"
    )

    img_session = req_lib.Session()
    img_session.headers.update({"User-Agent": IMAGE_UA})
    downloaded = 0
    for i, photo_url in enumerate(photo_urls, 1):
        dest = photos_dir / f"{i:02d}.jpg"
        try:
            download_as_jpeg(photo_url, dest, img_session)
            downloaded += 1
        except Exception:
            pass
        time.sleep(0.4)

    agency_name = details.get("agency_name") or (details.get("agents") or [{}])[0].get("agency")
    template_id, template_data = find_template(agency_name)

    return {
        "listing_id":      listing_id,
        "photo_count":     downloaded,
        "details":         details,
        "template_id":     template_id,
        "template_data":   template_data,
        "template_matched": template_id is not None,
        "agency_name":     agency_name,
    }


@app.route("/api/scrape", methods=["POST"])
def scrape():
    url = (request.get_json() or {}).get("url", "").strip()
    if not url:
        return jsonify({"error": "No URL provided."}), 400
    try:
        extract_listing_id(url)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400

    try:
        try:
            with SCRAPE_LOCK:
                html = fetch_page_html(url)
        except Exception as e:
            return jsonify({"error": f"Failed to load page: {e}"}), 500

        details = extract_details(html, url)
        photo_urls = extract_photo_urls(html)
        return jsonify(_save_listing_and_build_response(url, details, photo_urls))
    except Exception as e:
        import traceback
        app.logger.error(traceback.format_exc())
        return jsonify({
            "error": "Scrape failed",
            "details": str(e),
            "hint": "Check the terminal window for the full traceback",
        }), 500


@app.route("/api/ingest-listing", methods=["POST"])
def ingest_listing():
    """Receives an already-fetched-and-extracted listing from a teammate's
    local relay helper (see relay.py) — used in distributed-scrape mode when
    this server's own IP is blocked by KASADA. Machine-to-machine: auth is a
    shared RELAY_TOKEN, not the browser session cookie (see before_request)."""
    if not RELAY_TOKEN:
        return jsonify({"error": "Relay mode is not enabled on this server."}), 404
    if not hmac.compare_digest(request.headers.get("X-Relay-Token", ""), RELAY_TOKEN):
        return jsonify({"error": "Invalid relay token."}), 403

    body = request.get_json() or {}
    url = (body.get("url") or "").strip()
    details = body.get("details")
    photo_urls = body.get("photo_urls") or []
    if not url or not isinstance(details, dict):
        return jsonify({"error": "Missing url or details."}), 400

    try:
        return jsonify(_save_listing_and_build_response(url, details, photo_urls))
    except Exception as e:
        import traceback
        app.logger.error(traceback.format_exc())
        return jsonify({"error": "Ingest failed", "details": str(e)}), 500

@app.route("/photos/<listing_id>/<filename>")
def serve_photo(listing_id, filename):
    photo_dir = (LISTINGS_DIR / listing_id / "photos").resolve()
    return send_from_directory(str(photo_dir), filename)

@app.route("/api/download/<listing_id>", methods=["POST"])
def download(listing_id):
    data = request.get_json() or {}
    keep = [int(n) for n in data.get("keep", [])]
    photos_dir = LISTINGS_DIR / listing_id / "photos"
    if not photos_dir.exists():
        return jsonify({"error": "Listing not found."}), 404

    # Build address slug for file naming
    address_slug = listing_id
    listing_json = LISTINGS_DIR / listing_id / "listing.json"
    if listing_json.exists():
        try:
            details = json.loads(listing_json.read_text(encoding="utf-8"))
            street = (details.get("address") or "").split(",")[0]
            address_slug = re.sub(r"[^a-z0-9]+", "_", street.lower()).strip("_") or listing_id
        except Exception:
            pass

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for new_idx, photo_num in enumerate(keep, 1):
            src = photos_dir / f"{photo_num:02d}.jpg"
            if src.exists():
                zf.write(str(src), f"{address_slug}_{new_idx:02d}.jpg")
    buf.seek(0)
    resp = make_response(buf.getvalue())
    resp.headers["Content-Type"] = "application/zip"
    resp.headers["Content-Disposition"] = f'attachment; filename="{address_slug}.zip"'
    return resp

@app.route("/api/templates", methods=["GET"])
def get_templates():
    folder = request.args.get("folder") or None
    tag    = request.args.get("tag") or None
    return jsonify(all_templates(folder_filter=folder, tag_filter=tag))

@app.route("/api/templates", methods=["POST"])
def create_template():
    data = request.get_json() or {}
    agency_name = (data.get("agency_name") or "").strip()
    if not agency_name:
        return jsonify({"error": "agency_name is required"}), 400
    template_id = agency_to_id(agency_name)
    tags      = [t.strip() for t in (data.get("tags") or []) if t.strip()]
    folder    = (data.get("folder") or "").strip() or None
    custom_ph = {k: v for k, v in (data.get("custom_placeholders") or {}).items()}
    for key in custom_ph:
        if not re.match(r'^[a-z][a-z0-9_]*$', key):
            return jsonify({"error": f"Placeholder name '{key}' invalid. Use lowercase letters, numbers, and underscores only, starting with a letter."}), 400
    save_template(template_id, {
        "agency_name":         agency_name,
        "caption_template":    data.get("caption_template", DEFAULT_TEMPLATE),
        "default_agents":      data.get("default_agents", []),
        "notes":               data.get("notes", ""),
        "tags":                tags,
        "folder":              folder,
        "custom_placeholders": custom_ph,
        "shortcut_code":       (data.get("shortcut_code") or "").strip().lower() or None,
    })
    if tags:
        meta = load_meta()
        changed = any(t not in meta["tags"] for t in tags)
        if changed:
            for t in tags:
                if t not in meta["tags"]:
                    meta["tags"].append(t)
            save_meta(meta)
    return jsonify({"id": template_id}), 201

@app.route("/api/templates/meta", methods=["GET"])
def get_templates_meta():
    return jsonify(load_meta())

@app.route("/api/templates/folders", methods=["POST"])
def create_folder():
    data = request.get_json() or {}
    name = (data.get("name") or "").strip()
    if not name:
        return jsonify({"error": "name is required"}), 400
    meta = load_meta()
    if name not in meta["folders"]:
        meta["folders"].append(name)
        save_meta(meta)
    return jsonify({"ok": True}), 201

@app.route("/api/templates/folders/<path:name>", methods=["DELETE"])
def delete_folder(name):
    meta = load_meta()
    if name in meta["folders"]:
        meta["folders"].remove(name)
        save_meta(meta)
    for path in TEMPLATES_DIR.glob("*.json"):
        if path.name == "templates_meta.json":
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if data.get("folder") == name:
                data["folder"] = None
                path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
        except Exception:
            pass
    return jsonify({"ok": True})

@app.route("/api/templates/<template_id>", methods=["GET"])
def get_template(template_id):
    data = load_template(template_id)
    if data is None:
        return jsonify({"error": "Not found"}), 404
    return jsonify(data)

@app.route("/api/templates/<template_id>", methods=["PUT"])
def update_template(template_id):
    data     = request.get_json() or {}
    existing = load_template(template_id)
    if existing is None:
        return jsonify({"error": "Not found"}), 404
    tags       = [t.strip() for t in (data["tags"] or []) if t.strip()] \
                 if "tags" in data else existing.get("tags", [])
    folder_raw = data.get("folder") if "folder" in data else existing.get("folder")
    folder     = (folder_raw or "").strip() or None
    custom_ph  = {k: v for k, v in (data.get("custom_placeholders", existing.get("custom_placeholders", {})) or {}).items()}
    for key in custom_ph:
        if not re.match(r'^[a-z][a-z0-9_]*$', key):
            return jsonify({"error": f"Placeholder name '{key}' invalid. Use lowercase letters, numbers, and underscores only, starting with a letter."}), 400
    existing.update({
        "agency_name":         data.get("agency_name",      existing["agency_name"]),
        "caption_template":    data.get("caption_template", existing["caption_template"]),
        "default_agents":      data.get("default_agents",   existing["default_agents"]),
        "notes":               data.get("notes",             existing["notes"]),
        "default_values":      data.get("default_values",   existing.get("default_values", {})),
        "tags":                tags,
        "folder":              folder,
        "custom_placeholders": custom_ph,
        "shortcut_code":       (data.get("shortcut_code") or existing.get("shortcut_code") or "").strip().lower() or None,
    })
    save_template(template_id, existing)
    if tags:
        meta = load_meta()
        changed = any(t not in meta["tags"] for t in tags)
        if changed:
            for t in tags:
                if t not in meta["tags"]:
                    meta["tags"].append(t)
            save_meta(meta)
    return jsonify({"id": template_id})

@app.route("/api/templates/<template_id>", methods=["DELETE"])
def delete_template(template_id):
    path = TEMPLATES_DIR / f"{template_id}.json"
    if path.exists():
        path.unlink()
    return jsonify({"ok": True})


# ── HTML ────────────────────────────────────────────────────────────────────────

HTML_PAGE = '''<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Listing Tool</title>
<style>
*,*::before,*::after{box-sizing:border-box;margin:0;padding:0}
body{font-family:system-ui,-apple-system,sans-serif;background:#f3f4f6;color:#111;min-height:100vh}

/* Topbar */
.topbar{background:#fff;border-bottom:1px solid #e5e7eb;padding:14px 24px;display:flex;align-items:center;gap:14px}
.topbar h1{font-size:1rem;font-weight:700;white-space:nowrap}
.url-form{display:flex;gap:8px;flex:1;max-width:740px}
.url-form input{flex:1;padding:9px 13px;border:1px solid #d1d5db;border-radius:6px;font-size:.9rem;font-family:inherit}
.url-form input:focus{outline:none;border-color:#2563eb;box-shadow:0 0 0 3px rgba(37,99,235,.12)}
.btn-scrape{padding:9px 20px;background:#2563eb;color:#fff;border:none;border-radius:6px;font-size:.9rem;font-weight:600;cursor:pointer;white-space:nowrap;font-family:inherit}
.btn-scrape:disabled{background:#93c5fd;cursor:not-allowed}
.btn-tpl{padding:9px 16px;background:#f3f4f6;color:#374151;border:1px solid #d1d5db;border-radius:6px;font-size:.85rem;font-weight:500;cursor:pointer;white-space:nowrap;font-family:inherit}
.btn-tpl:hover{background:#e5e7eb}
.btn-logout{padding:9px 14px;background:none;color:#6b7280;border:none;font-size:.82rem;cursor:pointer;white-space:nowrap;font-family:inherit;text-decoration:underline}
.btn-logout:hover{color:#374151}
.tpl-select{padding:8px 12px;border:1px solid #d1d5db;border-radius:6px;font-size:.85rem;font-family:inherit;background:#fff;cursor:pointer;min-width:160px;max-width:260px;margin-left:auto}
.tpl-select:focus{outline:none;border-color:#2563eb}

/* Content */
#content{padding:24px;max-width:1200px;margin:0 auto}

/* States */
#state-idle{text-align:center;padding:80px 20px;color:#9ca3af;font-size:.95rem}
#state-loading{text-align:center;padding:80px 20px;display:none}
.spinner{width:40px;height:40px;border:3px solid #e5e7eb;border-top-color:#2563eb;border-radius:50%;animation:spin .7s linear infinite;margin:0 auto 20px}
@keyframes spin{to{transform:rotate(360deg)}}
.loading-msg{font-size:.95rem;color:#374151;font-weight:500}
.loading-sub{font-size:.82rem;color:#9ca3af;margin-top:8px}
#state-error{text-align:center;padding:60px 20px;display:none}
.error-box{display:inline-block;background:#fef2f2;border:1px solid #fecaca;color:#991b1b;padding:14px 20px;border-radius:8px;font-size:.9rem}
#state-result{display:none}

/* Banner */
#no-tpl-banner{display:none;background:#fffbeb;border:1px solid #fcd34d;border-radius:7px;padding:12px 16px;margin-bottom:18px;font-size:.87rem;color:#92400e;align-items:center;gap:8px}
#no-tpl-banner a{color:#b45309;font-weight:600;cursor:pointer;text-decoration:underline}

/* Listing header */
.listing-header{margin-bottom:18px}
.listing-header h2{font-size:1.2rem;font-weight:700}
.pills{display:flex;gap:8px;margin-top:8px;flex-wrap:wrap}
.pill{background:#e5e7eb;border-radius:20px;padding:4px 12px;font-size:.8rem;font-weight:500;color:#374151}
.pill.price{background:#dcfce7;color:#166534}

/* Photo grid */
.photo-controls{display:flex;gap:8px;margin-bottom:14px;align-items:center}
.sel-count{font-size:.85rem;color:#6b7280;margin-left:4px}
.btn{padding:7px 14px;border:none;border-radius:5px;font-size:.85rem;cursor:pointer;font-family:inherit}
.btn-ghost{background:#e5e7eb;color:#374151}
.btn-ghost:hover{background:#d1d5db}
.btn-primary{background:#2563eb;color:#fff;font-weight:600}
.btn-primary:hover{background:#1d4ed8}
.btn-green{background:#16a34a;color:#fff;font-weight:600}
.btn-green:hover{background:#15803d}
.btn-green:disabled{background:#86efac;cursor:not-allowed}
.btn-red{background:#dc2626;color:#fff;font-weight:600}
.btn-red:hover{background:#b91c1c}
.btn-sm{padding:5px 10px;font-size:.8rem}

.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(190px,1fr));gap:10px;margin-bottom:28px}
.photo-item label{display:block;position:relative;cursor:pointer;border-radius:7px;overflow:hidden;box-shadow:0 1px 3px rgba(0,0,0,.12)}
.photo-item img{display:block;width:100%;aspect-ratio:4/3;object-fit:cover}
.photo-item input[type=checkbox]{position:absolute;top:8px;left:8px;width:18px;height:18px;cursor:pointer;z-index:1;accent-color:#2563eb}
.photo-item .num{position:absolute;bottom:7px;right:8px;background:rgba(0,0,0,.52);color:#fff;font-size:.72rem;padding:2px 7px;border-radius:3px;pointer-events:none}
.photo-item .badge{display:none;position:absolute;top:8px;right:8px;background:#2563eb;color:#fff;font-size:.75rem;font-weight:700;width:24px;height:24px;border-radius:50%;align-items:center;justify-content:center;pointer-events:none}

/* Cards */
.card{background:#fff;border-radius:8px;padding:20px 22px;box-shadow:0 1px 3px rgba(0,0,0,.08);margin-bottom:20px}
.card-header{display:flex;align-items:center;justify-content:space-between;margin-bottom:14px}
.card-header h3{font-size:.93rem;font-weight:600}
.tpl-label{font-size:.78rem;color:#6b7280}
.tpl-label span{color:#2563eb;font-weight:500}

.url-row{display:flex;align-items:center;gap:10px;margin-bottom:12px}
.url-row label{font-size:.85rem;color:#555;white-space:nowrap}
.url-row input{flex:1;padding:8px 11px;border:1px solid #d1d5db;border-radius:5px;font-size:.85rem;font-family:inherit}
.url-row input:focus{outline:none;border-color:#2563eb}
#caption-text{width:100%;min-height:190px;padding:12px 14px;border:1px solid #d1d5db;border-radius:6px;font-size:.88rem;font-family:inherit;line-height:1.65;resize:vertical;color:#111;background:transparent;position:relative;z-index:1}
#caption-text:focus{outline:none;border-color:#2563eb}
.caption-actions{display:flex;gap:8px;margin-top:10px}

/* Two-column layout */
.result-cols{display:grid;grid-template-columns:5fr 6fr;gap:16px;align-items:start;margin-bottom:20px}
.col-right{display:flex;flex-direction:column;gap:0}

/* Scraped content panel */
#scraped-content{font-size:.85rem;line-height:1.75;color:#374151;white-space:pre-wrap;max-height:560px;overflow-y:auto;cursor:text;user-select:text}
.sc-sec{margin-bottom:16px}
.sc-sec-lbl{font-size:.7rem;font-weight:700;color:#9ca3af;text-transform:uppercase;letter-spacing:.07em;margin-bottom:5px}

/* Custom fields */
.cf-row{margin-bottom:12px}
.cf-row-hd{display:flex;align-items:center;justify-content:space-between;margin-bottom:4px}
.cf-row-lbl{font-size:.8rem;font-weight:600;color:#374151;text-transform:capitalize}
.cf-clear{font-size:.75rem;color:#9ca3af;cursor:pointer;background:none;border:none;padding:0;font-family:inherit}
.cf-clear:hover{color:#dc2626}
.cf-ta{width:100%;padding:8px 10px;border:1px solid #d1d5db;border-radius:5px;font-size:.84rem;font-family:inherit;resize:vertical;line-height:1.5;color:#111;min-height:58px}
.cf-ta:focus{outline:none;border-color:#2563eb;box-shadow:0 0 0 2px rgba(37,99,235,.1)}
.cf-ta.filled{border-color:#16a34a;background:#f0fdf4}

/* Assign tooltip */
#assign-tip{display:none;position:fixed;z-index:500;background:#1e293b;border-radius:8px;padding:8px 12px;box-shadow:0 4px 24px rgba(0,0,0,.35);align-items:center;gap:8px}
#assign-tip span{font-size:.78rem;color:#94a3b8;white-space:nowrap}
#assign-tip select{padding:5px 8px;border-radius:4px;font-size:.8rem;border:none;background:#334155;color:#fff;font-family:inherit;max-width:170px;cursor:pointer}
#assign-tip .btn-do{padding:5px 12px;background:#2563eb;color:#fff;border:none;border-radius:4px;font-size:.8rem;font-weight:600;cursor:pointer;font-family:inherit}
#assign-tip .btn-x{background:none;border:none;color:#64748b;cursor:pointer;font-size:1.1rem;padding:0 3px;line-height:1}

.download-bar{display:flex;align-items:center;gap:14px;background:#fff;border-radius:8px;padding:18px 22px;box-shadow:0 1px 3px rgba(0,0,0,.08)}
.dl-status{font-size:.88rem;color:#6b7280}


/* Drawer */
#overlay{display:none;position:fixed;inset:0;background:rgba(0,0,0,.35);z-index:100}
#drawer{position:fixed;top:0;right:-520px;width:520px;height:100vh;background:#fff;box-shadow:-4px 0 24px rgba(0,0,0,.12);z-index:101;display:flex;flex-direction:column;transition:right .25s ease;overflow:hidden}
#drawer.open{right:0}
.dw-header{display:flex;align-items:center;gap:10px;padding:18px 20px;border-bottom:1px solid #e5e7eb;flex-shrink:0}
.dw-header h2{font-size:1rem;font-weight:700;flex:1}
.dw-close{background:none;border:none;cursor:pointer;font-size:1.3rem;color:#6b7280;line-height:1;padding:2px 6px}
.dw-close:hover{color:#111}
.dw-back{background:none;border:none;cursor:pointer;font-size:.85rem;color:#2563eb;padding:0;font-family:inherit}
.dw-back:hover{text-decoration:underline}
.dw-body{flex:1;overflow-y:auto;padding:20px}
.dw-footer{padding:14px 20px;border-top:1px solid #e5e7eb;display:flex;gap:8px;flex-shrink:0}

/* Template list */
.tpl-empty{color:#9ca3af;font-size:.9rem;padding:20px 0}
.tpl-item{display:flex;align-items:center;padding:13px 14px;border:1px solid #e5e7eb;border-radius:7px;margin-bottom:8px;gap:12px}
.tpl-item-name{flex:1;font-size:.9rem;font-weight:500}

/* Edit form */
.form-group{margin-bottom:16px}
.form-group label{display:block;font-size:.83rem;font-weight:500;color:#374151;margin-bottom:5px}
.form-group input,.form-group textarea{width:100%;padding:8px 11px;border:1px solid #d1d5db;border-radius:5px;font-size:.88rem;font-family:inherit}
.form-group input:focus,.form-group textarea:focus{outline:none;border-color:#2563eb}
.form-group textarea{resize:vertical;line-height:1.5}
#tpl-body{min-height:180px;font-family:monospace;font-size:.82rem;background:transparent;position:relative;z-index:1;color:#111}
.ph-ref{background:#f9fafb;border:1px solid #e5e7eb;border-radius:5px;padding:10px 12px;margin-top:6px;font-size:.78rem;color:#6b7280;line-height:1.7}
.ph-ref code{background:#e5e7eb;padding:1px 4px;border-radius:3px;color:#374151}
.agent-row{display:flex;gap:8px;align-items:center;margin-bottom:8px}
.agent-row input{flex:1}
.btn-rm{background:none;border:none;cursor:pointer;color:#dc2626;font-size:1.1rem;padding:0 4px}
.btn-rm:hover{color:#991b1b}
.preview-label{font-size:.8rem;font-weight:500;color:#6b7280;margin-bottom:6px;display:flex;align-items:center;gap:8px}
.preview-box{background:#f9fafb;border:1px solid #e5e7eb;border-radius:6px;padding:12px 14px;font-size:.85rem;line-height:1.65;white-space:pre-wrap;color:#374151;min-height:80px}

/* Folder sidebar */
#folder-sidebar{width:150px;flex-shrink:0;overflow-y:auto;padding:10px 6px;border-right:1px solid #e5e7eb;display:flex;flex-direction:column;gap:1px}
.folder-item{display:flex;align-items:center;padding:7px 9px;border-radius:5px;cursor:pointer;font-size:.82rem;color:#374151;gap:4px;user-select:none}
.folder-item:hover{background:#f3f4f6}
.folder-item.active{background:#eff6ff;color:#2563eb;font-weight:600}
.folder-item-name{flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;min-width:0}
.folder-count{font-size:.74rem;color:#9ca3af;flex-shrink:0}
.folder-item.active .folder-count{color:#93c5fd}
.folder-del{display:none;background:none;border:none;cursor:pointer;color:#9ca3af;font-size:.9rem;padding:0 2px;line-height:1;flex-shrink:0}
.folder-del:hover{color:#dc2626}
.folder-item:hover .folder-del{display:inline}
.folder-new{margin-top:auto;padding:7px 9px;font-size:.81rem;color:#6b7280;cursor:pointer;background:none;border:none;font-family:inherit;text-align:left;border-radius:5px;width:100%}
.folder-new:hover{color:#2563eb;background:#f3f4f6}
/* Tag chips */
.tpl-tags{margin-top:5px;line-height:1.4}
.tag-chip{display:inline-block;background:#f3f4f6;color:#374151;border-radius:3px;padding:2px 7px;font-size:.72rem;font-weight:500;margin:1px 2px 1px 0}
.tag-chip-more{font-size:.72rem;color:#9ca3af;margin-left:2px}
/* Template list item alignment when tags are present */
.tpl-item{align-items:flex-start}
#list-body{padding:14px}
/* Custom placeholders drawer section */
.cp-row{display:flex;gap:8px;align-items:center;margin-bottom:8px}
.cp-name{width:120px;flex-shrink:0;padding:7px 9px;border:1px solid #d1d5db;border-radius:5px;font-size:.84rem;font-family:monospace}
.cp-name:focus{outline:none;border-color:#2563eb}
.cp-value{flex:1;padding:7px 9px;border:1px solid #d1d5db;border-radius:5px;font-size:.84rem;font-family:inherit}
.cp-value:focus{outline:none;border-color:#2563eb}
.cf-tpl-badge{font-size:.68rem;background:#eff6ff;color:#2563eb;border:1px solid #bfdbfe;border-radius:3px;padding:1px 5px;font-weight:500;flex-shrink:0}
/* ── Authoring UX ── */
.tpl-body-wrap{position:relative;display:block}
#tpl-body-backdrop{position:absolute;inset:0;overflow:hidden;pointer-events:none;border-radius:5px}
#tpl-body-highlights{font-family:monospace;font-size:.82rem;line-height:1.5;padding:8px 11px;border:1px solid transparent;border-radius:5px;white-space:pre-wrap;word-break:break-word;word-wrap:break-word;color:transparent;box-sizing:border-box}
.ph-hl{background:#dbeafe;border-radius:2px}
#ph-autocomplete{display:none;position:absolute;z-index:200;background:#fff;border:1px solid #d1d5db;border-radius:6px;box-shadow:0 4px 12px rgba(0,0,0,.15);width:224px;max-height:216px;overflow-y:auto}
#ph-ac-list{list-style:none;margin:0;padding:4px 0}
#ph-ac-list li{padding:5px 10px;font-size:.82rem;cursor:pointer;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
#ph-ac-list li:hover,#ph-ac-list li.ac-active{background:#eff6ff;color:#1d4ed8}
.ph-ac-sep{height:1px;background:#e5e7eb;margin:4px 0;padding:0;pointer-events:none}
#ph-warnings{font-size:.78rem;color:#6b7280;margin-top:4px}
.ph-warn-item{color:#2563eb;cursor:pointer;text-decoration:underline;text-underline-offset:2px}
.ph-unknown{color:#dc2626;text-decoration:line-through}
/* Template Picker */
#picker-overlay{display:none;position:fixed;inset:0;background:rgba(0,0,0,.45);backdrop-filter:blur(2px);z-index:200;align-items:center;justify-content:center}
#picker-overlay.open{display:flex}
#picker-modal{background:#fff;border-radius:10px;width:480px;max-width:calc(100vw - 32px);max-height:60vh;display:flex;flex-direction:column;box-shadow:0 8px 40px rgba(0,0,0,.28);overflow:hidden}
#picker-search{padding:14px 16px;border:none;border-bottom:1px solid #e5e7eb;font-size:.93rem;font-family:inherit;outline:none;width:100%;color:#111;background:#fff}
#picker-list{list-style:none;overflow-y:auto;flex:1;padding:4px 0;margin:0}
.picker-item{padding:10px 16px;cursor:pointer;display:flex;align-items:flex-start;justify-content:space-between;gap:12px}
.picker-item:hover,.picker-item.pk-active{background:#eff6ff}
.picker-item-info{flex:1;min-width:0}
.picker-item-name{font-size:.9rem;font-weight:500;color:#111}
.picker-item-folder{font-size:.76rem;color:#9ca3af;margin-top:2px}
.picker-item-hint{font-size:.72rem;color:#9ca3af;padding-top:3px;flex-shrink:0;display:none;white-space:nowrap}
.picker-item.pk-active .picker-item-hint{display:block}
#picker-empty{padding:28px 16px;text-align:center;color:#9ca3af;font-size:.88rem;display:none;flex-direction:column;align-items:center;gap:10px}
#picker-empty.visible{display:flex}
.picker-create-btn{background:none;border:1px solid #d1d5db;border-radius:5px;padding:6px 14px;font-size:.82rem;color:#374151;cursor:pointer;font-family:inherit}
.picker-create-btn:hover{background:#f3f4f6;border-color:#9ca3af}
/* Shortcode hint */
.caption-wrap{position:relative}
#sc-hint{display:none;position:absolute;z-index:150;background:#fff;border:1px solid #e5e7eb;border-radius:6px;box-shadow:0 4px 16px rgba(0,0,0,.12);min-width:220px;max-width:320px;overflow:hidden}
.sc-item{display:flex;align-items:center;gap:8px;padding:9px 13px;cursor:pointer;font-size:.84rem;color:#374151}
.sc-item:hover,.sc-item.sc-active{background:#eff6ff}
.sc-code{font-family:monospace;font-size:.78rem;color:#2563eb;background:#dbeafe;border-radius:3px;padding:1px 6px;flex-shrink:0}
.sc-name{flex:1;min-width:0;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.sc-enter{font-size:.72rem;color:#9ca3af;flex-shrink:0}
/* Caption fill overlay */
#cap-backdrop{position:absolute;inset:0;overflow:hidden;pointer-events:none;border-radius:6px}
#cap-highlights{font-family:inherit;font-size:.88rem;line-height:1.65;padding:12px 14px;border:1px solid transparent;border-radius:6px;white-space:pre-wrap;word-break:break-word;word-wrap:break-word;color:transparent;box-sizing:border-box}
.cap-fillable{background:#fef3c7;border-radius:2px}
#fill-hint{display:none;font-size:.78rem;color:#6b7280;margin-bottom:6px}
</style>
</head>
<body>

<div class="topbar">
  <h1>Listing Tool</h1>
  <div class="url-form">
    <input type="text" id="url-input" placeholder="Paste a realestate.com.au listing URL...">
    <button class="btn-scrape" id="scrape-btn" onclick="startScrape()">Get Listing</button>
  </div>
  <select id="tpl-select" class="tpl-select" onchange="onTemplateSelect()">
    <option value="">Auto-detect template</option>
  </select>
  <button class="btn-tpl" onclick="openDrawer()">&#9776; Agency Templates</button>
  {logout_block}
</div>

<div id="content">
  <div id="state-idle">Paste a listing URL above to get started.</div>

  <div id="state-loading">
    <div class="spinner"></div>
    <p class="loading-msg">Scraping listing&hellip;</p>
    <p class="loading-sub">A browser window will open briefly &mdash; this is normal.</p>
  </div>

  <div id="state-error">
    <div class="error-box" id="error-msg"></div>
  </div>

  <div id="state-result">
    <div id="no-tpl-banner">
      <span>&#9888;</span>
      <span>No saved template for <strong id="banner-agency"></strong>. <a onclick="openDrawerNew()">Save as new template</a></span>
    </div>

    <div class="listing-header">
      <h2 id="result-address"></h2>
      <p id="result-agency" style="font-size:.82rem;color:#6b7280;margin-top:3px"></p>
      <div class="pills" id="result-pills"></div>
    </div>

    <div class="photo-controls">
      <button class="btn btn-ghost" onclick="selectAll()">Select All</button>
      <button class="btn btn-ghost" onclick="deselectAll()">Deselect All</button>
      <span class="sel-count" id="sel-count">0 selected</span>
    </div>
    <div class="grid" id="photo-grid"></div>

    <div class="result-cols">
      <div class="card" style="margin-bottom:0">
        <div class="card-header">
          <h3>Scraped Content</h3>
          <span style="font-size:.75rem;color:#9ca3af">Highlight text &#8594; assign to placeholder</span>
        </div>
        <div id="scraped-content"></div>
      </div>
      <div class="col-right">
        <div class="card" id="custom-fields-card" style="display:none;margin-bottom:16px">
          <div class="card-header">
            <h3>Custom Fields</h3>
            <span style="font-size:.75rem;color:#9ca3af">Highlight from left panel to fill</span>
          </div>
          <div id="custom-fields-body"></div>
        </div>
        <div class="card" style="margin-bottom:0">
          <div class="card-header">
            <h3>Caption</h3>
            <span class="tpl-label" id="tpl-label"></span>
          </div>
          <div class="url-row">
            <label for="short-url">Short URL</label>
            <input type="text" id="short-url" placeholder="https://bit.ly/..." oninput="updateCaption()">
          </div>
          <div id="fill-hint"></div>
          <div class="caption-wrap">
            <div id="cap-backdrop" aria-hidden="true">
              <div id="cap-highlights"></div>
            </div>
            <textarea id="caption-text" spellcheck="true"></textarea>
            <div id="sc-hint"></div>
          </div>
          <div class="caption-actions">
            <button class="btn btn-ghost" onclick="copyCaption()">Copy Caption</button>
            <button class="btn btn-ghost" id="btn-save-defaults" style="display:none" onclick="saveDefaultsToTemplate()">Save to Template</button>
          </div>
        </div>
      </div>
    </div>

    <div class="download-bar">
      <button class="btn btn-green" id="dl-btn" onclick="downloadPhotos()">Download Selected Photos</button>
      <span class="dl-status" id="dl-status"></span>
    </div>
  </div>
</div>

<!-- Assign tooltip -->
<div id="assign-tip">
  <span>Assign to:</span>
  <select id="assign-sel" onchange="onAssignSelChange()"></select>
  <input id="assign-new-name" type="text" placeholder="placeholder name..." style="display:none;padding:5px 8px;border-radius:4px;font-size:.8rem;border:none;background:#334155;color:#fff;font-family:inherit;width:130px">
  <button class="btn-do" onclick="assignSelection()">Assign</button>
  <button class="btn-x" onclick="hideAssignTip()">&#215;</button>
</div>

<!-- Drawer -->
<div id="overlay" onclick="closeDrawer()"></div>
<div id="drawer">

  <!-- List view -->
  <div id="view-list" style="display:flex;flex-direction:column;height:100%">
    <div class="dw-header">
      <h2>Agency Templates</h2>
      <button class="dw-close" onclick="closeDrawer()">&times;</button>
    </div>
    <div style="display:flex;flex:1;overflow:hidden">
      <div id="folder-sidebar"></div>
      <div class="dw-body" id="list-body"></div>
    </div>
    <div class="dw-footer">
      <button class="btn btn-primary" onclick="openDrawerNew()">+ New Template</button>
    </div>
  </div>

  <!-- Edit view -->
  <div id="view-edit" style="display:none;flex-direction:column;height:100%">
    <div class="dw-header">
      <button class="dw-back" onclick="showList()">&#8592; Back</button>
      <h2 id="edit-title">New Template</h2>
      <button class="dw-close" onclick="closeDrawer()">&times;</button>
    </div>
    <div class="dw-body">
      <input type="hidden" id="tpl-id">
      <div class="form-group">
        <label>Agency Name</label>
        <input type="text" id="tpl-agency" placeholder="e.g. McGrath Pittwater - Avalon Beach" oninput="refreshPreview()">
      </div>
      <div class="form-group">
        <label>Caption Template</label>
        <div class="tpl-body-wrap">
          <div id="tpl-body-backdrop" aria-hidden="true">
            <div id="tpl-body-highlights"></div>
          </div>
          <textarea id="tpl-body" rows="10" spellcheck="false" oninput="onTplBodyInput()"></textarea>
          <div id="ph-autocomplete">
            <ul id="ph-ac-list"></ul>
          </div>
        </div>
        <div id="ph-warnings"></div>
        <div class="ph-ref">
          <strong>Auto-filled:</strong><br>
          <code>{address}</code> <code>{beds}</code> <code>{baths}</code> <code>{cars}</code> <code>{land_size}</code> <code>{features_line}</code><br>
          <code>{description_first_paragraph}</code> <code>{description_full}</code><br>
          <code>{price}</code> <code>{auction_date}</code> <code>{short_url}</code> <code>{agency_name}</code><br>
          <code>{agent_1_name}</code> <code>{agent_1_phone}</code> <code>{agent_2_name}</code> <code>{agent_2_phone}</code><br><br>
          <strong>Custom:</strong> Add any <code>{your_placeholder}</code> &mdash; you will fill it manually by highlighting text after scraping.
        </div>
      </div>
      <div class="form-group">
        <label>Default Agents <span style="font-weight:400;color:#9ca3af">(overrides scraped agents)</span></label>
        <div id="agents-list"></div>
        <button class="btn btn-ghost btn-sm" style="margin-top:6px" onclick="addAgent()">+ Add Agent</button>
      </div>
      <div class="form-group">
        <label>Custom Placeholders <span style="font-weight:400;color:#9ca3af">(optional)</span></label>
        <div id="cp-list" style="margin-bottom:4px"></div>
        <button class="btn btn-ghost btn-sm" onclick="addCPRow('','')">+ Add Placeholder</button>
      </div>
      <div class="form-group">
        <label>Notes <span style="font-weight:400;color:#9ca3af">(optional)</span></label>
        <textarea id="tpl-notes" rows="2"></textarea>
      </div>
      <div class="form-group">
        <label>Folder <span style="font-weight:400;color:#9ca3af">(optional)</span></label>
        <select id="tpl-folder"><option value="">None</option></select>
      </div>
      <div class="form-group">
        <label>Tags <span style="font-weight:400;color:#9ca3af">(comma-separated, optional)</span></label>
        <input type="text" id="tpl-tags" placeholder="e.g. mcgrath, house, premium">
      </div>
      <div class="form-group">
        <label>Shortcut Code <span style="font-weight:400;color:#9ca3af">(optional — type ;code in the caption to apply)</span></label>
        <input type="text" id="tpl-shortcut" placeholder="e.g. jl" style="font-family:monospace;max-width:120px" autocomplete="off" autocorrect="off" autocapitalize="off" spellcheck="false">
      </div>
      <div class="form-group">
        <p class="preview-label">Live Preview <span style="font-weight:400;color:#9ca3af">(uses current listing)</span><button onclick="refreshPreview()" class="btn btn-ghost btn-sm" style="margin-left:auto">&#8635; Refresh</button></p>
        <div class="preview-box" id="live-preview">Load a listing first to see a preview.</div>
      </div>
    </div>
    <div class="dw-footer">
      <button class="btn btn-primary" onclick="saveTemplate()">Save Template</button>
      <button class="btn btn-ghost" onclick="closeDrawer()">Cancel</button>
      <button class="btn btn-red btn-sm" id="btn-delete" style="display:none;margin-left:auto" onclick="deleteTemplate()">Delete</button>
    </div>
  </div>

</div>

<!-- Template Picker (Ctrl+/) -->
<div id="picker-overlay" onclick="pickerBackdropClick(event)">
  <div id="picker-modal" role="dialog" aria-modal="true" aria-label="Template picker">
    <input id="picker-search" type="text" autocomplete="off" spellcheck="false"
           placeholder="Search templates by name, folder, or tag...">
    <ul id="picker-list"></ul>
    <div id="picker-empty">
      <span>No templates match</span>
      <button id="picker-create-btn" class="picker-create-btn" onclick="closePicker();openDrawerNew()">Create your first template</button>
    </div>
  </div>
</div>

<script>
// ── State ────────────────────────────────────────────────────────────────────
// Set server-side when this server runs in distributed-scrape mode (RELAY_TOKEN
// configured): the actual page-fetch happens on a local relay helper on the
// teammate's own machine (their IP isn't blocked by KASADA) instead of here.
// Empty string means scrape locally as normal, same as always.
const RELAY_URL          = '{relay_url}';
let listing             = null;
let tplId               = null;
let tplData             = null;
let selOrder            = [];
let preSelectedTemplate = null;
let customValues        = {};
let pendingSelText      = '';
const NL                = String.fromCharCode(10);
let activeFolderName    = '';
let templateMeta        = {folders: [], tags: []};
let allTplList          = [];
let pickerOpen    = false;
let pickerAllTpls = [];
let pickerItems   = [];
let pickerActive  = -1;
let captionDirty  = false;
let scodeMap    = {};
let scodeItems  = [];
let scodeActive = 0;
let scodeStart  = -1;

const BUILTIN_PH_LIST = [
  'address','beds','baths','cars','land_size','property_type','features_line',
  'description_first_paragraph','description_full','price','auction_date',
  'short_url','agency_name','agent_1_name','agent_1_phone','agent_2_name','agent_2_phone'
];
const STANDARD_PH = new Set(BUILTIN_PH_LIST);

// ── Authoring UX state ────────────────────────────────────────────────────────
let recentPH  = [];
let acActive  = -1;
let warnTimer = null;
let acTrigPos = -1;

const DEFAULT_TPL = `Just Listed | {address}

{description_first_paragraph}

{features_line}

See more: {short_url}

{agent_1_name} | {agent_1_phone}`;

// ── Scrape ───────────────────────────────────────────────────────────────────
async function startScrape() {
  const url = document.getElementById('url-input').value.trim();
  if (!url) { alert('Please paste a listing URL first.'); return; }
  setState('loading');
  document.getElementById('scrape-btn').disabled = true;
  try {
    const endpoint = RELAY_URL ? (RELAY_URL + '/api/scrape') : '/api/scrape';
    const r = await fetch(endpoint, {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({url}),
    });
    let data;
    try {
      data = await r.json();
    } catch (parseErr) {
      showError('Server error (HTTP ' + r.status + '). Check the terminal window for the full error.');
      return;
    }
    if (!r.ok || data.error) {
      let msg = data.error || ('Request failed (HTTP ' + r.status + ').');
      if (data.details) msg += NL + NL + 'Details: ' + data.details;
      if (data.hint)    msg += NL + NL + data.hint;
      showError(msg);
      return;
    }
    renderResult(data);
  } catch(e) {
    const msg = RELAY_URL
      ? 'Cannot reach your local Listing Tool Relay helper at ' + RELAY_URL + '. Make sure it is running on your machine. (' + e.message + ')'
      : 'Cannot reach server. Make sure app is running. (' + e.message + ')';
    showError(msg);
  } finally {
    document.getElementById('scrape-btn').disabled = false;
  }
}

// ── Render result ────────────────────────────────────────────────────────────
function renderResult(data) {
  listing = data;
  // Pre-selected template overrides the auto-matched one
  if (preSelectedTemplate) {
    tplId   = preSelectedTemplate.id;
    tplData = preSelectedTemplate.data;
  } else {
    tplId   = data.template_id   || null;
    tplData = data.template_data || null;
  }

  // Show agency name for double-checking
  document.getElementById('result-agency').textContent =
    data.agency_name ? 'Agency: ' + data.agency_name : '';

  const d = data.details;
  document.getElementById('result-address').textContent = d.address || '';

  const pills = document.getElementById('result-pills');
  pills.innerHTML = '';
  const pill = (t, cls) => {
    if (t == null || t === '') return;
    const p = document.createElement('span');
    p.className = 'pill' + (cls ? ' ' + cls : '');
    p.textContent = t;
    pills.appendChild(p);
  };
  pill(d.bedrooms   != null ? d.bedrooms   + ' Bed'  : null);
  pill(d.bathrooms  != null ? d.bathrooms  + ' Bath' : null);
  pill(d.car_spaces != null ? d.car_spaces + ' Car'  : null);
  pill(d.land_size);
  pill(d.price,   'price');
  pill(d.auction  ? 'Auction: ' + d.auction : null);

  // Banner
  const banner = document.getElementById('no-tpl-banner');
  if (!data.template_matched && data.agency_name) {
    document.getElementById('banner-agency').textContent = data.agency_name;
    banner.style.display = 'flex';
  } else {
    banner.style.display = 'none';
  }

  // Template label
  const lbl = document.getElementById('tpl-label');
  lbl.innerHTML = tplData
    ? 'Template: <span>' + escHtml(tplData.agency_name) + '</span>'
    : 'Template: <span>Default</span>';

  // Photos
  selOrder = [];
  const grid = document.getElementById('photo-grid');
  grid.innerHTML = '';
  for (let i = 1; i <= data.photo_count; i++) {
    const pad = String(i).padStart(2, '0');
    const div = document.createElement('div');
    div.className = 'photo-item';
    div.innerHTML =
      '<label>' +
        '<input type="checkbox" value="' + i + '" onchange="togglePhoto(this)">' +
        '<img src="/photos/' + data.listing_id + '/' + pad + '.jpg" loading="lazy" alt="Photo ' + i + '">' +
        '<span class="num">' + i + '</span>' +
        '<span class="badge"></span>' +
      '</label>';
    grid.appendChild(div);
  }
  updateSelCount();
  document.getElementById('short-url').value = '';
  customValues = Object.assign({}, tplData && tplData.default_values || {});
  renderScrapedContent();
  renderCustomFields();
  updateCaption();
  setState('result');
}

// ── Custom placeholders ───────────────────────────────────────────────────────
function getCustomPH(tpl) {
  const re = /\{([a-z_0-9]+)\}/g, seen = new Set(), out = [];
  let m;
  while ((m = re.exec(tpl)) !== null)
    if (!STANDARD_PH.has(m[1]) && !seen.has(m[1])) { seen.add(m[1]); out.push(m[1]); }
  return out;
}

function renderCustomFields() {
  const tpl    = tplData ? tplData.caption_template : DEFAULT_TPL;
  const custom = getCustomPH(tpl);
  const savedCP = (tplData && tplData.custom_placeholders) || {};
  const card   = document.getElementById('custom-fields-card');
  const body   = document.getElementById('custom-fields-body');
  body.innerHTML = '';
  // All names to show: template custom PH + any standard PH the user manually assigned
  const extraKeys = Object.keys(customValues).filter(function(k) { return custom.indexOf(k) === -1; });
  const allNames  = custom.concat(extraKeys);
  if (!allNames.length) { card.style.display = 'none'; updateAssignSel(custom); return; }
  card.style.display = 'block';
  allNames.forEach(function(name) {
    // Determine source: user override takes priority over template-saved value
    const hasUserOverride  = customValues.hasOwnProperty(name);
    const savedVal         = savedCP.hasOwnProperty(name) ? savedCP[name] : null;
    const showFromTemplate = !hasUserOverride && savedVal;
    const displayValue     = hasUserOverride ? customValues[name] : (savedVal || '');

    const row = document.createElement('div');
    row.className = 'cf-row';
    const hd = document.createElement('div');
    hd.className = 'cf-row-hd';
    const lblWrap = document.createElement('div');
    lblWrap.style.cssText = 'display:flex;align-items:center;gap:6px';
    const lbl = document.createElement('span');
    lbl.className = 'cf-row-lbl';
    lbl.textContent = name.replace(/_/g, ' ');
    lblWrap.appendChild(lbl);
    if (showFromTemplate) {
      const badge = document.createElement('span');
      badge.className = 'cf-tpl-badge';
      badge.textContent = 'from template';
      lblWrap.appendChild(badge);
    }
    const clr = document.createElement('button');
    clr.className = 'cf-clear';
    clr.textContent = 'clear';
    clr.addEventListener('click', function() { clearCustomField(name); });
    hd.appendChild(lblWrap); hd.appendChild(clr);
    const ta = document.createElement('textarea');
    ta.className = 'cf-ta' + (displayValue ? ' filled' : '');
    ta.rows = 2;
    ta.placeholder = 'Highlight text on the left to fill → or type here';
    ta.dataset.key = name;
    ta.value = displayValue;
    ta.addEventListener('input', function() {
      customValues[name] = this.value;
      this.className = 'cf-ta' + (this.value ? ' filled' : '');
      updateCaption();
    });
    row.appendChild(hd); row.appendChild(ta);
    body.appendChild(row);
  });
  updateAssignSel(custom);
}

function clearCustomField(name) {
  delete customValues[name];
  updateCaption();
  renderCustomFields();
}

function updateAssignSel(custom) {
  const sel = document.getElementById('assign-sel');
  let html = '';
  if (custom.length) {
    html += '<optgroup label="Custom (from template)">';
    custom.forEach(function(c) { html += '<option value="' + c + '">' + c.replace(/_/g,' ') + '</option>'; });
    html += '</optgroup>';
  }
  html += '<optgroup label="Standard (override auto-fill)">'
    + '<option value="description_first_paragraph">Description (first para)</option>'
    + '<option value="description_full">Description (full)</option>'
    + '<option value="address">Address</option>'
    + '<option value="features_line">Features line</option>'
    + '<option value="price">Price</option>'
    + '<option value="auction_date">Auction date</option>'
    + '<option value="agent_1_name">Agent 1 name</option>'
    + '<option value="agent_1_phone">Agent 1 phone</option>'
    + '<option value="agent_2_name">Agent 2 name</option>'
    + '<option value="agent_2_phone">Agent 2 phone</option>'
    + '<option value="agency_name">Agency name</option>'
    + '</optgroup>'
    + '<option value="__new__">+ New placeholder...</option>';
  sel.innerHTML = html;
  document.getElementById('assign-new-name').style.display = 'none';
}

function onAssignSelChange() {
  const isNew = document.getElementById('assign-sel').value === '__new__';
  document.getElementById('assign-new-name').style.display = isNew ? 'inline-block' : 'none';
  if (isNew) document.getElementById('assign-new-name').focus();
}

function renderScrapedContent() {
  if (!listing) return;
  const d  = listing.details;
  const el = document.getElementById('scraped-content');
  el.innerHTML = '';
  function addSec(label, text) {
    if (!text) return;
    const wrap = document.createElement('div');
    wrap.className = 'sc-sec';
    const lbl = document.createElement('div');
    lbl.className = 'sc-sec-lbl';
    lbl.textContent = label;
    const val = document.createElement('div');
    val.textContent = text;
    wrap.appendChild(lbl); wrap.appendChild(val);
    el.appendChild(wrap);
  }
  // Property identity
  const typeLine = [d.property_type, d.address].filter(Boolean).join(' — ');
  addSec('Property', typeLine || d.address);
  if (d.agency_name) addSec('Agency', d.agency_name);
  // Attributes
  const attrs = [];
  if (d.bedrooms   != null) attrs.push(d.bedrooms   + ' Bed');
  if (d.bathrooms  != null) attrs.push(d.bathrooms  + ' Bath');
  if (d.car_spaces != null) attrs.push(d.car_spaces + ' Car');
  if (d.land_size)          attrs.push(d.land_size);
  if (attrs.length) addSec('Attributes', attrs.join(' | '));
  // Price / auction
  if (d.price)   addSec('Price',   d.price);
  if (d.auction) addSec('Auction', d.auction);
  // Description
  addSec('Description', d.description);
  // Agents
  (d.agents || []).forEach(function(a, i) {
    const parts = [a.name, a.phone];
    if (a.agency && a.agency !== d.agency_name) parts.push(a.agency);
    const line = parts.filter(Boolean).join(' | ');
    if (line) addSec('Agent ' + (i + 1), line);
  });
}

// ── Assign tooltip ────────────────────────────────────────────────────────────
function showAssignTip(text, rect) {
  const tip  = document.getElementById('assign-tip');
  pendingSelText = text;
  tip.style.display = 'flex';
  const tipH = 44;
  // position:fixed uses viewport coords — getBoundingClientRect() is already viewport-relative
  const top  = (window.innerHeight - rect.bottom < 80)
    ? rect.top  - tipH - 8
    : rect.bottom + 8;
  tip.style.top  = Math.max(8, top) + 'px';
  tip.style.left = Math.max(8, Math.min(rect.left, window.innerWidth - 360)) + 'px';
}

function hideAssignTip() {
  document.getElementById('assign-tip').style.display = 'none';
  pendingSelText = '';
}

function assignSelection() {
  let ph = document.getElementById('assign-sel').value;
  if (ph === '__new__') {
    ph = document.getElementById('assign-new-name').value
      .trim().toLowerCase().replace(/\s+/g,'_').replace(/[^a-z0-9_]/g,'');
    if (!ph) { document.getElementById('assign-new-name').focus(); return; }
  }
  if (!ph || !pendingSelText) return;
  customValues[ph] = pendingSelText;
  renderCustomFields();
  updateCaption();
  hideAssignTip();
  window.getSelection().removeAllRanges();
}

// ── Caption ──────────────────────────────────────────────────────────────────
function renderTemplate(tpl, details, td, shortUrl) {
  const agents = (td && td.default_agents && td.default_agents.length)
    ? td.default_agents : (details.agents || []);
  const a1 = agents[0] || {}, a2 = agents[1] || {};
  const fp = [];
  if (details.bedrooms   != null) fp.push(details.bedrooms   + ' Bed');
  if (details.bathrooms  != null) fp.push(details.bathrooms  + ' Bath');
  if (details.car_spaces != null) fp.push(details.car_spaces + ' Car');
  if (details.land_size)          fp.push(details.land_size);
  const subs = {
    address:                     details.address || '',
    beds:                        details.bedrooms   != null ? String(details.bedrooms)   : '',
    baths:                       details.bathrooms  != null ? String(details.bathrooms)  : '',
    cars:                        details.car_spaces != null ? String(details.car_spaces) : '',
    land_size:                   details.land_size || '',
    property_type:               details.property_type || '',
    features_line:               fp.join(' | '),
    description_first_paragraph: firstParagraph(details.description || ''),
    description_full:            details.description_full || details.description || '',
    price:                       details.price || '',
    auction_date:                details.auction || '',
    short_url:                   shortUrl || '[add short URL]',
    agency_name:                 details.agency_name || (details.agents && details.agents[0] && details.agents[0].agency) || '',
    agent_1_name:                a1.name  || '',
    agent_1_phone:               a1.phone || '',
    agent_2_name:                a2.name  || '',
    agent_2_phone:               a2.phone || '',
  };
  const customPH = (td && td.custom_placeholders) || {};
  const emptyCustomKeys = Object.keys(customPH).filter(function(k) { return !customPH[k]; });
  let out = tpl;
  // 1. User session fills (highest priority — override everything)
  for (const [k, v] of Object.entries(customValues)) out = out.split('{' + k + '}').join(v);
  // 2. Built-in scraped subs (win over template-saved custom values)
  for (const [k, v] of Object.entries(subs)) out = out.split('{' + k + '}').join(v);
  // 3. Non-empty template-saved custom placeholder values
  for (const [k, v] of Object.entries(customPH)) {
    if (v) out = out.split('{' + k + '}').join(v);
  }
  // 4. Filter: keep lines where all remaining {x} are empty custom placeholders (intentional);
  //    strip lines with unknown placeholders or separator-only content.
  return out.split(NL)
    .filter(function(l) {
      if (/^[\s|\\-]+$/.test(l)) return false;
      const remaining = [...l.matchAll(/\{([a-z_0-9]+)\}/g)].map(function(m) { return m[1]; });
      if (!remaining.length) return true;
      return remaining.every(function(k) { return emptyCustomKeys.indexOf(k) !== -1; });
    })
    .join(NL).replace(NL+NL+NL, NL+NL).trim();
}

function firstParagraph(desc) {
  for (const p of desc.split(NL+NL)) {
    const t = p.trim();
    if (t.length > 60 && !t.startsWith('\u2022') && !t.startsWith('DATE PUBLISHED') && t !== t.toUpperCase())
      return t;
  }
  return (desc.split(NL)[0] || '').trim();
}

function updateCaption() {
  if (!listing) return;
  const shortUrl = document.getElementById('short-url').value.trim();
  document.getElementById('caption-text').value =
    renderTemplate(tplData ? tplData.caption_template : DEFAULT_TPL, listing.details, tplData, shortUrl);
  captionDirty = false;
  document.getElementById('btn-save-defaults').style.display = tplId ? 'inline-flex' : 'none';
  updateCaptionHighlights();
}

async function saveDefaultsToTemplate() {
  if (!tplId || !tplData) return;
  const btn = document.getElementById('btn-save-defaults');
  btn.textContent = 'Saving...';
  btn.disabled = true;
  try {
    const payload = Object.assign({}, tplData, { default_values: Object.assign({}, customValues) });
    const r = await fetch('/api/templates/' + tplId, {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload)
    });
    if (r.ok) {
      tplData = payload;
      btn.textContent = 'Saved!';
      setTimeout(function() { btn.textContent = 'Save to Template'; btn.disabled = false; }, 2000);
    } else {
      btn.textContent = 'Save to Template';
      btn.disabled = false;
      alert('Failed to save.');
    }
  } catch(e) {
    btn.textContent = 'Save to Template';
    btn.disabled = false;
    alert('Failed to save.');
  }
}

function copyCaption() {
  navigator.clipboard.writeText(document.getElementById('caption-text').value).then(() => {
    const btn = event.target;
    btn.textContent = 'Copied!';
    setTimeout(() => btn.textContent = 'Copy Caption', 2000);
  });
}

// ── Photo selection ──────────────────────────────────────────────────────────
function cbs() { return [...document.querySelectorAll('#photo-grid input[type=checkbox]')]; }

function togglePhoto(cb) {
  const v = parseInt(cb.value);
  if (cb.checked) selOrder.push(v);
  else selOrder = selOrder.filter(x => x !== v);
  updateBadges();
  updateSelCount();
}

function updateBadges() {
  cbs().forEach(cb => {
    const badge = cb.closest('.photo-item').querySelector('.badge');
    const idx = selOrder.indexOf(parseInt(cb.value));
    if (idx >= 0) { badge.textContent = idx + 1; badge.style.display = 'flex'; }
    else { badge.style.display = 'none'; }
  });
}

function selectAll() {
  cbs().forEach(c => { if (!c.checked) { c.checked = true; selOrder.push(parseInt(c.value)); } });
  updateBadges(); updateSelCount();
}

function deselectAll() {
  selOrder = []; cbs().forEach(c => c.checked = false);
  updateBadges(); updateSelCount();
}

function updateSelCount() {
  document.getElementById('sel-count').textContent = selOrder.length + ' selected';
}

// ── Download ─────────────────────────────────────────────────────────────────
async function downloadPhotos() {
  if (!selOrder.length) { alert('No photos selected.'); return; }
  const btn = document.getElementById('dl-btn');
  const status = document.getElementById('dl-status');
  btn.disabled = true;
  status.textContent = 'Preparing...';
  try {
    const r = await fetch('/api/download/' + listing.listing_id, {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({keep: [...selOrder]}),
    });
    if (!r.ok) { status.textContent = 'Download failed.'; return; }
    const blob = await r.blob();
    const street = ((listing.details.address || '').split(',')[0]);
    const slug = street.toLowerCase().replace(/[^a-z0-9]+/g,'_').replace(/^_|_$/g,'') || listing.listing_id;
    const a = document.createElement('a');
    a.href = URL.createObjectURL(blob);
    a.download = slug + '.zip';
    a.click();
    status.textContent = selOrder.length + ' photo(s) downloaded.';
  } catch(e) {
    status.textContent = 'Error: ' + e.message;
  } finally {
    btn.disabled = false;
  }
}

// ── Template select ───────────────────────────────────────────────────────────
async function loadTemplateSelect() {
  const sel = document.getElementById('tpl-select');
  const prev = sel.value;
  const r = await fetch('/api/templates');
  const list = await r.json();
  sel.innerHTML = '<option value="">Auto-detect template</option>';
  for (const t of list) {
    const opt = document.createElement('option');
    opt.value = t.id;
    opt.textContent = t.agency_name;
    sel.appendChild(opt);
  }
  if (prev && [...sel.options].some(o => o.value === prev)) sel.value = prev;
  buildScodeMap(list);
}

function buildScodeMap(tpls) {
  scodeMap = {};
  for (const t of tpls) {
    if (t.shortcut_code) scodeMap[t.shortcut_code] = {id: t.id, agency_name: t.agency_name};
  }
}

async function loadAndApplyTemplate(id) {
  const r    = await fetch('/api/templates/' + id);
  const data = await r.json();
  preSelectedTemplate = {id, data};
  tplId   = id;
  tplData = data;
  const sel = document.getElementById('tpl-select');
  if ([...sel.options].some(function(o) { return o.value === id; })) sel.value = id;
  if (listing) {
    document.getElementById('tpl-label').innerHTML =
      'Template: <span>' + escHtml(data.agency_name) + '</span>';
    document.getElementById('no-tpl-banner').style.display = 'none';
    customValues = Object.assign({}, data.default_values || {});
    renderCustomFields();
    updateCaption();
  }
}

async function onTemplateSelect() {
  const id = document.getElementById('tpl-select').value;
  if (!id) {
    preSelectedTemplate = null;
    if (listing) {
      tplId   = listing.template_id   || null;
      tplData = listing.template_data || null;
      document.getElementById('tpl-label').innerHTML = tplData
        ? 'Template: <span>' + escHtml(tplData.agency_name) + '</span>'
        : 'Template: <span>Default</span>';
      updateCaption();
    }
    return;
  }
  await loadAndApplyTemplate(id);
}

// ── Template Picker (Ctrl+/) ──────────────────────────────────────────────────
async function openPicker() {
  const r = await fetch('/api/templates');
  pickerAllTpls = await r.json();
  pickerOpen    = true;
  pickerActive  = -1;
  document.getElementById('picker-search').value = '';
  document.getElementById('picker-overlay').classList.add('open');
  pickerFilter();
  document.getElementById('picker-search').focus();
}

function closePicker() {
  document.getElementById('picker-overlay').classList.remove('open');
  pickerOpen   = false;
  pickerActive = -1;
  if (listing) document.getElementById('caption-text').focus();
}

function pickerBackdropClick(e) {
  if (e.target === document.getElementById('picker-overlay')) closePicker();
}

function pickerFilter() {
  const q     = document.getElementById('picker-search').value.trim().toLowerCase();
  const alpha = function(a, b) { return (a.agency_name || '').localeCompare(b.agency_name || ''); };
  if (!q) {
    pickerItems = pickerAllTpls.slice();
  } else {
    const tier1 = [], tier2 = [], tier3 = [];
    for (const t of pickerAllTpls) {
      const name   = (t.agency_name || '').toLowerCase();
      const folder = (t.folder      || '').toLowerCase();
      const tags   = (t.tags        || []).map(function(x) { return x.toLowerCase(); });
      if (name.startsWith(q))                                                               tier1.push(t);
      else if (name.includes(q))                                                            tier2.push(t);
      else if (folder.includes(q) || tags.some(function(tag) { return tag.includes(q); })) tier3.push(t);
    }
    pickerItems = tier1.sort(alpha).concat(tier2.sort(alpha)).concat(tier3.sort(alpha));
  }
  pickerActive = pickerItems.length ? 0 : -1;
  pickerRenderList();
}

function pickerRenderList() {
  const list      = document.getElementById('picker-list');
  const empty     = document.getElementById('picker-empty');
  const createBtn = document.getElementById('picker-create-btn');
  if (!pickerItems.length) {
    list.innerHTML = '';
    empty.classList.add('visible');
    createBtn.style.display = pickerAllTpls.length === 0 ? 'inline-block' : 'none';
    return;
  }
  empty.classList.remove('visible');
  list.innerHTML = pickerItems.map(function(t, i) {
    const folderHtml = t.folder ? escHtml(t.folder) : 'No folder';
    const tagsHtml   = renderTagChips(t.tags);
    return '<li class="picker-item' + (i === pickerActive ? ' pk-active' : '') + '"' +
      ' data-id="' + escAttr(t.id) + '">' +
      '<div class="picker-item-info">' +
        '<div class="picker-item-name">' + escHtml(t.agency_name) + '</div>' +
        '<div class="picker-item-folder">' + folderHtml + '</div>' +
        (tagsHtml ? '<div style="margin-top:4px">' + tagsHtml + '</div>' : '') +
      '</div>' +
      '<span class="picker-item-hint">Ctrl+/</span>' +
      '</li>';
  }).join('');
}

function pickerHighlight() {
  const items = [...document.getElementById('picker-list').querySelectorAll('.picker-item')];
  items.forEach(function(el, i) { el.classList.toggle('pk-active', i === pickerActive); });
  if (pickerActive >= 0 && items[pickerActive]) {
    items[pickerActive].scrollIntoView({block: 'nearest'});
  }
}

function pickerMoveActive(dir) {
  if (!pickerItems.length) return;
  pickerActive = (pickerActive + dir + pickerItems.length) % pickerItems.length;
  pickerHighlight();
}

function pickerConfirmActive() {
  const items = [...document.getElementById('picker-list').querySelectorAll('.picker-item')];
  if (pickerActive >= 0 && items[pickerActive]) pickerSelect(items[pickerActive].dataset.id);
}

async function pickerSelect(id) {
  if (listing && captionDirty) {
    const ok = await customConfirm(
      'Loading a new template will replace your current caption. Continue?',
      'Replace', 'Keep my edits'
    );
    if (!ok) return;
  }
  closePicker();
  await loadAndApplyTemplate(id);
}

// ── Shortcode hint (;code) ────────────────────────────────────────────────────
function checkShortcode() {
  if (!listing) return;
  const ta    = document.getElementById('caption-text');
  const pos   = ta.selectionStart;
  const text  = ta.value.substring(0, pos);
  const m     = text.match(/;([a-z0-9]+)$/);
  if (!m) { closeScodeHint(); return; }
  scodeStart  = pos - m[1].length - 1;
  const matches = Object.entries(scodeMap).filter(function([code]) { return code.startsWith(m[1]); });
  if (!matches.length) { closeScodeHint(); return; }
  showScodeHint(matches);
}

function showScodeHint(matches) {
  const ta   = document.getElementById('caption-text');
  const wrap = ta.parentElement;
  const drop = document.getElementById('sc-hint');
  scodeItems  = matches;
  scodeActive = 0;
  renderScodeItems();
  const {x, y} = getCaretCoords(ta);
  drop.style.left    = Math.max(0, Math.min(x, wrap.offsetWidth - 280)) + 'px';
  drop.style.top     = (y + 4) + 'px';
  drop.style.display = 'block';
}

function renderScodeItems() {
  const drop = document.getElementById('sc-hint');
  drop.innerHTML = scodeItems.map(function([code, t], i) {
    return '<div class="sc-item' + (i === scodeActive ? ' sc-active' : '') + '" data-id="' + escAttr(t.id) + '">' +
      '<span class="sc-code">' + escHtml(code) + '</span>' +
      '<span class="sc-name">' + escHtml(t.agency_name) + '</span>' +
      '<span class="sc-enter">↵</span>' +
      '</div>';
  }).join('');
}

function closeScodeHint() {
  document.getElementById('sc-hint').style.display = 'none';
  scodeItems  = [];
  scodeActive = 0;
}

function scodeMoveActive(dir) {
  if (!scodeItems.length) return;
  scodeActive = (scodeActive + dir + scodeItems.length) % scodeItems.length;
  renderScodeItems();
}

async function applyShortcode(id) {
  if (listing && captionDirty) {
    const ok = await customConfirm(
      'Loading a new template will replace your current caption. Continue?',
      'Replace', 'Keep my edits'
    );
    if (!ok) { closeScodeHint(); return; }
  }
  closeScodeHint();
  await loadAndApplyTemplate(id);
}

// ── Caption fill overlay ──────────────────────────────────────────────────────
function getFillablePlaceholders() {
  if (!tplData || !tplData.custom_placeholders) return new Set();
  const s = new Set();
  for (const [name, val] of Object.entries(tplData.custom_placeholders))
    if (val === '') s.add(name);
  return s;
}

let hlTimer = null;
function scheduleHighlights() {
  clearTimeout(hlTimer);
  hlTimer = setTimeout(updateCaptionHighlights, 100);
}

function updateCaptionHighlights() {
  const ta       = document.getElementById('caption-text');
  const hl       = document.getElementById('cap-highlights');
  const hint     = document.getElementById('fill-hint');
  const fillable = getFillablePlaceholders();
  hl.style.marginTop = -ta.scrollTop + 'px';
  if (!fillable.size) { hl.innerHTML = ''; hint.style.display = 'none'; return; }
  let count = 0;
  const marked = escHtml(ta.value).replace(/\{([a-z][a-z0-9_]*)\}/g, function(match, name) {
    if (!fillable.has(name)) return match;
    count++;
    return '<mark class="cap-fillable">' + match + '</mark>';
  });
  hl.innerHTML = marked;
  hint.style.display = count ? 'block' : 'none';
  if (count) hint.textContent = 'Tab to fill ' + count + ' placeholder' + (count === 1 ? '' : 's');
}

// ── Folder / meta ─────────────────────────────────────────────────────────────
async function loadMeta() {
  const r = await fetch('/api/templates/meta');
  templateMeta = await r.json();
}

function renderTagChips(tags) {
  if (!tags || !tags.length) return '';
  const sorted  = [...tags].sort();
  const visible = sorted.slice(0, 3);
  const extra   = sorted.length - 3;
  let html = '<div class="tpl-tags">';
  for (const t of visible) html += '<span class="tag-chip">' + escHtml(t) + '</span>';
  if (extra > 0) html += '<span class="tag-chip-more">+' + extra + ' more</span>';
  html += '</div>';
  return html;
}

function renderFolderSidebar(allTpls) {
  const sidebar = document.getElementById('folder-sidebar');
  const folders = templateMeta.folders || [];
  let html = '<div class="folder-item' + (activeFolderName === '' ? ' active' : '') +
    '" data-fld="" onclick="selectFolder(this.dataset.fld)">' +
    '<span class="folder-item-name">All Templates</span>' +
    '<span class="folder-count">(' + allTpls.length + ')</span>' +
    '</div>';
  for (const f of folders) {
    const count    = allTpls.filter(t => t.folder === f).length;
    const isActive = activeFolderName === f;
    html += '<div class="folder-item' + (isActive ? ' active' : '') +
      '" data-fld="' + escAttr(f) + '" onclick="selectFolder(this.dataset.fld)">' +
      '<span class="folder-item-name">' + escHtml(f) + '</span>' +
      '<span style="display:flex;align-items:center;gap:2px;flex-shrink:0">' +
        '<span class="folder-count">(' + count + ')</span>' +
        '<button class="folder-del" title="Delete folder" data-fname="' + escAttr(f) + '"' +
          ' onclick="event.stopPropagation();deleteFolder(this.dataset.fname)">&#215;</button>' +
      '</span>' +
      '</div>';
  }
  html += '<button class="folder-new" onclick="addFolder()">+ New Folder</button>';
  sidebar.innerHTML = html;
}

function renderTemplateList(filteredTpls) {
  const body = document.getElementById('list-body');
  if (!filteredTpls.length) {
    const msg = activeFolderName === ''
      ? 'No agency templates saved yet.'
      : 'No templates in this folder yet.';
    body.innerHTML = '<p class="tpl-empty">' + msg + '</p>';
    return;
  }
  body.innerHTML = filteredTpls.map(t =>
    '<div class="tpl-item">' +
      '<div style="flex:1;min-width:0">' +
        '<span class="tpl-item-name">' + escHtml(t.agency_name) + '</span>' +
        renderTagChips(t.tags) +
      '</div>' +
      '<div style="flex-shrink:0">' +
        '<button class="btn btn-ghost btn-sm" data-id="' + escAttr(t.id) +
          '" onclick="openDrawerEdit(this.dataset.id)">Edit</button>' +
      '</div>' +
    '</div>'
  ).join('');
}

function renderDrawerList() {
  const filtered = activeFolderName === ''
    ? allTplList
    : allTplList.filter(t => t.folder === activeFolderName);
  renderFolderSidebar(allTplList);
  renderTemplateList(filtered);
}

function selectFolder(name) {
  activeFolderName = name;
  renderDrawerList();
}

async function addFolder() {
  const name = prompt('New folder name:');
  if (!name || !name.trim()) return;
  const r = await fetch('/api/templates/folders', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({name: name.trim()}),
  });
  if (!r.ok) { alert('Failed to create folder.'); return; }
  await loadMeta();
  renderFolderSidebar(allTplList);
}

async function deleteFolder(name) {
  if (!confirm(
    "Delete folder '" + name + "'? Templates in this folder will be moved to no folder." +
    " The templates themselves won't be deleted."
  )) return;
  const r = await fetch('/api/templates/folders/' + encodeURIComponent(name), {method: 'DELETE'});
  if (!r.ok) { alert('Failed to delete folder.'); return; }
  if (activeFolderName === name) activeFolderName = '';
  await loadMeta();
  const r2 = await fetch('/api/templates');
  allTplList = await r2.json();
  renderDrawerList();
}

function populateFolderSelect(currentFolder) {
  const sel     = document.getElementById('tpl-folder');
  const folders = templateMeta.folders || [];
  let html = '<option value="">None</option>';
  for (const f of folders) {
    html += '<option value="' + escAttr(f) + '"' + (f === currentFolder ? ' selected' : '') + '>' +
      escHtml(f) + '</option>';
  }
  sel.innerHTML = html;
  if (currentFolder && !folders.includes(currentFolder)) sel.value = '';
}

// ── Drawer ───────────────────────────────────────────────────────────────────
function openDrawer() {
  showList();
  loadList();
  document.getElementById('overlay').style.display = 'block';
  document.getElementById('drawer').classList.add('open');
}

async function openDrawerNew() {
  await loadMeta();
  const agency = (listing && listing.agency_name) || '';
  showEdit('New Template');
  document.getElementById('tpl-id').value = '';
  document.getElementById('tpl-agency').value = agency;
  document.getElementById('tpl-body').value = DEFAULT_TPL;
  document.getElementById('tpl-notes').value = '';
  document.getElementById('agents-list').innerHTML = '';
  document.getElementById('cp-list').innerHTML = '';
  document.getElementById('btn-delete').style.display = 'none';
  populateFolderSelect('');
  document.getElementById('tpl-tags').value = '';
  document.getElementById('tpl-shortcut').value = '';
  recentPH = [];
  updateHighlights();
  document.getElementById('ph-warnings').innerHTML = '';
  refreshPreview();
  document.getElementById('overlay').style.display = 'block';
  document.getElementById('drawer').classList.add('open');
}

async function openDrawerEdit(id) {
  await loadMeta();
  const r = await fetch('/api/templates/' + id);
  const data = await r.json();
  showEdit('Edit Template');
  document.getElementById('tpl-id').value = id;
  document.getElementById('tpl-agency').value = data.agency_name || '';
  document.getElementById('tpl-body').value = data.caption_template || DEFAULT_TPL;
  document.getElementById('tpl-notes').value = data.notes || '';
  document.getElementById('btn-delete').style.display = 'inline-flex';
  const list = document.getElementById('agents-list');
  list.innerHTML = '';
  for (const a of (data.default_agents || [])) addAgent(a.name, a.phone);
  document.getElementById('cp-list').innerHTML = '';
  for (const [name, value] of Object.entries(data.custom_placeholders || {})) addCPRow(name, value);
  populateFolderSelect(data.folder || '');
  document.getElementById('tpl-tags').value = (data.tags || []).join(', ');
  document.getElementById('tpl-shortcut').value = data.shortcut_code || '';
  recentPH = [];
  updateHighlights();
  document.getElementById('ph-warnings').innerHTML = '';
  refreshPreview();
  document.getElementById('overlay').style.display = 'block';
  document.getElementById('drawer').classList.add('open');
}

function closeDrawer() {
  document.getElementById('overlay').style.display = 'none';
  document.getElementById('drawer').classList.remove('open');
  recentPH = [];
  closeAC();
}

function showList() {
  document.getElementById('view-list').style.display = 'flex';
  document.getElementById('view-edit').style.display = 'none';
}

function showEdit(title) {
  document.getElementById('view-list').style.display = 'none';
  document.getElementById('view-edit').style.display = 'flex';
  document.getElementById('edit-title').textContent = title;
}

async function loadList() {
  loadTemplateSelect();
  await loadMeta();
  const r = await fetch('/api/templates');
  allTplList = await r.json();
  renderDrawerList();
}

// ── Agent rows ────────────────────────────────────────────────────────────────
function addAgent(name, phone) {
  const row = document.createElement('div');
  row.className = 'agent-row';
  const nameIn = document.createElement('input');
  nameIn.type = 'text'; nameIn.placeholder = 'Agent name'; nameIn.value = name || '';
  nameIn.addEventListener('input', refreshPreview);
  const phoneIn = document.createElement('input');
  phoneIn.type = 'text'; phoneIn.placeholder = 'Phone'; phoneIn.value = phone || '';
  phoneIn.addEventListener('input', refreshPreview);
  const rm = document.createElement('button');
  rm.className = 'btn-rm'; rm.textContent = '×';
  rm.addEventListener('click', function() { row.remove(); refreshPreview(); });
  row.appendChild(nameIn); row.appendChild(phoneIn); row.appendChild(rm);
  document.getElementById('agents-list').appendChild(row);
}

function getAgents() {
  return [...document.querySelectorAll('#agents-list .agent-row')].map(row => {
    const ins = row.querySelectorAll('input');
    return {name: ins[0].value.trim(), phone: ins[1].value.trim()};
  }).filter(a => a.name || a.phone);
}

// ── Live preview ──────────────────────────────────────────────────────────────
function refreshPreview() {
  const box = document.getElementById('live-preview');
  if (!listing) { box.textContent = 'Load a listing first to see a preview.'; return; }
  const tpl = document.getElementById('tpl-body').value;
  const shortUrl = document.getElementById('short-url').value.trim();
  box.innerHTML = renderPreviewHTML(tpl, listing.details, {default_agents: getAgents(), custom_placeholders: getCustomPlaceholders()}, shortUrl || '[short URL]');
}

// ── Authoring UX ─────────────────────────────────────────────────────────────

function onTplBodyInput() {
  updateHighlights();
  maybeShowAC();
  scheduleWarnings();
  refreshPreview();
}

function updateHighlights() {
  const ta = document.getElementById('tpl-body');
  const hl = document.getElementById('tpl-body-highlights');
  if (!hl) return;
  const marked = escHtml(ta.value).replace(/\{([a-z_0-9]+)\}/g, '<mark class="ph-hl">{$1}</mark>');
  hl.innerHTML = marked;
  hl.style.marginTop = -ta.scrollTop + 'px';
}

function getCaretCoords(ta) {
  const mirror = document.createElement('div');
  const cs = window.getComputedStyle(ta);
  mirror.style.position   = 'absolute';
  mirror.style.visibility = 'hidden';
  mirror.style.overflow   = 'hidden';
  mirror.style.whiteSpace = 'pre-wrap';
  mirror.style.wordBreak  = 'break-word';
  mirror.style.wordWrap   = 'break-word';
  mirror.style.fontFamily = cs.fontFamily;
  mirror.style.fontSize   = cs.fontSize;
  mirror.style.lineHeight = cs.lineHeight;
  mirror.style.padding    = cs.padding;
  mirror.style.border     = cs.border;
  mirror.style.boxSizing  = cs.boxSizing;
  mirror.style.width      = ta.offsetWidth + 'px';
  mirror.appendChild(document.createTextNode(ta.value.substring(0, ta.selectionStart)));
  const span = document.createElement('span');
  span.textContent = '​';
  mirror.appendChild(span);
  document.body.appendChild(mirror);
  const taRect   = ta.getBoundingClientRect();
  const spanRect = span.getBoundingClientRect();
  document.body.removeChild(mirror);
  return {
    x: spanRect.left - taRect.left + ta.scrollLeft,
    y: spanRect.bottom - taRect.top + ta.scrollTop
  };
}

function maybeShowAC() {
  const ta     = document.getElementById('tpl-body');
  const before = ta.value.substring(0, ta.selectionStart);
  const m      = before.match(/\{([a-z0-9_]*)$/i);
  if (!m) { closeAC(); return; }
  const query       = m[1].toLowerCase();
  const customKeys  = Object.keys(getCustomPlaceholders());
  const builtins    = BUILTIN_PH_LIST.filter(function(p) { return p.startsWith(query); });
  const customs     = customKeys.filter(function(k) { return k.startsWith(query); });
  if (!builtins.length && !customs.length) { closeAC(); return; }
  function byRecency(arr) {
    return arr.slice().sort(function(a, b) {
      const ra = recentPH.indexOf(a), rb = recentPH.indexOf(b);
      if (ra === -1 && rb === -1) return 0;
      if (ra === -1) return 1;
      if (rb === -1) return -1;
      return ra - rb;
    });
  }
  showAC(byRecency(builtins), byRecency(customs));
}

function showAC(builtins, customs) {
  const ta   = document.getElementById('tpl-body');
  const wrap = document.querySelector('.tpl-body-wrap');
  const ac   = document.getElementById('ph-autocomplete');
  const list = document.getElementById('ph-ac-list');
  list.innerHTML = '';
  const items = [];
  builtins.forEach(function(name) {
    const li = document.createElement('li');
    li.textContent = '{' + name + '}';
    li.dataset.name = name;
    li.addEventListener('mousedown', function(e) { e.preventDefault(); insertPH(name); });
    list.appendChild(li);
    items.push(li);
  });
  if (builtins.length && customs.length) {
    const sep = document.createElement('li');
    sep.className = 'ph-ac-sep';
    sep.setAttribute('aria-hidden', 'true');
    list.appendChild(sep);
  }
  customs.forEach(function(name) {
    const li = document.createElement('li');
    li.textContent = '{' + name + '} — custom';
    li.dataset.name = name;
    li.addEventListener('mousedown', function(e) { e.preventDefault(); insertPH(name); });
    list.appendChild(li);
    items.push(li);
  });
  acActive = items.length ? 0 : -1;
  if (items.length) items[0].classList.add('ac-active');
  const {x, y} = getCaretCoords(ta);
  ac.style.left    = Math.max(0, Math.min(x, wrap.offsetWidth - 228)) + 'px';
  ac.style.top     = (y + 4) + 'px';
  ac.style.display = 'block';
}

function closeAC() {
  document.getElementById('ph-autocomplete').style.display = 'none';
  acActive = -1;
}

function insertPH(name) {
  const ta      = document.getElementById('tpl-body');
  const pos     = ta.selectionStart;
  const val     = ta.value;
  const before  = val.substring(0, pos);
  const trigIdx = before.lastIndexOf('{');
  if (trigIdx === -1) return;
  const after        = val.substring(pos);
  const consumeClose = after.startsWith('}') ? 1 : 0;
  ta.value = val.substring(0, trigIdx) + '{' + name + '}' + val.substring(pos + consumeClose);
  const newPos = trigIdx + name.length + 2;
  ta.selectionStart = ta.selectionEnd = newPos;
  const idx = recentPH.indexOf(name);
  if (idx !== -1) recentPH.splice(idx, 1);
  recentPH.unshift(name);
  closeAC();
  onTplBodyInput();
}

function acMoveActive(dir) {
  const list  = document.getElementById('ph-ac-list');
  const items = [...list.querySelectorAll('li:not(.ph-ac-sep)')];
  if (!items.length) return;
  if (acActive >= 0) items[acActive].classList.remove('ac-active');
  acActive = (acActive + dir + items.length) % items.length;
  items[acActive].classList.add('ac-active');
  items[acActive].scrollIntoView({block: 'nearest'});
}

function scheduleWarnings() {
  if (warnTimer) clearTimeout(warnTimer);
  warnTimer = setTimeout(updateWarnings, 300);
}

function updateWarnings() {
  const ta         = document.getElementById('tpl-body');
  const div        = document.getElementById('ph-warnings');
  const customKeys = new Set(Object.keys(getCustomPlaceholders()));
  const allNames   = [...ta.value.matchAll(/\{([a-z_0-9]+)\}/g)].map(function(m) { return m[1]; });
  const seen = new Set(), unknowns = [];
  for (const n of allNames) {
    if (!seen.has(n) && !STANDARD_PH.has(n) && !customKeys.has(n)) {
      seen.add(n); unknowns.push(n);
    }
  }
  if (!unknowns.length) { div.innerHTML = ''; return; }
  let html = '&#9888; Unknown placeholders: ';
  html += unknowns.map(function(n) {
    return '<span class="ph-warn-item" data-name="' + escAttr(n) + '">{' + escHtml(n) + '}</span>';
  }).join(', ');
  div.innerHTML = html;
  div.querySelectorAll('.ph-warn-item').forEach(function(el) {
    el.addEventListener('click', function() { jumpToUnknown(this.dataset.name); });
  });
}

function jumpToUnknown(name) {
  const ta  = document.getElementById('tpl-body');
  const idx = ta.value.indexOf('{' + name);
  if (idx === -1) return;
  ta.focus();
  const cursorPos = idx + 1 + name.length;
  ta.selectionStart = ta.selectionEnd = cursorPos;
  maybeShowAC();
}

function renderPreviewHTML(tpl, details, td, shortUrl) {
  const agents = (td && td.default_agents && td.default_agents.length)
    ? td.default_agents : (details.agents || []);
  const a1 = agents[0] || {}, a2 = agents[1] || {};
  const fp = [];
  if (details.bedrooms   != null) fp.push(details.bedrooms   + ' Bed');
  if (details.bathrooms  != null) fp.push(details.bathrooms  + ' Bath');
  if (details.car_spaces != null) fp.push(details.car_spaces + ' Car');
  if (details.land_size)          fp.push(details.land_size);
  const subs = {
    address:                     details.address || '',
    beds:                        details.bedrooms   != null ? String(details.bedrooms)   : '',
    baths:                       details.bathrooms  != null ? String(details.bathrooms)  : '',
    cars:                        details.car_spaces != null ? String(details.car_spaces) : '',
    land_size:                   details.land_size || '',
    property_type:               details.property_type || '',
    features_line:               fp.join(' | '),
    description_first_paragraph: firstParagraph(details.description || ''),
    description_full:            details.description_full || details.description || '',
    price:                       details.price || '',
    auction_date:                details.auction || '',
    short_url:                   shortUrl || '[add short URL]',
    agency_name:                 details.agency_name || (details.agents && details.agents[0] && details.agents[0].agency) || '',
    agent_1_name:                a1.name  || '',
    agent_1_phone:               a1.phone || '',
    agent_2_name:                a2.name  || '',
    agent_2_phone:               a2.phone || '',
  };
  const customPH       = (td && td.custom_placeholders) || {};
  const emptyCustomKeys = new Set(Object.keys(customPH).filter(function(k) { return !customPH[k]; }));
  let out = escHtml(tpl);
  for (const [k, v] of Object.entries(customValues)) out = out.split('{' + k + '}').join(escHtml(String(v)));
  for (const [k, v] of Object.entries(subs))         out = out.split('{' + k + '}').join(escHtml(String(v)));
  for (const [k, v] of Object.entries(customPH)) {
    if (v) out = out.split('{' + k + '}').join(escHtml(String(v)));
  }
  out = out.replace(/\{([a-z_0-9]+)\}/g, function(match, name) {
    return emptyCustomKeys.has(name) ? match : '<span class="ph-unknown">' + match + '</span>';
  });
  return out;
}

// ── Save / delete ─────────────────────────────────────────────────────────────
async function saveTemplate() {
  const id      = document.getElementById('tpl-id').value.trim();
  const agency  = document.getElementById('tpl-agency').value.trim();
  const body    = document.getElementById('tpl-body').value;
  const notes   = document.getElementById('tpl-notes').value.trim();
  const agents  = getAgents();
  const folder  = document.getElementById('tpl-folder').value || null;
  const tagsRaw = document.getElementById('tpl-tags').value;
  const tags    = tagsRaw.split(',').map(t => t.trim().toLowerCase()).filter(Boolean);
  const sc      = document.getElementById('tpl-shortcut').value.trim().toLowerCase().replace(/[^a-z0-9]/g, '') || null;
  if (!agency) { alert('Agency name is required.'); return; }

  // Custom placeholder validation
  const customPH  = getCustomPlaceholders();
  const nameRe    = /^[a-z][a-z0-9_]*$/;
  for (const k of Object.keys(customPH)) {
    if (!nameRe.test(k)) {
      alert("Placeholder name '" + k + "' invalid. Use lowercase letters, numbers, and underscores only, starting with a letter.");
      return;
    }
  }
  if (sc) {
    const scOther = allTplList.find(function(t) { return t.shortcut_code === sc && t.id !== id; });
    if (scOther) {
      const ok = await customConfirm(
        'Shortcut code "' + sc + '" is already used by "' + escHtml(scOther.agency_name) + '". Each code should be unique. Save anyway?',
        'Save anyway', 'Let me rename it'
      );
      if (!ok) return;
    }
  }
  const conflicts = Object.keys(customPH).filter(function(k) { return STANDARD_PH.has(k); });
  if (conflicts.length) {
    const names = conflicts.map(function(k) { return "'" + k + "'"; }).join(', ');
    const ok = await customConfirm(
      "Custom placeholder " + names + " conflicts with a built-in placeholder — the built-in will be used during rendering. Rename to avoid confusion.",
      'Save anyway', 'Let me rename it'
    );
    if (!ok) return;
  }

  const payload = {agency_name: agency, caption_template: body, default_agents: agents, notes, folder, tags, custom_placeholders: customPH, shortcut_code: sc};
  const isNew   = !id;
  const r = await fetch(isNew ? '/api/templates' : '/api/templates/' + id, {
    method: isNew ? 'POST' : 'PUT',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify(payload),
  });
  if (!r.ok) {
    const errData = await r.json().catch(function() { return {}; });
    alert(errData.error || 'Failed to save template.');
    return;
  }
  const saved = await r.json();

  // Refresh caption if this template belongs to current listing
  if (listing) {
    if (isNew && listing.agency_name === agency) {
      tplId = saved.id; tplData = payload;
      document.getElementById('tpl-label').innerHTML = 'Template: <span>' + escHtml(agency) + '</span>';
      document.getElementById('no-tpl-banner').style.display = 'none';
      updateCaption();
    } else if (!isNew && id === tplId) {
      tplData = payload;
      document.getElementById('tpl-label').innerHTML = 'Template: <span>' + escHtml(agency) + '</span>';
      updateCaption();
    }
  }

  showList();
  loadList();
}

async function deleteTemplate() {
  const id = document.getElementById('tpl-id').value.trim();
  if (!id || !confirm('Delete this template?')) return;
  await fetch('/api/templates/' + id, {method: 'DELETE'});
  if (id === tplId) {
    tplId = null; tplData = null;
    document.getElementById('tpl-label').innerHTML = 'Template: <span>Default</span>';
    updateCaption();
  }
  showList();
  loadList();
}

// ── Helpers ───────────────────────────────────────────────────────────────────
function setState(s) {
  document.getElementById('state-idle').style.display    = s === 'idle'    ? 'block' : 'none';
  document.getElementById('state-loading').style.display = s === 'loading' ? 'block' : 'none';
  document.getElementById('state-error').style.display   = s === 'error'   ? 'block' : 'none';
  document.getElementById('state-result').style.display  = s === 'result'  ? 'block' : 'none';
}

function showError(msg) {
  const el = document.getElementById('error-msg');
  el.style.whiteSpace = 'pre-wrap';
  el.textContent = msg;
  setState('error');
}

function escHtml(s) {
  return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
}

function escAttr(s) {
  return String(s).replace(/"/g, '&quot;');
}

document.getElementById('url-input').addEventListener('keydown', e => {
  if (e.key === 'Enter') startScrape();
});

document.addEventListener('mouseup', function(e) {
  if (document.getElementById('assign-tip').contains(e.target)) return;
  const sel = window.getSelection();
  if (!sel || sel.isCollapsed) { hideAssignTip(); return; }
  const text = sel.toString().trim();
  if (!text) { hideAssignTip(); return; }
  const panel = document.getElementById('scraped-content');
  if (!panel) return;
  try {
    const range = sel.getRangeAt(0);
    if (!panel.contains(range.commonAncestorContainer)) { hideAssignTip(); return; }
    const custom = getCustomPH(tplData ? tplData.caption_template : DEFAULT_TPL);
    updateAssignSel(custom);
    showAssignTip(text, range.getBoundingClientRect());
  } catch(err) {}
});

// ── Custom placeholder drawer helpers ─────────────────────────────────────────
function getCustomPlaceholders() {
  const rows = document.querySelectorAll('#cp-list .cp-row');
  const result = {};
  rows.forEach(function(row) {
    const name  = row.querySelector('.cp-name').value.trim();
    const value = row.querySelector('.cp-value').value;
    if (name) result[name] = value;
  });
  return result;
}

function addCPRow(name, value) {
  const row = document.createElement('div');
  row.className = 'cp-row';

  const nameIn = document.createElement('input');
  nameIn.type = 'text';
  nameIn.className = 'cp-name';
  nameIn.placeholder = 'name';
  nameIn.maxLength = 60;
  nameIn.value = name || '';
  nameIn.addEventListener('blur', function() {
    this.value = this.value.toLowerCase().replace(/[^a-z0-9_]/g, '').replace(/^[^a-z]+/, '');
    refreshPreview();
    scheduleWarnings();
  });

  const valueIn = document.createElement('input');
  valueIn.type = 'text';
  valueIn.className = 'cp-value';
  valueIn.placeholder = '(empty — fillable)';
  valueIn.value = value !== undefined ? value : '';
  valueIn.addEventListener('input', refreshPreview);

  const rm = document.createElement('button');
  rm.className = 'btn-rm';
  rm.textContent = '×';
  rm.addEventListener('click', function() { row.remove(); refreshPreview(); scheduleWarnings(); });

  row.appendChild(nameIn); row.appendChild(valueIn); row.appendChild(rm);
  document.getElementById('cp-list').appendChild(row);
}

function customConfirm(message, confirmLabel, cancelLabel) {
  return new Promise(function(resolve) {
    const overlay = document.createElement('div');
    overlay.style.cssText = 'position:fixed;inset:0;background:rgba(0,0,0,.4);z-index:300;display:flex;align-items:center;justify-content:center';
    const box = document.createElement('div');
    box.style.cssText = 'background:#fff;border-radius:8px;padding:24px;max-width:380px;width:90%;box-shadow:0 4px 24px rgba(0,0,0,.22);font-family:system-ui,-apple-system,sans-serif';
    const msg = document.createElement('p');
    msg.style.cssText = 'font-size:.9rem;color:#374151;margin-bottom:20px;line-height:1.55';
    msg.textContent = message;
    const btns = document.createElement('div');
    btns.style.cssText = 'display:flex;gap:8px;justify-content:flex-end';
    const cancelBtn = document.createElement('button');
    cancelBtn.textContent = cancelLabel || 'Cancel';
    cancelBtn.style.cssText = 'padding:8px 14px;border-radius:5px;border:1px solid #d1d5db;background:#f9fafb;cursor:pointer;font-size:.88rem;font-family:inherit';
    cancelBtn.onclick = function() { document.body.removeChild(overlay); resolve(false); };
    const confirmBtn = document.createElement('button');
    confirmBtn.textContent = confirmLabel || 'OK';
    confirmBtn.style.cssText = 'padding:8px 14px;border-radius:5px;border:none;background:#2563eb;color:#fff;cursor:pointer;font-size:.88rem;font-weight:600;font-family:inherit';
    confirmBtn.onclick = function() { document.body.removeChild(overlay); resolve(true); };
    btns.appendChild(cancelBtn); btns.appendChild(confirmBtn);
    box.appendChild(msg); box.appendChild(btns);
    overlay.appendChild(box);
    document.body.appendChild(overlay);
  });
}

// ── Authoring UX event listeners ─────────────────────────────────────────────
(function() {
  const ta = document.getElementById('tpl-body');

  ta.addEventListener('keydown', function(e) {
    const ac = document.getElementById('ph-autocomplete');
    if (ac.style.display === 'none') return;
    if (e.key === 'ArrowDown') {
      e.preventDefault(); acMoveActive(1);
    } else if (e.key === 'ArrowUp') {
      e.preventDefault(); acMoveActive(-1);
    } else if (e.key === 'Enter' || e.key === 'Tab') {
      const items = [...document.getElementById('ph-ac-list').querySelectorAll('li:not(.ph-ac-sep)')];
      if (acActive >= 0 && acActive < items.length) {
        e.preventDefault();
        insertPH(items[acActive].dataset.name);
      }
    } else if (e.key === 'Escape') {
      e.preventDefault(); closeAC();
    }
  });

  ta.addEventListener('scroll', function() {
    const hl = document.getElementById('tpl-body-highlights');
    if (hl) hl.style.marginTop = -this.scrollTop + 'px';
  });

  ta.addEventListener('blur', function() { closeAC(); });
  ta.addEventListener('click', function() { maybeShowAC(); });
})();

// ── Picker event listeners ────────────────────────────────────────────────────
document.addEventListener('keydown', function(e) {
  if (e.ctrlKey && e.key === '/') {
    e.preventDefault();
    if (pickerOpen) closePicker(); else openPicker();
    return;
  }
  if (!pickerOpen) return;
  if      (e.key === 'Escape')    { e.preventDefault(); closePicker(); }
  else if (e.key === 'ArrowDown') { e.preventDefault(); pickerMoveActive(1); }
  else if (e.key === 'ArrowUp')   { e.preventDefault(); pickerMoveActive(-1); }
  else if (e.key === 'Enter')     { e.preventDefault(); pickerConfirmActive(); }
});

document.getElementById('picker-list').addEventListener('click', function(e) {
  const item = e.target.closest('.picker-item');
  if (item) pickerSelect(item.dataset.id);
});

document.getElementById('picker-search').addEventListener('keydown', function(e) {
  if (e.key === 'Tab') e.preventDefault();
});

document.getElementById('picker-search').addEventListener('input', pickerFilter);

document.getElementById('caption-text').addEventListener('input', function() {
  captionDirty = true;
  checkShortcode();
  scheduleHighlights();
});

document.getElementById('caption-text').addEventListener('keydown', function(e) {
  if (scodeItems.length) {
    if      (e.key === 'ArrowDown') { e.preventDefault(); scodeMoveActive(1); return; }
    else if (e.key === 'ArrowUp')   { e.preventDefault(); scodeMoveActive(-1); return; }
    else if ((e.key === 'Enter' || e.key === 'Tab') && scodeActive >= 0 && scodeActive < scodeItems.length) {
      e.preventDefault(); applyShortcode(scodeItems[scodeActive][1].id); return;
    }
    else if (e.key === 'Escape') { e.preventDefault(); closeScodeHint(); return; }
  }
  if (e.key === 'Tab' && !e.ctrlKey && !e.altKey && !e.metaKey) {
    const fillable = getFillablePlaceholders();
    if (!fillable.size) return;
    e.preventDefault();
    const ta = this, text = ta.value;
    const selStart = ta.selectionStart, selEnd = ta.selectionEnd;
    const re = /\{([a-z][a-z0-9_]*)\}/g;
    let match, found = null;
    if (!e.shiftKey) {
      while ((match = re.exec(text)) !== null) {
        if (fillable.has(match[1]) && match.index >= selEnd) { found = match; break; }
      }
    } else {
      while ((match = re.exec(text)) !== null) {
        if (fillable.has(match[1]) && match.index + match[0].length <= selStart) found = match;
      }
    }
    if (found) { ta.selectionStart = found.index; ta.selectionEnd = found.index + found[0].length; }
  }
});

document.getElementById('caption-text').addEventListener('blur', function() {
  setTimeout(closeScodeHint, 150);
});

document.getElementById('caption-text').addEventListener('scroll', function() {
  const hl = document.getElementById('cap-highlights');
  if (hl) hl.style.marginTop = -this.scrollTop + 'px';
});

document.getElementById('sc-hint').addEventListener('mousedown', function(e) {
  const item = e.target.closest('.sc-item');
  if (item) { e.preventDefault(); applyShortcode(item.dataset.id); }
});

loadTemplateSelect();
loadMeta();
</script>
</body>
</html>'''


# ── Run ────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    host = os.environ.get("HOST", "127.0.0.1")
    port = int(os.environ.get("PORT", 5000))
    if host in ("127.0.0.1", "localhost"):
        import webbrowser
        webbrowser.open(f"http://localhost:{port}")
    app.run(debug=False, host=host, port=port, use_reloader=False)
