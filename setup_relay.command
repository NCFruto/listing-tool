#!/bin/bash
cd "$(dirname "$0")"

echo "============================================================"
echo "  Listing Tool Relay - First-time Setup (Mac)"
echo "  This will take 2-3 minutes. Please don't close this window."
echo "============================================================"
echo

# ── Pre-flight: required files ──────────────────────────────────────────────
for f in relay.py scrape_listing.py requirements.txt relay_config.json; do
    if [ ! -f "$f" ]; then
        echo "ERROR: Missing file: $f"
        echo "The download may be incomplete. Re-download the zip, extract ALL"
        echo "of it, then try again."
        read -r -p "Press Enter to close..."
        exit 1
    fi
done
echo "All required files present. OK."
echo

# ── 1/4  Check for Google Chrome ────────────────────────────────────────────
if [ ! -d "/Applications/Google Chrome.app" ]; then
    echo "ERROR: Google Chrome was not found in /Applications."
    echo "Chrome is required for the relay to fetch listings."
    echo
    echo "Install it from https://google.com/chrome, then run this again."
    open "https://google.com/chrome" 2>/dev/null
    read -r -p "Press Enter to close..."
    exit 1
fi
echo "[1/4] Chrome found. OK."

# ── 2/4  Check for Python 3 ─────────────────────────────────────────────────
if ! command -v python3 >/dev/null 2>&1; then
    echo
    echo "ERROR: Python 3 was not found on this Mac."
    echo "Install it from https://www.python.org/downloads/macos/"
    echo "(the latest version is fine — default install options work)."
    echo "Then run this setup again."
    open "https://www.python.org/downloads/macos/" 2>/dev/null
    read -r -p "Press Enter to close..."
    exit 1
fi
echo "[2/4] Python found: $(python3 --version)"

# ── 3/4  Isolated virtual environment + packages ────────────────────────────
echo "[3/4] Installing packages (this may take 1-2 minutes)..."
if [ ! -d "venv" ]; then
    python3 -m venv venv || {
        echo "ERROR: Could not create a Python environment. Contact NC for help."
        read -r -p "Press Enter to close..."
        exit 1
    }
fi

./venv/bin/pip install --quiet --upgrade pip
./venv/bin/pip install -r requirements.txt || {
    echo "ERROR: Package installation failed. Contact NC for help."
    read -r -p "Press Enter to close..."
    exit 1
}
echo "All packages installed. OK."

# ── 4/4  Smoke test ──────────────────────────────────────────────────────────
./venv/bin/python -c "import flask, requests, bs4, PIL, nodriver" || {
    echo "ERROR: Verification failed - a package did not install correctly."
    echo "Contact NC for help."
    read -r -p "Press Enter to close..."
    exit 1
}
echo "[4/4] Installation verified. OK."

# Desktop shortcut (a symlink to start_relay.command) so daily use doesn't
# require remembering where this folder is — mirrors the Windows installer's
# desktop shortcut.
chmod +x start_relay.command setup_relay.command 2>/dev/null
ln -sf "$(pwd)/start_relay.command" "$HOME/Desktop/Listing Tool Relay.command" 2>/dev/null

echo
echo "============================================================"
echo "  Setup complete! Starting the relay helper now..."
echo "============================================================"
echo
echo "  Leave THIS window open while using the Listing Tool."
echo
echo "  Now open the shared Listing Tool in your browser:"
echo "    https://209-38-94-2.sslip.io"
echo
echo "  Next time, use the 'Listing Tool Relay' shortcut on your Desktop."
echo

exec ./venv/bin/python relay.py
