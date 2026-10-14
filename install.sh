#!/usr/bin/env bash
# User-level install/update/uninstall. Never needs root.
#   ./install.sh               install or update
#   ./install.sh --uninstall   remove the app only
# Your data is never touched: Sober's config + backups live under
# ~/.var/app/org.vinegarhq.Sober, Nixspec's UI settings under ~/.config/nixspec.
set -euo pipefail

: "${HOME:?HOME is not set}"
here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
data="${XDG_DATA_HOME:-$HOME/.local/share}"
bin="$HOME/.local/bin"
app="$data/nixspec"
venv="$app/venv"
desktop="$data/applications/nixspec.desktop"
icon="$data/icons/hicolor/scalable/apps/nixspec.svg"
die() { echo "error: $*" >&2; exit 1; }

refresh_desktop_db() {
  command -v update-desktop-database >/dev/null 2>&1 && update-desktop-database "$data/applications" 2>/dev/null || true
}

if [[ "${1:-}" == "--uninstall" ]]; then
  rm -rf "$venv"
  rmdir "$app" 2>/dev/null || true
  rm -f "$bin/nixspec" "$desktop" "$icon"
  refresh_desktop_db
  echo "Removed Nixspec. Left untouched: ~/.config/nixspec and Sober's config/backups."
  exit 0
fi
[[ $# -eq 0 ]] || die "unknown option: $1 (use --uninstall or no arguments)"

for f in nixspec.py nixspec_logic.py nixspec_app.py pyproject.toml nixspec.svg nixspec.desktop; do
  [[ -f "$here/$f" ]] || die "missing $f next to install.sh (copy your existing nixspec.py here)"
done
command -v python3 >/dev/null || die "python3 is required (Arch: sudo pacman -S python)"
python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)' || die "Python 3.9 or newer is required"

python3 -m venv "$venv" || die "could not create a virtualenv (Debian/Ubuntu: install python3-venv)"
"$venv/bin/python" -m pip install --quiet "textual>=0.80" rich || die "pip could not install dependencies (network?)"
# --force-reinstall --no-deps: an update must replace the code even though the version may be unchanged
"$venv/bin/python" -m pip install --quiet --force-reinstall --no-deps "$here" || die "pip could not install Nixspec"
# verify the *installed* copy (run from / so the source directory is not on sys.path)
(cd / && "$venv/bin/python" -c "import textual, nixspec, nixspec_app") || die "installed package failed to import"
"$venv/bin/nixspec" --version >/dev/null || die "the nixspec command does not run"

mkdir -p "$bin" "$data/applications" "$data/icons/hicolor/scalable/apps"
ln -sf "$venv/bin/nixspec" "$bin/nixspec"
install -m 644 "$here/nixspec.svg" "$icon"
# absolute path: launchers often lack ~/.local/bin in PATH
EXEC="Exec=\"$venv/bin/nixspec\"" TRYEXEC="TryExec=$venv/bin/nixspec" \
  awk '/^Exec=/{print ENVIRON["EXEC"]; next} /^TryExec=/{print ENVIRON["TRYEXEC"]; next} {print}' \
  "$here/nixspec.desktop" > "$desktop"
chmod 644 "$desktop"
refresh_desktop_db

echo "Installed. Open 'Nixspec' from your application menu, or run: nixspec"
case ":$PATH:" in *":$bin:"*) ;; *) echo "Note: add $bin to PATH to run 'nixspec' from a shell.";; esac
