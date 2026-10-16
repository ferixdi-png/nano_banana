#!/bin/sh
set -eu
: "${REMOTE_PASSWORD:?REMOTE_PASSWORD environment variable is required}"
htpasswd -bc /etc/nginx/.htpasswd ferixdi "$REMOTE_PASSWORD" >/dev/null
chmod 600 /etc/nginx/.htpasswd
pkill Xvfb >/dev/null 2>&1 || true
rm -f /tmp/.X99-lock /tmp/.X11-unix/X99
Xvfb :99 -screen 0 1440x900x24 -ac &
export DISPLAY=:99
sleep 1
fluxbox >/tmp/fluxbox.log 2>&1 &
x11vnc -display :99 -forever -shared -nopw -listen 127.0.0.1 -rfbport 5900 >/tmp/x11vnc.log 2>&1 &
python /app/remote_browser.py &
websockify 127.0.0.1:6080 127.0.0.1:5900 >/tmp/websockify.log 2>&1 &
nginx -g 'daemon off;'
