#!/usr/bin/env bash
# Launch Music Organizer. First run sets everything up automatically
# (a private Python environment inside this folder - nothing system-wide).
#
#   ./run.sh                 open the app and pick a folder
#   ./run.sh ~/Music         open a folder directly
#   ./run.sh --undo ~/Music  undo renames
#
# Each Python version / machine gets its own environment under .venvs/, so the
# same checkout works from the host, a distrobox/toolbox container, or after
# a Python upgrade without the environments breaking each other.
set -euo pipefail

HERE="$(cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")" && pwd)"

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

# e.g. "cpython-3.14-x86_64-tux-debian"
TAG="$("$PY" -c 'import sys, platform
print(f"{sys.implementation.name}-{sys.version_info[0]}.{sys.version_info[1]}-{platform.machine()}")')-$(hostname 2>/dev/null || echo local)"
VENV="$HERE/.venvs/$TAG"
STAMP="$VENV/.installed"
LOG="$VENV/install.log"
CHECK='import textual, textual_image, mutagen, PIL, httpx, unidecode, indic_transliteration, rapidfuzz, music_organizer'

healthy() { "$VENV/bin/python" -c "$CHECK" >/dev/null 2>&1; }

# --- rebuild an environment that no longer works ----------------------------
# (Python upgraded/removed underneath it, packages deleted, half-finished install…)
if [ -d "$VENV" ] && [ -f "$STAMP" ] && ! healthy; then
    say "The Python environment is broken - rebuilding it…"
    rm -rf "$VENV"
elif [ -d "$VENV" ] && ! "$VENV/bin/python" -c 'pass' >/dev/null 2>&1; then
    rm -rf "$VENV"
fi

# --- create it ---------------------------------------------------------------
if [ ! -x "$VENV/bin/python" ]; then
    say "Setting up for $("$PY" --version) (takes a minute, only once)…"
    mkdir -p "$HERE/.venvs"
    if ! "$PY" -m venv "$VENV" >/dev/null 2>&1; then
        rm -rf "$VENV"
        die "Python's venv module is missing. On Debian/Ubuntu: sudo apt install python3-venv"
    fi
fi

# --- install when new or when the project's dependencies changed -------------
if [ ! -f "$STAMP" ] || [ "$HERE/pyproject.toml" -nt "$STAMP" ]; then
    say "Installing dependencies…"
    if ! { "$VENV/bin/python" -m pip install --disable-pip-version-check --upgrade pip &&
           "$VENV/bin/python" -m pip install --disable-pip-version-check -e "$HERE"; } >"$LOG" 2>&1; then
        tail -n 20 "$LOG" >&2
        die "Install failed (full log: $LOG). Check your internet connection and run ./run.sh again."
    fi
    if ! healthy; then
        "$VENV/bin/python" -c "$CHECK" 2>&1 | tail -n 5 >&2
        die "Dependencies installed but can't be loaded (full log: $LOG)."
    fi
    touch "$STAMP"
    say "Ready."
fi

exec "$VENV/bin/python" -m music_organizer "$@"
