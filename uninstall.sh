#!/bin/bash
# Removes IISER Connect. Your saved login is kept unless you pass --purge.
systemctl --user disable --now iiser-connect.service 2>/dev/null || true
rm -f "$HOME/.config/systemd/user/iiser-connect.service" \
      "$HOME/.local/bin/iiser-connect" \
      "$HOME/.local/share/applications/app.iiserconnect.linux.desktop" \
      "$HOME/.local/share/icons/hicolor/scalable/apps/app.iiserconnect.linux.svg"
rm -rf "$HOME/.local/share/iiser-connect"
systemctl --user daemon-reload 2>/dev/null || true
if [ "$1" = "--purge" ]; then
  rm -rf "$HOME/.config/iiser-connect" "$HOME/.local/state/iiser-connect"
  echo "Removed everything, including your saved login."
else
  echo "Removed. Your saved login is still in ~/.config/iiser-connect (use --purge to delete it)."
fi
