#!/usr/bin/env bash
# Removes the service, the virtualenv and the launcher. Keeps config/state/label files
# unless --purge is given.
set -euo pipefail
APP="${XDG_DATA_HOME:-$HOME/.local/share}/streamlabels"
UNIT="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user/streamlabels.service"
systemctl --user disable --now streamlabels 2>/dev/null || true
rm -f "$UNIT" "$HOME/.local/bin/streamlabels"
systemctl --user daemon-reload 2>/dev/null || true
rm -rf "$APP"
if [ "${1:-}" = "--purge" ]; then
  rm -rf "${XDG_CONFIG_HOME:-$HOME/.config}/streamlabels" "${XDG_STATE_HOME:-$HOME/.local/state}/streamlabels"
  echo "removed config and state too (label .txt files were left alone)"
fi
echo "streamlabels uninstalled"
