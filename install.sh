#!/usr/bin/env bash
# Install Shmoozy for the current user (no root needed).
#   - the app        -> ~/.local/bin/shmoozy
#   - a launcher      -> ~/.local/share/applications/shmoozy.desktop
set -euo pipefail

src="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
bindir="$HOME/.local/bin"
appdir="$HOME/.local/share/applications"

# Runtime deps: GTK4 + libadwaita + PyGObject, and the claude CLI.
missing=()
python3 -c 'import gi; gi.require_version("Gtk","4.0"); gi.require_version("Adw","1"); from gi.repository import Gtk, Adw' 2>/dev/null \
  || missing+=("python-gobject gtk4 libadwaita")
command -v claude >/dev/null 2>&1 || missing+=("claude (Claude Code CLI)")
if ((${#missing[@]})); then
  echo "Missing dependencies: ${missing[*]}"
  echo "Install the Arch packages with:  sudo pacman -S --needed python-gobject gtk4 libadwaita"
  echo "And Claude Code from: https://claude.com/claude-code"
  exit 1
fi

mkdir -p "$bindir" "$appdir"
install -m 755 "$src/shmoozy.py" "$bindir/shmoozy"
install -m 644 "$src/shmoozy.desktop" "$appdir/shmoozy.desktop"
command -v update-desktop-database >/dev/null 2>&1 \
  && update-desktop-database "$appdir" 2>/dev/null || true

echo "Installed. Launch 'Shmoozy' from your app launcher, or run: shmoozy"
case ":$PATH:" in
  *":$bindir:"*) ;;
  *) echo "Note: $bindir is not on your PATH." ;;
esac
