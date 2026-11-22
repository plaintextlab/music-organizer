"""Full-resolution cover preview through ueberzug++ (``ueberzugpp``).

ueberzug++ draws a real image on top of the terminal window at a given cell
position. We run ``ueberzugpp layer`` in the background and send it JSON
commands on stdin. If it isn't installed, can't start, or dies, the app falls
back to its built-in character-based preview.

Environment overrides:
    MUSIC_ORGANIZER_PREVIEW=blocks     never use ueberzug++
    MUSIC_ORGANIZER_UEBERZUG_OUTPUT=x  force an output (x11, wayland, sixel, kitty…)
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import tempfile
import time
from pathlib import Path


class UeberzugPreview:
    IDENT = "music-organizer-cover"

    def __init__(self, proc: subprocess.Popen, tmpdir: Path):
        self.proc = proc
        self.tmpdir = tmpdir
        self._shown: tuple | None = None

    # ------------------------------------------------------------------ start
    @classmethod
    def start(cls) -> "UeberzugPreview | None":
        if os.environ.get("MUSIC_ORGANIZER_PREVIEW", "").lower() in ("blocks", "off", "none"):
            return None
        exe = shutil.which("ueberzugpp")
        if not exe:
            return None
        output = os.environ.get("MUSIC_ORGANIZER_UEBERZUG_OUTPUT", "")
        if not output:
            # Draw in a separate X11/Wayland surface. Terminal-graphics outputs
            # (sixel/kitty) would fight with the TUI redrawing the same screen.
            if os.environ.get("WAYLAND_DISPLAY"):
                output = "wayland"
            elif os.environ.get("DISPLAY"):
                output = "x11"
            else:
                return None
        tmpdir = Path(tempfile.mkdtemp(prefix="music-organizer-preview-"))
        attempts = [[exe, "layer", "--silent", "-o", output], [exe, "layer", "--silent"]]
        for cmd in attempts:
            try:
                proc = subprocess.Popen(
                    cmd, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL, text=True, bufsize=1,
                )
            except OSError:
                continue
            time.sleep(0.3)
            if proc.poll() is None:   # still running -> it accepted the options
                return cls(proc, tmpdir)
        shutil.rmtree(tmpdir, ignore_errors=True)
        return None

    # --------------------------------------------------------------- commands
    @property
    def alive(self) -> bool:
        return self.proc.poll() is None

    def _send(self, cmd: dict) -> bool:
        try:
            self.proc.stdin.write(json.dumps(cmd) + "\n")
            self.proc.stdin.flush()
            return True
        except (OSError, ValueError):
            return False

    def _file_for(self, data: bytes) -> Path:
        p = self.tmpdir / f"{hashlib.sha1(data).hexdigest()[:16]}.jpg"
        if not p.exists():
            p.write_bytes(data)
        return p

    def show(self, data: bytes, x: int, y: int, width: int, height: int) -> bool:
        """Place the image inside the given cell box (no-op if unchanged)."""
        path = self._file_for(data)
        state = (str(path), x, y, width, height)
        if state == self._shown:
            return True
        self._shown = state
        return self._send({
            "action": "add", "identifier": self.IDENT,
            "x": x, "y": y, "max_width": width, "max_height": height,
            "path": str(path),
        })

    def hide(self) -> None:
        if self._shown is not None:
            self._shown = None
            self._send({"action": "remove", "identifier": self.IDENT})

    def close(self) -> None:
        self.hide()
        try:
            self.proc.stdin.close()   # EOF: ueberzug++ removes the image and exits
        except OSError:
            pass
        try:
            self.proc.wait(timeout=1)
        except subprocess.TimeoutExpired:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=1)
            except subprocess.TimeoutExpired:
                self.proc.kill()
        shutil.rmtree(self.tmpdir, ignore_errors=True)
