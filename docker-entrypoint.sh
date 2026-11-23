#!/bin/sh
set -e

# Seed the (possibly empty, bind-mounted) templates/ and listings/ dirs on
# first run only — a mounted volume hides whatever the image had baked in at
# that path, so the starter agency templates are copied in from templates_seed/
# instead of living at the real path in the image.
mkdir -p /app/listings /app/templates
if [ -z "$(ls -A /app/templates 2>/dev/null)" ]; then
    cp -r /app/templates_seed/. /app/templates/
fi

Xvfb :99 -screen 0 1920x1080x24 -nolisten tcp &
XVFB_PID=$!
trap "kill $XVFB_PID" EXIT

# Wait briefly for the X socket so Chrome doesn't race Xvfb's startup.
for i in $(seq 1 20); do
    [ -e /tmp/.X11-unix/X99 ] && break
    sleep 0.25
done

exec gunicorn \
    --workers 1 \
    --threads 4 \
    --timeout 120 \
    --bind 0.0.0.0:5000 \
    app:app
