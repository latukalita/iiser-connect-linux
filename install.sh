#!/bin/bash
# Installs IISER Connect for the current user (no root needed).  Usage: ./install.sh [--enable]
set -e
HERE="$(cd "$(dirname "$0")" && pwd)"
APP="$HOME/.local/share/iiser-connect"
BIN="$HOME/.local/bin"
ICONS="$HOME/.local/share/icons/hicolor/scalable/apps"
APPS="$HOME/.local/share/applications"
UNITS="$HOME/.config/systemd/user"

command -v python3 >/dev/null || { echo "python3 is required"; exit 1; }
python3 - <<'PY' || GUI_MISSING=1
import gi
gi.require_version("Gtk", "4.0"); gi.require_version("Adw", "1")
PY
if [ -n "$GUI_MISSING" ]; then
  echo "NOTE: the window needs GTK4 + libadwaita. On Ubuntu/Debian:  sudo apt install python3-gi gir1.2-gtk-4.0 gir1.2-adw-1"
  echo "      (the command-line tool and background service work without them)"
fi

mkdir -p "$APP" "$BIN" "$ICONS" "$APPS" "$UNITS"
rm -rf "$APP/iiser_connect"
cp -r "$HERE/iiser_connect" "$APP/iiser_connect"
find "$APP" -name __pycache__ -type d -prune -exec rm -rf {} +
cp "$HERE/README.md" "$APP/README.md" 2>/dev/null || true

cat > "$BIN/iiser-connect" <<LAUNCH
#!/usr/bin/python3
import sys
sys.path.insert(0, "$APP")
from iiser_connect.cli import main
sys.exit(main())
LAUNCH
chmod +x "$BIN/iiser-connect"

cp "$HERE/data/app.iiserconnect.linux.svg" "$ICONS/"
sed "s#@BIN@#$BIN#" "$HERE/data/app.iiserconnect.linux.desktop" > "$APPS/app.iiserconnect.linux.desktop"
cp "$HERE/data/iiser-connect.service" "$UNITS/"
systemctl --user daemon-reload 2>/dev/null || true
command -v gtk-update-icon-cache >/dev/null && gtk-update-icon-cache -q -t "$HOME/.local/share/icons/hicolor" 2>/dev/null || true
command -v update-desktop-database >/dev/null && update-desktop-database -q "$APPS" 2>/dev/null || true

if systemctl --user is-active --quiet iiser-connect.service 2>/dev/null; then   # upgrading: pick up the new code
  systemctl --user restart iiser-connect.service && echo "Background service restarted with the new version."
fi

if [ "$1" = "--enable" ]; then
  systemctl --user enable --now iiser-connect.service
  echo "Background service enabled."
fi

echo
echo "Installed.  Open 'IISER Connect' from your app menu (or run: iiser-connect)."
case ":$PATH:" in *":$BIN:"*) ;; *) echo "Add $BIN to your PATH to use the 'iiser-connect' command.";; esac
if systemctl --user is-active --quiet iiser-login.timer 2>/dev/null; then
  echo
  echo "NOTE: an older 'iiser-login.timer' is still active and logs in every minute."
  echo "      IISER Connect replaces it:  systemctl --user disable --now iiser-login.timer"
fi
