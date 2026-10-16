#!/bin/sh
set -eu
: "${REMOTE_PASSWORD:?REMOTE_PASSWORD environment variable is required}"
htpasswd -bc /etc/nginx/.htpasswd ferixdi "$REMOTE_PASSWORD" >/dev/null
chmod 600 /etc/nginx/.htpasswd
Xvfb :99 -screen 0 1440x900x24 -ac &
export DISPLAY=:99
sleep 1
fluxbox >/tmp/fluxbox.log 2>&1 &
x11vnc -display :99 -forever -shared -nopw -listen 127.0.0.1 -rfbport 5900 >/tmp/x11vnc.log 2>&1 &
python /app/remote_browser.py >/tmp/remote-browser.log 2>&1 &
websockify --web=/usr/share/novnc 127.0.0.1:6080 127.0.0.1:5900 >/tmp/websockify.log 2>&1 &
nginx -g 'daemon off;'
