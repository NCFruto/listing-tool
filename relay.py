import os
import sys

# Same reason as app.py — see SPEC.md > Distribution.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import json
from pathlib import Path

import requests
from flask import Flask, request, jsonify

from scrape_listing import (
    extract_details,
    extract_listing_id,
    extract_photo_urls,
    fetch_page_html,
)

# Listing Tool Relay — runs on a teammate's own machine so page-fetches use
# their home/office IP instead of the shared VPS's IP, which
# realestate.com.au's KASADA bot-protection blocks outright (confirmed: even
# a paid residential proxy pool still got blocked — see SPEC.md, "Shared-IP
# scraping risk"). The shared webapp's "Get Listing" button calls this
# relay's /api/scrape instead of the central server directly; this relay
# does the real fetch+extract locally, then forwards the result to the
# central server's /api/ingest-listing, and hands the response straight back
# to the browser.

CONFIG_PATH = Path(__file__).parent / "relay_config.json"
DEFAULT_CONFIG = {
    "central_url": "https://209-38-94-2.sslip.io",
    "relay_token": "REPLACE_ME",
    "port": 5051,
}


def load_config():
    if CONFIG_PATH.exists():
        try:
            return {**DEFAULT_CONFIG, **json.loads(CONFIG_PATH.read_text(encoding="utf-8"))}
        except Exception:
            pass
    return DEFAULT_CONFIG


CONFIG = load_config()

app = Flask(__name__)


@app.after_request
def add_cors(resp):
    # The shared webapp's page (loaded from the central server) calls this
    # relay cross-origin from the browser, so it needs CORS headers.
    resp.headers["Access-Control-Allow-Origin"] = "*"
    # Chrome's Private Network Access spec requires this explicit opt-in on
    # top of normal CORS before a page is allowed to reach localhost/private
    # IPs at all — without it modern Chrome blocks the request outright.
    resp.headers["Access-Control-Allow-Private-Network"] = "true"
    resp.headers["Access-Control-Allow-Methods"] = "POST, OPTIONS"
    resp.headers["Access-Control-Allow-Headers"] = "Content-Type"
    return resp


@app.route("/api/scrape", methods=["POST", "OPTIONS"])
def scrape():
    if request.method == "OPTIONS":
        return ("", 204)

    url = (request.get_json() or {}).get("url", "").strip()
    if not url:
        return jsonify({"error": "No URL provided."}), 400
    try:
        extract_listing_id(url)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400

    try:
        print(f"  Relay: fetching {url} from this machine...")
        html = fetch_page_html(url)
        details = extract_details(html, url)
        photo_urls = extract_photo_urls(html)
    except Exception as e:
        return jsonify({"error": f"Failed to load page: {e}"}), 500

    try:
        r = requests.post(
            CONFIG["central_url"].rstrip("/") + "/api/ingest-listing",
            json={"url": url, "details": details, "photo_urls": photo_urls},
            headers={"X-Relay-Token": CONFIG["relay_token"]},
            timeout=120,
        )
        r.raise_for_status()
        return jsonify(r.json())
    except Exception as e:
        return jsonify({
            "error": f"Fetched the listing okay, but couldn't send it to the shared app: {e}",
            "hint": "Check your internet connection and that the shared app is online, then try again.",
        }), 502


if __name__ == "__main__":
    port = int(CONFIG.get("port", 5051))
    if CONFIG.get("relay_token") == "REPLACE_ME":
        print("WARNING: relay_config.json still has the placeholder relay_token — ask NC for the real one.")
    print(f"Listing Tool Relay running on http://localhost:{port}")
    print(f"Forwarding scraped listings to: {CONFIG['central_url']}")
    print("Keep this window open while using the shared Listing Tool webapp.")
    app.run(host="127.0.0.1", port=port, debug=False, use_reloader=False)
