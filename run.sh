#!/usr/bin/env bash
# Launch Music Organizer. First run sets everything up automatically
# (a private Python environment inside this folder - nothing system-wide).
#
#   ./run.sh                 open the app and pick a folder
#   ./run.sh ~/Music         open a folder directly
#   ./run.sh --undo ~/Music  undo renames
set -euo pipefail

HERE="$(cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")" && pwd)"
VENV="$HERE/.venv"
STAMP="$VENV/.installed"

say()  { printf '\033[1;35m♫\033[0m %s\n' "$*"; }
die()  { printf '\033[1;31m✗\033[0m %s\n' "$*" >&2; exit 1; }

# --- find a Python >= 3.10 ---------------------------------------------------
PY=""
for cand in python3 python3.14 python3.13 python3.12 python3.11 python3.10 python; do
    if command -v "$cand" >/dev/null 2>&1 &&
       "$cand" -c 'import sys; sys.exit(sys.version_info < (3, 10))' 2>/dev/null; then
        PY="$(command -v "$cand")"
        break
    fi
done
[ -n "$PY" ] || die "Python 3.10 or newer is needed (e.g. sudo apt install python3)."

# --- create the private environment on first run -----------------------------
if [ ! -x "$VENV/bin/python" ]; then
    say "First run: setting up (takes a minute, only once)…"
    if ! "$PY" -m venv "$VENV" 2>/dev/null; then
        rm -rf "$VENV"
        die "Python's venv module is missing. On Debian/Ubuntu: sudo apt install python3-venv"
    fi
fi

# --- (re)install when missing or when the project changed --------------------
if [ ! -f "$STAMP" ] || [ "$HERE/pyproject.toml" -nt "$STAMP" ]; then
    say "Installing dependencies…"
    LOG="$VENV/install.log"
    if ! { "$VENV/bin/python" -m pip install --disable-pip-version-check --upgrade pip &&
           "$VENV/bin/python" -m pip install --disable-pip-version-check -e "$HERE"; } >"$LOG" 2>&1; then
        tail -n 20 "$LOG" >&2
        die "Install failed (full log: $LOG). Check your internet connection and run ./run.sh again."
    fi
    touch "$STAMP"
    say "Ready."
fi

exec "$VENV/bin/python" -m music_organizer "$@"
