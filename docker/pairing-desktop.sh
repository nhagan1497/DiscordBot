#!/bin/sh
set -eu

export DISPLAY="${PAIRING_DISPLAY:-:92}"
export XDG_CONFIG_HOME="$HOME/.config"
export XDG_CACHE_HOME="$HOME/.cache"
export XDG_DATA_HOME="$HOME/.local/share"
export XDG_RUNTIME_DIR="$HOME/.runtime"
export BROWSER=/usr/local/bin/pairing-browser.sh
mkdir -p "$XDG_CONFIG_HOME" "$XDG_DATA_HOME/applications" "$XDG_RUNTIME_DIR"
chmod 700 "$XDG_RUNTIME_DIR"

cat > "$XDG_DATA_HOME/applications/music-pairing-browser.desktop" <<'DESKTOP'
[Desktop Entry]
Type=Application
Name=Pairing Browser
Exec=/usr/local/bin/pairing-browser.sh %U
MimeType=x-scheme-handler/http;x-scheme-handler/https;
DESKTOP
xdg-mime default music-pairing-browser.desktop x-scheme-handler/http
xdg-mime default music-pairing-browser.desktop x-scheme-handler/https
xdg-mime default spotify.desktop x-scheme-handler/spotify

Xvfb "$DISPLAY" -screen 0 1280x800x24 -nolisten tcp &
attempt=0
until xdpyinfo >/dev/null 2>&1; do
  attempt=$((attempt + 1))
  [ "$attempt" -lt 50 ] || exit 1
  sleep .1
done

openbox &
x11vnc -display "$DISPLAY" -localhost -listen 127.0.0.1 -rfbport 5900 -nopw -forever -quiet &
dbus-run-session -- spotify --no-sandbox &
websockify --web=/usr/share/novnc 0.0.0.0:6080 127.0.0.1:5900
