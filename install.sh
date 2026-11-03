#!/usr/bin/env bash
# Installs streamlabels into a private virtualenv and registers a systemd --user service.
# Safe to re-run (upgrades in place). Needs python3 >= 3.11 with the venv module.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
APP="${XDG_DATA_HOME:-$HOME/.local/share}/streamlabels"
BIN="$HOME/.local/bin"
UNIT_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"

command -v python3 >/dev/null 2>&1 || { echo "python3 is required" >&2; exit 1; }
python3 -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else 1)' \
  || { echo "Python 3.11 or newer is required (found $(python3 --version))" >&2; exit 1; }

mkdir -p "$APP" "$BIN" "$UNIT_DIR"
if [ ! -x "$APP/venv/bin/python" ]; then
  echo "creating virtualenv in $APP/venv"
  python3 -m venv "$APP/venv"
fi
"$APP/venv/bin/python" -m pip install --quiet --upgrade pip
"$APP/venv/bin/python" -m pip install --quiet --upgrade "$HERE"
ln -sfn "$APP/venv/bin/streamlabels" "$BIN/streamlabels"

sed "s|@APP@|$APP|g" "$HERE/systemd/streamlabels.service" > "$UNIT_DIR/streamlabels.service"
systemctl --user daemon-reload 2>/dev/null || true

"$APP/venv/bin/streamlabels" init

cat <<MSG

Installed: $BIN/streamlabels  (add ~/.local/bin to PATH if it is not already)

Next steps:
  1. Paste your Streamlabs socket token into ~/.config/streamlabels/config.toml
       Streamlabs dashboard -> Settings -> API Settings -> API Tokens -> "Your Socket API Token"
  2. streamlabels test            # writes sample values so you can set up OBS text sources
  3. systemctl --user enable --now streamlabels
  4. journalctl --user -u streamlabels -f   # watch it connect and receive events
MSG
