#!/bin/bash
cd "$(dirname "$0")"

if [ ! -f "venv/bin/python" ]; then
    echo "Setup hasn't been run yet on this Mac."
    echo "Double-click setup_relay.command first (one-time only)."
    read -r -p "Press Enter to close..."
    exit 1
fi

echo "Starting Listing Tool Relay..."
echo
echo "Keep this window open while using the shared Listing Tool webapp."
echo "Close it when you are done."
echo

exec ./venv/bin/python relay.py
