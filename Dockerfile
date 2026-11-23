FROM python:3.11-slim

# Chrome (for nodriver) + Xvfb (so headful Chrome can run on a display-less
# server — see SPEC.md, KASADA bot-detection notes: headless=False evades
# more detection than true headless mode, so we keep the same real-browser
# posture in the container by giving it a virtual display) + the shared libs
# Chrome needs to launch at all on a minimal Debian base.
RUN apt-get update && apt-get install -y --no-install-recommends \
        wget gnupg ca-certificates fonts-liberation xvfb procps \
        libnss3 libatk-bridge2.0-0 libx11-xcb1 libxcomposite1 libxdamage1 \
        libxrandr2 libgbm1 libasound2 libpangocairo-1.0-0 libxshmfence1 \
        libxfixes3 libdrm2 libgtk-3-0 libxss1 \
    && wget -q -O /tmp/chrome.deb https://dl.google.com/linux/direct/google-chrome-stable_current_amd64.deb \
    && apt-get install -y --no-install-recommends /tmp/chrome.deb \
    && rm /tmp/chrome.deb \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt requirements-server.txt ./
RUN pip install --no-cache-dir -r requirements-server.txt

COPY app.py scrape_listing.py ./
# Baked-in starter templates live here, not at the runtime /app/templates
# path — that path gets bind-mounted from the host so template edits persist
# across redeploys, which would otherwise hide these files. Copied in by
# docker-entrypoint.sh on first run only.
COPY templates/ ./templates_seed/
COPY docker-entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

ENV NODRIVER_NO_SANDBOX=1 \
    DISPLAY=:99 \
    HOST=0.0.0.0 \
    PORT=5000 \
    PYTHONUNBUFFERED=1

EXPOSE 5000
ENTRYPOINT ["/entrypoint.sh"]
