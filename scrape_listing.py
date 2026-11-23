import argparse
import asyncio
import json
import os
import re
import sys
import threading
import time
import webbrowser
from concurrent.futures import ThreadPoolExecutor
from io import BytesIO
from pathlib import Path

import nodriver as nd
import requests
from bs4 import BeautifulSoup
from PIL import Image


# ── Helpers ──────────────────────────────────────────────────────────────────

def extract_listing_id(url):
    match = re.search(r"-(\d{6,})(?:[/?#]|$)", url)
    if not match:
        raise ValueError(f"Could not extract listing ID from URL: {url}")
    return match.group(1)


# ── Browser fetch ─────────────────────────────────────────────────────────────

def _is_kasada_challenge(html):
    """Return True when the page source is the KASADA bot-detection challenge, not real content."""
    return len(html) < 10_000 and ("KPSDK" in html or "kpsdk" in html.lower())


async def _stable_get_content(page, retries=3, delay=1.0):
    """page.get_content() can raise a stale-CDP-node error ("Could not find
    node with given id") if called while the page is mid-navigation — most
    likely right as the KASADA challenge clears and the real page loads in.
    Retry a few times instead of failing the whole scrape on that race."""
    last_err = None
    for _ in range(retries):
        try:
            return await page.get_content()
        except Exception as e:
            last_err = e
            await asyncio.sleep(delay)
    raise last_err


async def _fetch_html_async(url):
    """Fetch a listing page using nodriver (CDP-native, no chromedriver binary)."""
    # Docker runs Chrome as root, which refuses to start without --no-sandbox.
    # Set by the container entrypoint; unset (default) on local installs, so
    # Chrome's sandbox stays on there as before.
    no_sandbox = os.environ.get("NODRIVER_NO_SANDBOX") == "1"
    browser_args = ["--disable-dev-shm-usage"] if no_sandbox else []

    # Datacenter VPS IPs get a hard, fingerprint-independent block from
    # realestate.com.au's KASADA protection (confirmed — even the homepage
    # never clears the challenge from a DigitalOcean IP, regardless of
    # browser fingerprint quality). SCRAPE_PROXY routes Chrome through a
    # residential proxy instead. Format: http://user:pass@host:port
    # (nodriver runs a small local auth-forwarder for this — no Chrome
    # proxy-auth popup to worry about). Unset locally; local installs are
    # unaffected.
    proxy = os.environ.get("SCRAPE_PROXY")
    if proxy:
        # Images aren't needed — photo URLs come from inline JS/DOM text,
        # actual photo bytes are downloaded separately over plain requests
        # (not through the proxy). Skipping them cuts proxy bandwidth cost.
        browser_args.append("--blink-settings=imagesEnabled=false")

    browser = await nd.start(
        headless=False, sandbox=not no_sandbox, browser_args=browser_args or None
    )
    try:
        if proxy:
            page = await browser.create_context(url=url, proxy_server=proxy)
        else:
            page = await browser.get(url)

        # realestate.com.au uses KASADA bot-protection which serves a ~773-byte
        # JS challenge on first load.  nodriver (CDP-native, no chromedriver
        # binary) avoids the main detection vectors; we still poll up to 45 s
        # in case the challenge takes a moment to resolve.
        deadline = time.time() + 45
        content_ready = False
        while time.time() < deadline:
            html_check = await _stable_get_content(page)
            if _is_kasada_challenge(html_check):
                await asyncio.sleep(2)
                continue
            # Real page — wait for the description to render
            try:
                await page.find('[data-testid="PropertyDescription"]', timeout=5)
                content_ready = True
                break
            except Exception:
                await asyncio.sleep(1)

        if not content_ready:
            await asyncio.sleep(3)

        # Attempt to reveal full phone numbers by clicking call buttons
        try:
            tel_links = await page.query_selector_all('a[href^="tel:"]')
        except Exception:
            tel_links = []  # best-effort — a stale-node race here shouldn't fail the whole scrape
        if not tel_links:
            try:
                all_buttons = await page.query_selector_all("button")
                seen = set()
                clicked = 0
                for btn in all_buttons[:20]:
                    try:
                        label = (await btn.get_attribute("data-testid") or "").lower()
                        text  = (await btn.get_attribute("innerText") or "").lower()
                        if "call" in label or "call" in text:
                            bid = id(btn)
                            if bid not in seen:
                                seen.add(bid)
                                await btn.click()
                                await asyncio.sleep(0.5)
                                clicked += 1
                    except Exception:
                        pass
                if clicked:
                    await asyncio.sleep(1)
            except Exception:
                pass

        html = await _stable_get_content(page)
        if _is_kasada_challenge(html):
            raise RuntimeError(
                "realestate.com.au bot-detection (KASADA) blocked this request. "
                "The site may be temporarily blocking automated requests — "
                "try again in a few minutes."
            )
        return html
    finally:
        browser.stop()


def _fetch_html_via_unlocker(url, api_key):
    """Fetch via a third-party scraping-unlocker API instead of our own browser.

    These services (ScraperAPI, ZenRows, ScrapingBee, ...) run their own
    residential-proxy + headless-browser infra tuned specifically to bypass
    protections like KASADA/Cloudflare/PerimeterX, so this bypasses our own
    datacenter-IP problem entirely if it works. Provider chosen via
    SCRAPE_API_PROVIDER (default: scraperapi); swap providers by changing
    that env var, no code change needed.
    """
    provider = os.environ.get("SCRAPE_API_PROVIDER", "scraperapi").lower()
    print(f"  Fetching via {provider} unlocker API...")

    if provider == "scraperapi":
        resp = requests.get(
            "https://api.scraperapi.com/",
            params={"api_key": api_key, "url": url, "render": "true", "premium": "true"},
            timeout=90,
        )
    elif provider == "zenrows":
        resp = requests.get(
            "https://api.zenrows.com/v1/",
            params={"apikey": api_key, "url": url, "js_render": "true", "premium_proxy": "true"},
            timeout=90,
        )
    elif provider == "scrapingbee":
        resp = requests.get(
            "https://app.scrapingbee.com/api/v1/",
            params={"api_key": api_key, "url": url, "render_js": "true", "premium_proxy": "true"},
            timeout=90,
        )
    else:
        raise RuntimeError(f"Unknown SCRAPE_API_PROVIDER: {provider!r}")

    resp.raise_for_status()
    html = resp.text
    if _is_kasada_challenge(html):
        raise RuntimeError(
            f"{provider} still returned a KASADA challenge page — it didn't bypass the block."
        )
    return html


def fetch_page_html(url):
    api_key = os.environ.get("SCRAPE_API_KEY")
    if api_key:
        return _fetch_html_via_unlocker(url, api_key)
    print("  Launching browser (this takes ~10 seconds)...")
    # Use nodriver's own loop to avoid Windows ProactorEventLoop cleanup noise.
    return nd.loop().run_until_complete(_fetch_html_async(url))


# ── Listing details extraction ────────────────────────────────────────────────

def extract_details(html, url):
    soup = BeautifulSoup(html, "html.parser")

    # ── JSON-LD (parse once, reuse everywhere) ───────────────────────────────────
    jsonld_scripts = soup.find_all("script", type="application/ld+json")
    jsonld_data = []
    for s in jsonld_scripts:
        try:
            parsed = json.loads(s.string)
            if isinstance(parsed, list):
                jsonld_data.extend(parsed)
            elif isinstance(parsed, dict):
                jsonld_data.append(parsed)
        except Exception:
            pass

    # ── Address ──────────────────────────────────────────────────────────────────
    address = None
    title_tag = soup.find("title")
    if title_tag:
        # Match both old format ("... - House for Sale") and new format ("... - Property Details")
        m = re.match(
            r"^(.+?)\s*-\s*(House|Unit|Apartment|Townhouse|Land|Rural|Acreage|Villa|Studio|Duplex|Terrace|Semi|Property\s+Details|Sold|For\s+Sale)",
            title_tag.text, re.IGNORECASE
        )
        if m:
            address = m.group(1).strip()
    if not address:
        # JSON-LD fallback: use only street + suburb to avoid state/postcode noise
        for obj in jsonld_data:
            addr = obj.get("address") or {}
            parts = [addr.get("streetAddress"), addr.get("addressLocality")]
            candidate = ", ".join(p for p in parts if p)
            if candidate:
                address = candidate
                break
    # Strip trailing state code + postcode in either format:
    #   ", NSW 2107"  (old title format — space between state and postcode)
    #   ", NSW, 2107" (new title format — comma between state and postcode)
    if address:
        address = re.sub(
            r",\s*(?:NSW|VIC|QLD|SA|WA|TAS|NT|ACT)[,\s]+\d{4}\s*$", "", address, flags=re.IGNORECASE
        ).strip()

    # ── Property type ─────────────────────────────────────────────────────────────
    property_type = None
    # Primary: from URL path (most reliable)
    _type_re = r"/property-(house|unit|apartment|townhouse|land|rural|acreage|villa|studio|duplex|terrace|semi)[+\-]"
    _tm = re.search(_type_re, url, re.IGNORECASE)
    if _tm:
        property_type = _tm.group(1).replace("-", " ").capitalize()
    # Fallback: from page title
    if not property_type and title_tag:
        _tm2 = re.search(r"-\s*(house|unit|apartment|townhouse|land|rural|acreage|villa|studio|duplex|terrace|semi)\s*(?:for sale|-)", title_tag.text, re.IGNORECASE)
        if _tm2:
            property_type = _tm2.group(1).capitalize()
    # Fallback: from aria-label
    if not property_type:
        _al = soup.find(True, {"aria-label": re.compile(r"(house|unit|apartment|townhouse|land|acreage|villa|studio|duplex|terrace|semi)", re.IGNORECASE)})
        if _al:
            _tm3 = re.match(r"(house|unit|apartment|townhouse|land|acreage|villa|studio|duplex|terrace|semi)", _al.get("aria-label", ""), re.IGNORECASE)
            if _tm3:
                property_type = _tm3.group(1).capitalize()

    # ── Beds / baths / cars ───────────────────────────────────────────────────────
    bedrooms = bathrooms = car_spaces = None
    _summary = soup.find(True, {"aria-label": re.compile(r"bedroom|bathroom|car\s*space|garage", re.IGNORECASE)})
    if _summary:
        label = _summary.get("aria-label", "")
        _bm = re.search(r"(\d+)\s*bedroom", label, re.IGNORECASE)
        _ba = re.search(r"(\d+)\s*bathroom", label, re.IGNORECASE)
        _cm = re.search(r"(\d+)\s*(car|garage)", label, re.IGNORECASE)
        bedrooms   = int(_bm.group(1)) if _bm else None
        bathrooms  = int(_ba.group(1)) if _ba else None
        car_spaces = int(_cm.group(1)) if _cm else None

    # ── Land size ────────────────────────────────────────────────────────────────
    land_size = None
    _land_re = re.compile(r"Land\s*[Ss]ize[:\s]*([\d,\.]+\s*(?:m²|m2|sqm|ha|acres?))", re.IGNORECASE)
    for _testid in ("all-property-features-section", "top-property-features-section"):
        _feat_el = soup.find(True, {"data-testid": _testid})
        if _feat_el:
            _lm = _land_re.search(_feat_el.get_text(" ", strip=True))
            if _lm:
                land_size = _lm.group(1).strip()
                break
    # Fallback: extract from description text
    if not land_size:
        _page_text = soup.get_text(" ", strip=True)
        _lm2 = re.search(r"([\d,\.]+\s*(?:m²|m2|sqm|ha|hectares?|acres?))\s*(?:parcel|block|lot|land|property)", _page_text, re.IGNORECASE)
        if _lm2:
            land_size = _lm2.group(1).strip()

    # ── Price / auction ───────────────────────────────────────────────────────────
    price = auction = None
    _meta = soup.find("meta", attrs={"name": "description"}) or soup.find("meta", attrs={"property": "og:description"})
    if _meta:
        _meta_content = _meta.get("content", "")
        _pm = re.search(r"((?:Auction\s+Guide\s+)?\$[\d,.]+(?:m|k)?(?:\s*[-–]\s*\$[\d,.]+(?:m|k)?)?)", _meta_content, re.IGNORECASE)
        if _pm:
            price = _pm.group(1).strip().rstrip(".")
        elif re.search(r"contact\s+agent", _meta_content, re.IGNORECASE):
            price = "Contact Agent"
    else:
        _meta_content = ""
    # Fallback: look for price in key sections only (not KEY DETAILS/rates which contain $NNN)
    if not price:
        # Only search in the listing header / price display sections, not the full page
        _price_sections = []
        for _pid in ("listing-price", "price-display", "listing-details-header"):
            _pe = soup.find(True, {"data-testid": _pid})
            if _pe:
                _price_sections.append(_pe.get_text(" ", strip=True))
        # Also try og:title which sometimes has the price
        _og_title = soup.find("meta", attrs={"property": "og:title"})
        if _og_title:
            _price_sections.append(_og_title.get("content", ""))
        for _section_text in _price_sections:
            _pm2 = re.search(r"((?:Auction\s+Guide\s+)?\$[\d,.]+(?:m|k)?(?:\s*[-–]\s*\$[\d,.]+(?:m|k)?)?)", _section_text, re.IGNORECASE)
            if _pm2 and len(_pm2.group(0)) < 35:
                price = _pm2.group(0).strip().rstrip(".")
                break

    for obj in jsonld_data:
        if obj.get("@type") == "SaleEvent":
            auction = obj.get("startDate")
            if auction:
                try:
                    from datetime import datetime
                    clean = re.sub(r"[+-]\d{2}:\d{2}$", "", auction)
                    dt = datetime.fromisoformat(clean)
                    auction = dt.strftime("%d %B %Y, %I:%M %p").lstrip("0")
                except Exception:
                    pass
            break

    # ── Headline + description + features ────────────────────────────────────────
    headline = description = description_full = None
    features = []
    desc_el = soup.find(True, {"data-testid": "PropertyDescription"})
    if desc_el:
        h2 = desc_el.find("h2")
        headline = h2.get_text(strip=True) if h2 else None

        p_body = desc_el.find("p", class_="property-description__content")
        raw_body = p_body.get_text("\n", strip=True) if p_body else desc_el.get_text("\n", strip=True)
        description_full = raw_body

        lines = raw_body.split("\n")
        prose_lines = []
        in_bullets = False
        for line in lines:
            s = line.rstrip()
            if re.match(r"^[-•]\s", s):
                in_bullets = True
                features.append(re.sub(r"^[-•]\s+", "", s))
            elif re.match(r"^(DISCLAIMER|KEY\s+DETAILS|Read more)", s, re.IGNORECASE):
                break
            elif not in_bullets:
                prose_lines.append(s)
        description = re.sub(r"\n{3,}", "\n\n", "\n".join(prose_lines)).strip()

    # ── Agency name ───────────────────────────────────────────────────────────────
    agency_name = None
    # 1. branding__image alt — full agency name, works for all listing types
    _brand_img = soup.find("img", class_="branding__image")
    if _brand_img and _brand_img.get("alt"):
        agency_name = _brand_img["alt"].strip()
    # 2. JSON-LD SaleEvent organizer (auction listings, fallback)
    if not agency_name:
        for obj in jsonld_data:
            if obj.get("@type") == "SaleEvent":
                agency_name = (obj.get("organizer") or {}).get("name")
                if agency_name:
                    break
    # 3. Enquiry form heuristic (last resort)
    if not agency_name:
        _enq = soup.find(True, {"data-testid": "enquiry-form"})
        if _enq:
            _enq_text = _enq.get_text("|", strip=True)
            for part in _enq_text.split("|"):
                part = part.strip()
                if (5 < len(part) < 80 and " " in part
                        and not re.match(r"^[A-Z][a-z]+ [A-Z][a-z]+$", part)
                        and not any(w in part.lower() for w in ["review", "call", "message", "sign", "enquire", "contact", "rating"])):
                    _words = part.split()
                    if 2 <= len(_words) <= 6 and any(w[0].isupper() for w in _words):
                        agency_name = part
                        break

    # ── Sale status ───────────────────────────────────────────────────────────────
    if auction:
        sale_status = "Auction"
    elif price and re.search(r"contact\s+agent", price, re.IGNORECASE):
        sale_status = "Contact Agent"
    else:
        _status_text = (title_tag.get_text() if title_tag else "") + " " + _meta_content
        if re.search(r"\bsold\b", _status_text, re.IGNORECASE):
            sale_status = "Sold"
        elif re.search(r"under\s+offer", _status_text, re.IGNORECASE):
            sale_status = "Under Offer"
        else:
            sale_status = "For Sale"

    # ── Full phone numbers: tel: links, data attrs, then embedded JSON ──────────
    def _au_mobile(raw):
        """Return clean 10-digit AU phone string if valid, else None."""
        norm = re.sub(r"[\s\-\+\(\)]", "", raw or "")
        return norm if re.match(r"^0\d{9}$", norm) else None

    def _dedup(lst):
        seen, out = set(), []
        for p in lst:
            if p not in seen:
                seen.add(p); out.append(p)
        return out

    # 1. tel: href attributes — full number even when button text is truncated
    tel_phones = []
    for el in soup.find_all("a", href=re.compile(r"^tel:", re.I)):
        p = _au_mobile(el["href"][4:])
        if p:
            tel_phones.append(p)
    # Also catch data-* attributes (e.g. data-phone, data-number)
    for el in soup.find_all(True):
        for attr, val in el.attrs.items():
            if isinstance(val, str) and "phone" in attr.lower():
                p = _au_mobile(val)
                if p:
                    tel_phones.append(p)
    tel_phones = _dedup(tel_phones)

    # 2. __NEXT_DATA__ / inline script JSON
    def _collect_phones(obj, result, depth=0):
        if depth > 20:
            return
        if isinstance(obj, dict):
            for key in ("phone", "phoneNumber", "mobilePhone", "telephone", "contactPhone"):
                p = _au_mobile(obj.get(key, ""))
                if p:
                    result.append(p)
            for v in obj.values():
                _collect_phones(v, result, depth + 1)
        elif isinstance(obj, list):
            for item in obj:
                _collect_phones(item, result, depth + 1)

    json_phones = []
    next_data_el = soup.find("script", id="__NEXT_DATA__")
    if next_data_el and next_data_el.string:
        try:
            _collect_phones(json.loads(next_data_el.string), json_phones)
        except Exception:
            pass
    if not json_phones:
        for script in soup.find_all("script"):
            text = script.string or ""
            for m in re.finditer(
                r'"(?:phone|phoneNumber|mobilePhone|telephone)"\s*:\s*"(0[45][\d\s]{8,11})"',
                text, re.IGNORECASE
            ):
                p = _au_mobile(m.group(1))
                if p:
                    json_phones.append(p)
    json_phones = _dedup(json_phones)

    # Best available full phones (tel: links beat JSON)
    full_phones_found = _dedup(tel_phones + json_phones)

    # ── Agents ──
    agents = []
    enq_el = soup.find(True, {"data-testid": "enquiry-form"})
    if enq_el:
        raw_text = enq_el.get_text("|", strip=True)
        parts = raw_text.split("|")
        agent_names = []
        phones = []
        _name_re = re.compile(
            r"^[A-Z][a-zA-Z\-\']*(\s+[A-Z][a-zA-Z\-\']+)+$"
        )
        _skip = {"Call", "Message", "Email", "Sign", "Join", "Enquire", "Contact",
                 "Send", "Submit", "View", "Profile", "Reviews", "Review", "Rating"}
        for part in parts:
            part = part.strip()
            # Normalise ALL-CAPS to Title Case (REA sometimes renders names in caps)
            words = part.split()
            if all(w.isupper() for w in words if w.isalpha()):
                part = " ".join(w.capitalize() for w in words)
            if (2 <= len(words) <= 4
                    and _name_re.match(part)
                    and len(part) < 60
                    and not any(part.lower().startswith(s.lower()) for s in _skip)):
                agent_names.append(part)
            p = _au_mobile(part)
            if p:
                phones.append(p)

        # Prefer full phones found via tel: links / JSON over truncated UI text
        if full_phones_found and len(full_phones_found) >= len(phones):
            phones = full_phones_found
        elif not phones and full_phones_found:
            phones = full_phones_found

        # Last resort: keep truncated text so caller at least knows a number exists
        if not phones:
            phone_pattern = re.findall(r"(0\d[\d\s]{6,}(?:\.\.\.?)?)", raw_text)
            phones = [p.strip() for p in phone_pattern]

        for idx, name in enumerate(agent_names):
            phone = phones[idx] if idx < len(phones) else None
            agents.append({"name": name, "phone": phone, "agency": agency_name})

    return {
        "url": url,
        "address": address,
        "property_type": property_type,
        "bedrooms": bedrooms,
        "bathrooms": bathrooms,
        "car_spaces": car_spaces,
        "land_size": land_size,
        "price": price,
        "auction": auction,
        "sale_status": sale_status,
        "headline": headline,
        "description": description,
        "features": features,
        "description_full": description_full,
        "agency_name": agency_name,
        "agents": agents,
    }


# ── Photo extraction ──────────────────────────────────────────────────────────

def extract_photo_urls(html):
    # Get declared photo count from the page
    soup = BeautifulSoup(html, "html.parser")
    photo_count = None
    hero = soup.find(True, {"data-testid": "HeroMosaic"})
    if hero:
        count_el = hero.find(True, {"data-testid": "countIcon"})
        if count_el:
            try:
                photo_count = int(count_el.get_text(strip=True))
            except ValueError:
                pass

    # Identify hashes that are used for logos/thumbnails and exclude them
    logo_hashes = set(re.findall(r'([a-f0-9]{64})/logo\.jpg', html))
    main_hashes = set(re.findall(r'([a-f0-9]{64})/main\.jpg', html))
    exclude = logo_hashes | main_hashes

    # Extract full templatedUrl values and substitute the size token with
    # "original" to get the unresized, uncropped source image.
    # The JSON field looks like: "templatedUrl":"https://i2.au.reastatic.net/{size}/abc.../image.jpg"
    positions = [m.start() for m in re.finditer("templatedUrl", html)]
    seen = set()
    photo_urls = []
    for pos in positions:
        snippet = html[pos : pos + 600]
        # Try to grab the full URL template (with {size} or similar placeholder)
        tm = re.search(r'https://[^"\'\\]+/\{[^}]+\}/([a-f0-9]{64})/[^"\'\\]+', snippet)
        if tm:
            h = tm.group(1)
            if h not in seen and h not in exclude:
                seen.add(h)
                # Replace the {size} placeholder with "original"
                url = re.sub(r'\{[^}]+\}', 'original', tm.group(0))
                photo_urls.append(url)
        else:
            # Fallback: reconstruct from hash alone
            hm = re.search(r"[a-f0-9]{64}", snippet)
            if hm:
                h = hm.group()
                if h not in seen and h not in exclude:
                    seen.add(h)
                    photo_urls.append(f"https://i2.au.reastatic.net/original/{h}/image.jpg")

    # Take up to photo_count URLs (if we know the count)
    if photo_count and len(photo_urls) >= photo_count:
        photo_urls = photo_urls[:photo_count]

    return photo_urls


# ── Photo download ────────────────────────────────────────────────────────────

def download_as_jpeg(url, dest_path, session):
    resp = session.get(url, timeout=30)
    resp.raise_for_status()
    img = Image.open(BytesIO(resp.content)).convert("RGB")
    img.save(dest_path, "JPEG", quality=92)


# ── Preview HTML generation ───────────────────────────────────────────────────

def _first_paragraph(description):
    """Return the first meaningful paragraph of the description."""
    if not description:
        return ""
    for para in description.split("\n\n"):
        para = para.strip()
        # Skip short lines, headers, date stamps, bullet lists
        if (len(para) > 60
                and not para.startswith("•")
                and not para.startswith("DATE PUBLISHED")
                and not para.upper() == para):
            return para
    return description.split("\n")[0].strip()


def _features_line(details):
    parts = []
    if details.get("bedrooms") is not None:
        parts.append(f"{details['bedrooms']} Bed")
    if details.get("bathrooms") is not None:
        parts.append(f"{details['bathrooms']} Bath")
    if details.get("car_spaces") is not None:
        parts.append(f"{details['car_spaces']} Car")
    if details.get("land_size"):
        parts.append(details["land_size"])
    return " | ".join(parts)


def _agents_line(details):
    lines = []
    for agent in details.get("agents") or []:
        name = agent.get("name") or ""
        phone = agent.get("phone") or ""
        if name:
            lines.append(f"{name} | {phone}".strip(" |"))
    return "\n".join(lines)


def generate_preview_html(listing_id, photo_count, details):
    address = details.get("address") or "Unknown address"

    items = ""
    for i in range(1, photo_count + 1):
        items += f"""
        <div class="photo-item">
          <label>
            <input type="checkbox" value="{i}" checked>
            <img src="photos/{i:02d}.jpg" loading="lazy" alt="Photo {i}">
            <span class="num">{i}</span>
          </label>
        </div>"""

    beds = details.get("bedrooms")
    baths = details.get("bathrooms")
    cars = details.get("car_spaces")
    meta_parts = []
    if beds is not None:
        meta_parts.append(f"{beds} bed")
    if baths is not None:
        meta_parts.append(f"{baths} bath")
    if cars is not None:
        meta_parts.append(f"{cars} car")
    meta_str = " &nbsp;·&nbsp; ".join(meta_parts) if meta_parts else ""

    # Caption template parts (escaped for JS string literals)
    def js_str(s):
        return (s or "").replace("\\", "\\\\").replace("`", "\\`").replace("$", "\\$")

    caption_line1   = js_str(f"Just Listed | {address}")
    caption_para    = js_str(_first_paragraph(details.get("description", "")))
    caption_features = js_str(_features_line(details))
    caption_agents  = js_str(_agents_line(details))

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <title>Photo Selection — {address}</title>
  <style>
    *, *::before, *::after {{ box-sizing: border-box; margin: 0; padding: 0; }}
    body {{ font-family: system-ui, sans-serif; background: #f0f0f0; color: #222; padding: 24px; }}
    header {{ margin-bottom: 20px; }}
    h1 {{ font-size: 1.15rem; font-weight: 600; }}
    .meta {{ font-size: 0.85rem; color: #666; margin-top: 4px; }}
    .controls {{ display: flex; gap: 8px; margin-bottom: 18px; flex-wrap: wrap; align-items: center; }}
    .controls span {{ font-size: 0.85rem; color: #555; margin-left: 4px; }}
    button {{
      padding: 7px 14px; border: none; border-radius: 5px;
      font-size: 0.85rem; cursor: pointer; font-family: inherit;
    }}
    #btn-all {{ background: #e5e7eb; color: #333; }}
    #btn-none {{ background: #e5e7eb; color: #333; }}
    #btn-generate {{ background: #2563eb; color: #fff; font-weight: 600; }}
    .grid {{
      display: grid;
      grid-template-columns: repeat(auto-fill, minmax(200px, 1fr));
      gap: 10px;
    }}
    .photo-item label {{
      display: block; position: relative; cursor: pointer;
      border-radius: 6px; overflow: hidden;
      box-shadow: 0 1px 3px rgba(0,0,0,.15);
    }}
    .photo-item img {{
      display: block; width: 100%; aspect-ratio: 4/3;
      object-fit: cover; transition: opacity .15s;
    }}
    .photo-item input[type=checkbox] {{
      position: absolute; top: 7px; left: 7px;
      width: 17px; height: 17px; cursor: pointer; z-index: 1;
      accent-color: #2563eb;
    }}
    .photo-item input:not(:checked) + img {{ opacity: 0.4; }}
    .photo-item .num {{
      position: absolute; bottom: 6px; right: 7px;
      background: rgba(0,0,0,.55); color: #fff;
      font-size: 0.72rem; padding: 2px 6px; border-radius: 3px;
    }}
    #cmd-box {{
      display: none; margin-top: 24px; background: #1a1a2e;
      color: #e2e8f0; border-radius: 8px; padding: 18px 20px;
      font-family: monospace;
    }}
    #cmd-box p {{ font-family: system-ui; font-size: 0.82rem; color: #94a3b8; margin-bottom: 10px; }}
    #cmd-text {{ font-size: 0.9rem; word-break: break-all; line-height: 1.5; }}
    #btn-copy-cmd {{
      margin-top: 12px; background: #334155; color: #cbd5e1;
      border: 1px solid #475569; border-radius: 4px;
      padding: 5px 12px; font-size: 0.82rem; cursor: pointer;
    }}
    .caption-section {{
      margin-top: 32px; background: #fff; border-radius: 8px;
      padding: 20px 24px; box-shadow: 0 1px 3px rgba(0,0,0,.1);
    }}
    .caption-section h2 {{
      font-size: 0.95rem; font-weight: 600; margin-bottom: 14px; color: #111;
    }}
    .url-row {{
      display: flex; align-items: center; gap: 10px; margin-bottom: 14px;
    }}
    .url-row label {{ font-size: 0.85rem; color: #555; white-space: nowrap; }}
    .url-row input {{
      flex: 1; padding: 7px 10px; border: 1px solid #d1d5db;
      border-radius: 5px; font-size: 0.85rem; font-family: inherit;
    }}
    .url-row input:focus {{ outline: none; border-color: #2563eb; }}
    #caption-text {{
      width: 100%; min-height: 180px; padding: 12px 14px;
      border: 1px solid #d1d5db; border-radius: 5px;
      font-size: 0.9rem; font-family: inherit; line-height: 1.6;
      resize: vertical; color: #222;
    }}
    #caption-text:focus {{ outline: none; border-color: #2563eb; }}
    #btn-copy-caption {{
      margin-top: 10px; background: #16a34a; color: #fff;
      font-weight: 600; border-radius: 5px;
      padding: 8px 18px; font-size: 0.85rem; cursor: pointer; border: none;
    }}
  </style>
</head>
<body>
  <header>
    <h1>{address}</h1>
    <p class="meta">ID: {listing_id} &nbsp;·&nbsp; {photo_count} photos {("&nbsp;·&nbsp; " + meta_str) if meta_str else ""}</p>
  </header>
  <div class="controls">
    <button id="btn-all">Select All</button>
    <button id="btn-none">Deselect All</button>
    <button id="btn-generate">Generate Command</button>
    <span id="count-label"></span>
  </div>
  <div class="grid">{items}
  </div>
  <div id="cmd-box">
    <p>Run this in your terminal to copy the selected photos:</p>
    <div id="cmd-text"></div>
    <button id="btn-copy-cmd">Copy to clipboard</button>
  </div>

  <div class="caption-section">
    <h2>Caption</h2>
    <div class="url-row">
      <label for="short-url">Short URL:</label>
      <input type="text" id="short-url" placeholder="https://bit.ly/..." oninput="updateCaption()">
    </div>
    <textarea id="caption-text" spellcheck="true"></textarea>
    <br>
    <button id="btn-copy-caption">Copy Caption</button>
  </div>

  <script>
    const LINE1     = `{caption_line1}`;
    const PARA      = `{caption_para}`;
    const FEATURES  = `{caption_features}`;
    const AGENTS    = `{caption_agents}`;

    function buildCaption(shortUrl) {{
      const seeMore = shortUrl ? `See more: ${{shortUrl}}` : `See more: [add short URL]`;
      return [LINE1, PARA, FEATURES, seeMore, AGENTS]
        .filter(s => s.trim())
        .join('\\n\\n');
    }}

    function updateCaption() {{
      const url = document.getElementById('short-url').value.trim();
      document.getElementById('caption-text').value = buildCaption(url);
    }}

    // Initialise caption on load
    updateCaption();

    // Photo selection
    const checkboxes = () => [...document.querySelectorAll('input[type=checkbox]')];
    function updateCount() {{
      const sel = checkboxes().filter(c => c.checked).length;
      document.getElementById('count-label').textContent = sel + ' selected';
    }}
    document.querySelectorAll('input[type=checkbox]').forEach(cb => cb.addEventListener('change', updateCount));
    document.getElementById('btn-all').onclick = () => {{ checkboxes().forEach(c => c.checked = true); updateCount(); }};
    document.getElementById('btn-none').onclick = () => {{ checkboxes().forEach(c => c.checked = false); updateCount(); }};
    document.getElementById('btn-generate').onclick = () => {{
      const sel = checkboxes().filter(c => c.checked).map(c => c.value).join(',');
      if (!sel) {{ alert('No photos selected.'); return; }}
      const cmd = 'python finalize.py {listing_id} --keep ' + sel;
      document.getElementById('cmd-text').textContent = cmd;
      document.getElementById('cmd-box').style.display = 'block';
      document.getElementById('cmd-box').scrollIntoView({{ behavior: 'smooth' }});
    }};
    document.getElementById('btn-copy-cmd').onclick = () => {{
      navigator.clipboard.writeText(document.getElementById('cmd-text').textContent).then(() => {{
        const btn = document.getElementById('btn-copy-cmd');
        btn.textContent = 'Copied!';
        setTimeout(() => btn.textContent = 'Copy to clipboard', 2000);
      }});
    }};
    document.getElementById('btn-copy-caption').onclick = () => {{
      const text = document.getElementById('caption-text').value;
      navigator.clipboard.writeText(text).then(() => {{
        const btn = document.getElementById('btn-copy-caption');
        btn.textContent = 'Copied!';
        setTimeout(() => btn.textContent = 'Copy Caption', 2000);
      }});
    }};
    updateCount();
  </script>
</body>
</html>"""


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Scrape a realestate.com.au listing: download photos and extract details."
    )
    parser.add_argument("url", help="Full realestate.com.au listing URL")
    args = parser.parse_args()

    url = args.url.strip()
    listing_id = extract_listing_id(url)
    print(f"Listing ID : {listing_id}")

    base_dir = Path("listings") / listing_id
    photos_dir = base_dir / "photos"
    photos_dir.mkdir(parents=True, exist_ok=True)

    print("Fetching listing page...")
    try:
        html = fetch_page_html(url)
    except Exception as e:
        print(f"Error fetching page: {e}", file=sys.stderr)
        sys.exit(1)

    print("Extracting listing details...")
    details = extract_details(html, url)
    listing_json = base_dir / "listing.json"
    listing_json.write_text(json.dumps(details, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"  Saved listing.json")
    if details.get("address"):
        print(f"  Address  : {details['address']}")
    if details.get("bedrooms") is not None:
        print(f"  Features : {details['bedrooms']} bed | {details['bathrooms']} bath | {details['car_spaces']} car")
    if details.get("price"):
        print(f"  Price    : {details['price']}")
    if details.get("auction"):
        print(f"  Auction  : {details['auction']}")

    photo_urls = extract_photo_urls(html)
    if not photo_urls:
        print("WARNING: No photos found.", file=sys.stderr)
    else:
        print(f"Found {len(photo_urls)} photos — downloading...")

    session = requests.Session()
    session.headers.update({
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    })

    _total = len(photo_urls)
    _done = [0]
    _lock = threading.Lock()

    def _download_one(args):
        i, photo_url = args
        dest = photos_dir / f"{i:02d}.jpg"
        try:
            download_as_jpeg(photo_url, dest, session)
            with _lock:
                _done[0] += 1
                print(f"  {_done[0]}/{_total} downloaded", end="\r")
            return True
        except Exception as e:
            print(f"\n  WARNING: Failed to download photo {i}: {e}")
            return False

    with ThreadPoolExecutor(max_workers=6) as pool:
        results = list(pool.map(_download_one, enumerate(photo_urls, 1)))

    downloaded = sum(results)
    print(f"\n  Downloaded {downloaded}/{_total} photos")

    preview_path = base_dir / "preview.html"
    preview_path.write_text(
        generate_preview_html(listing_id, downloaded, details), encoding="utf-8"
    )
    print("Opening preview in browser...")
    webbrowser.open(preview_path.resolve().as_uri())
    print(f"\nDone. Files saved to: listings/{listing_id}/")


if __name__ == "__main__":
    main()
