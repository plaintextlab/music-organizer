"""Textual TUI: scan -> fetch drafts -> review / edit -> approve -> write."""

from __future__ import annotations

import io
import os
import shutil
import subprocess
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from PIL import Image
from rich.color import Color
from rich.style import Style
from rich.table import Table
from rich.text import Text
from textual import on, work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Grid, Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import (Button, Checkbox, DataTable, DirectoryTree, Footer, Header,
                             Input, Label, ProgressBar, RichLog, Static)

from . import organizer
from .drafts import DraftStore, last_folder, remember_folder
from .matcher import blank_meta, final_meta, lookup, search
from .models import Status, Track
from . import graphics   # must load before the app starts (queries the terminal)
from .preview import UeberzugPreview
from .romanize import is_latin, to_english
from .scanner import filename_parts, read_track, scan

KEY_HELP = Text.assemble(
    ("\n── keys ─────────────────────────────\n", "dim"),
    ("space", "bold blue"), (" approve   ", ""), ("a", "bold blue"), (" approve all ◆\n", ""),
    ("e", "bold blue"), (" edit   ", ""), ("/", "bold blue"), (" search & pick\n", ""),
    ("c", "bold blue"), (" other results\n", ""),
    ("i", "bold blue"), (" image file   ", ""), ("g", "bold blue"), (" generate art\n", ""),
    ("d", "bold blue"), (" web art   ", ""), ("k", "bold blue"), (" keep old art   ", ""),
    ("v", "bold blue"), (" view\n", ""),
    ("w", "bold blue"), (" write approved   ", ""), ("u", "bold blue"), (" undo renames\n", ""),
    ("r", "bold blue"), (" rescan   ", ""), ("o", "bold blue"), (" open folder   ", ""),
    ("q", "bold blue"), (" quit", ""),
)

IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".bmp", ".gif"}

STATUS_STYLE = {
    Status.PENDING: ("·  scanned", "dim"),
    Status.SEARCHING: ("⟳ searching", "bold cyan"),
    Status.MATCHED: ("◆ draft", "bold green"),
    Status.REVIEW: ("◇ check", "bold yellow"),
    Status.NOT_FOUND: ("✗ no match", "bold red"),
    Status.DONE: ("✓ written", "bold blue"),
    Status.ERROR: ("! error", "bold red"),
}
ART_STYLE = {"online": ("web", "cyan"), "existing": ("kept", "default"),
             "generated": ("gen", "blue"), "file": ("file", "yellow"), "": ("—", "dim")}

FIELDS = [("title", "Title"), ("artist", "Artist(s)"), ("album", "Album / Movie"),
          ("albumartist", "Album artist"), ("year", "Year"), ("genre", "Genre"),
          ("composer", "Composer"), ("label", "Label")]


def shown(name: str) -> str:
    """File name safe to draw in a terminal table.

    Terminals disagree on how wide Bengali/Devanagari text is, which shoves
    columns and borders out of line, so such names are shown transliterated
    (marked with ≈). The real file name is never changed by this.
    """
    if is_latin(name):
        return name
    p = Path(name)
    return f"≈ {to_english(p.stem)}{p.suffix}"


def halfblock(data: bytes, width: int = 40) -> Text:
    """Render an image with ▀ characters: 2 pixels per terminal cell."""
    im = Image.open(io.BytesIO(data)).convert("RGB").resize((width, width), Image.LANCZOS)
    px = im.load()
    out = Text()
    for y in range(0, width, 2):
        for x in range(width):
            top, bot = px[x, y], px[x, y + 1]
            out.append("▀", Style(color=Color.from_rgb(*top), bgcolor=Color.from_rgb(*bot)))
        if y < width - 2:
            out.append("\n")
    return out


# =========================================================================== #
# modal screens
# =========================================================================== #

class _Modal(ModalScreen):
    BINDINGS = [Binding("escape", "cancel", "Cancel")]

    def action_cancel(self) -> None:
        self.dismiss(None)


class _DirsOnly(DirectoryTree):
    def filter_paths(self, paths):
        return [p for p in paths if p.is_dir() and not p.name.startswith(".")]


class _DirsAndImages(DirectoryTree):
    def filter_paths(self, paths):
        return [p for p in paths if not p.name.startswith(".")
                and (p.is_dir() or p.suffix.lower() in IMAGE_EXTS)]


class FolderPicker(_Modal):
    def __init__(self, start: Path):
        super().__init__()
        self.start = start

    def compose(self) -> ComposeResult:
        with Vertical(classes="dialog wide"):
            yield Label("Choose your music folder", classes="dialog-title")
            yield Input(str(self.start), id="path", placeholder="/path/to/music")
            yield _DirsOnly(str(self.start if self.start.is_dir() else Path.home()), id="tree")
            with Horizontal(classes="buttons"):
                yield Button("Open folder", variant="primary", id="ok")
                yield Button("Cancel", id="cancel")

    @on(DirectoryTree.DirectorySelected)
    def _picked(self, ev: DirectoryTree.DirectorySelected) -> None:
        self.query_one("#path", Input).value = str(ev.path)

    @on(Input.Submitted)
    @on(Button.Pressed, "#ok")
    def _ok(self) -> None:
        p = Path(self.query_one("#path", Input).value).expanduser()
        if p.is_dir():
            self.dismiss(p)
        else:
            self.notify("That folder doesn't exist", severity="error")

    @on(Button.Pressed, "#cancel")
    def _cancel(self) -> None:
        self.dismiss(None)


class ImagePicker(_Modal):
    def __init__(self, start: Path):
        super().__init__()
        self.start = start

    def compose(self) -> ComposeResult:
        with Vertical(classes="dialog wide"):
            yield Label("Pick an image for the cover (jpg / png / webp)", classes="dialog-title")
            yield Input(placeholder="type a path, or pick below", id="path")
            yield _DirsAndImages(str(self.start), id="tree")
            with Horizontal(classes="buttons"):
                yield Button("Use image", variant="primary", id="ok")
                yield Button("Cancel", id="cancel")

    @on(DirectoryTree.FileSelected)
    def _picked(self, ev: DirectoryTree.FileSelected) -> None:
        self.dismiss(Path(ev.path))

    @on(Input.Submitted)
    @on(Button.Pressed, "#ok")
    def _ok(self) -> None:
        p = Path(self.query_one("#path", Input).value).expanduser()
        if p.is_file():
            self.dismiss(p)
        else:
            self.notify("Pick an image file", severity="error")

    @on(Button.Pressed, "#cancel")
    def _cancel(self) -> None:
        self.dismiss(None)


class EditScreen(_Modal):
    """Hand-edit every field of the draft."""

    def __init__(self, track: Track):
        super().__init__()
        self.track = track
        self.meta = dict(track.meta or blank_meta(track))

    def compose(self) -> ComposeResult:
        with Vertical(classes="dialog wide"):
            yield Label(f"Edit draft · {shown(self.track.path.name)}", classes="dialog-title")
            with Grid(id="form"):
                for key, label in FIELDS:
                    yield Label(label, classes="form-label")
                    yield Input(str(self.meta.get(key, "") or ""), id=f"f-{key}",
                                placeholder="separate several with commas" if key == "artist" else "")
            yield Static(id="preview", classes="preview")
            yield Static("Everything is converted to English letters when saved. "
                         "Nothing is written to the song until you approve + write.",
                         classes="hint")
            with Horizontal(classes="buttons"):
                yield Button("Save draft", variant="primary", id="ok")
                yield Button("Cancel", id="cancel")

    def on_mount(self) -> None:
        self._update_preview()
        self.query_one("#f-title", Input).focus()

    def _values(self) -> dict:
        m = {k: to_english(self.query_one(f"#f-{k}", Input).value) for k, _ in FIELDS}
        m["artists"] = organizer.split_artists(m["artist"])
        m["artist"] = ", ".join(m["artists"])
        return m

    @on(Input.Changed)
    def _update_preview(self) -> None:
        m = self._values()
        name = organizer.new_filename(m, self.track.ext) if m["title"] else "(needs a title)"
        self.query_one("#preview", Static).update(Text.assemble(("New file name  ", "dim"),
                                                                (name, "bold yellow")))

    @on(Input.Submitted)
    @on(Button.Pressed, "#ok")
    def _ok(self) -> None:
        m = self._values()
        if not m["title"]:
            self.notify("A title is required", severity="error")
            return
        if m["year"] and not (m["year"].isdigit() and len(m["year"]) == 4):
            self.notify("Year must be 4 digits, e.g. 2010", severity="error")
            return
        self.dismiss(m)

    @on(Button.Pressed, "#cancel")
    def _cancel(self) -> None:
        self.dismiss(None)


def _mmss(sec: float | None) -> str:
    return f"{int(sec) // 60}:{int(sec) % 60:02d}" if sec else "-:--"


class SearchPickScreen(_Modal):
    """Type any search, see every result, pick the right one yourself."""

    BINDINGS = [Binding("escape", "cancel", "Cancel"),
                Binding("down", "to_results", show=False, priority=False)]

    def __init__(self, track: Track, start_in_results: bool = False):
        super().__init__()
        self.track = track
        self.results = list(track.candidates)
        self.used = list(track.queries)
        self.start_in_results = start_in_results and bool(self.results)
        if track.custom_query:
            self.query_text = track.custom_query
        elif track.queries:
            self.query_text = track.queries[0]
        elif track.meta:
            self.query_text = f"{track.meta['title']} {track.meta['artist']}".strip()
        else:
            self.query_text = filename_parts(track.path)[0].replace(" - ", " ")

    def compose(self) -> ComposeResult:
        with Vertical(classes="dialog wide", id="search-dialog"):
            yield Label(f"Search & pick · {shown(self.track.path.name)}", classes="dialog-title")
            yield Static(id="used", classes="hint-top")
            yield Input(self.query_text, id="q", placeholder="song name, artist, movie…")
            yield Static(id="status", classes="hint")
            yield DataTable(id="results", cursor_type="row", zebra_stripes=True)
            yield Static("Enter in the box searches · ↓ / Tab moves to the results · "
                         "Enter on a result uses it · Esc cancels", classes="hint")

    def on_mount(self) -> None:
        self._show_used()
        self.call_after_refresh(self._fill, self.results)
        if self.start_in_results:
            self.query_one("#results", DataTable).focus()
        else:
            self.query_one("#q", Input).focus()

    def _show_used(self) -> None:
        if self.used:
            qs = ", ".join(f"“{q}”" for q in self.used)
            txt = Text.assemble(("Last search used: ", "dim"), (qs, "bold"))
        else:
            txt = Text("Not searched yet — press Enter to search.", "dim")
        self.query_one("#used", Static).update(txt)

    def _fill(self, results) -> None:
        tbl = self.query_one("#results", DataTable)
        tbl.clear(columns=True)
        # one line per result; long names are cut to fit the dialog
        w = max(60, self.query_one("#search-dialog").content_region.width or 110)
        # marker, %, source, year, length + 1 cell padding each side of 8 columns + scrollbar
        fixed = 2 + 5 + 7 + 5 + 6 + 8 * 2 + 3
        flex = max(30, w - fixed)
        cols = [("", 2), ("Match", 5), ("Source", 7), ("Title", flex * 30 // 100),
                ("Artist", flex * 30 // 100), ("Album / Movie", flex - 2 * (flex * 30 // 100)),
                ("Year", 5), ("Length", 6)]
        for label, width in cols:
            tbl.add_column(label, width=width)
        chosen, file_len = self.track.chosen, self.track.duration
        src = {"jiosaavn": "saavn", "itunes": "itunes", "musicbrainz": "mbrainz"}
        for i, c in enumerate(results):
            m = final_meta(c)
            sc = min(100, c.score)
            same_len = bool(c.duration and file_len and abs(c.duration - file_len) <= 3)
            tbl.add_row(
                Text("●", "green") if c is chosen else "",
                Text(f"{sc:3.0f}%", "bold green" if sc >= 75 else "yellow" if sc >= 45 else "red"),
                Text(src.get(c.source, c.source), "dim"),
                Text(m["title"] or "?", "bold"),
                Text(m["artist"] or "?", "cyan"),
                m["album"] or "—",
                Text(m["year"] or "----", "yellow"),
                Text(_mmss(c.duration), "green" if same_len else "dim"),
                key=str(i),
            )
        status = self.query_one("#status", Static)
        if results:
            note = f"  ·  this song is {_mmss(file_len)}; matching lengths are green" if file_len else ""
            status.update(Text(f"{len(results)} results{note}", "dim"))
        else:
            status.update(Text("No results yet." if not self.used else
                               "Nothing found — try fewer words, or the movie/album name.", "dim"))

    def action_to_results(self) -> None:
        if self.query_one("#q", Input).has_focus and self.results:
            self.query_one("#results", DataTable).focus()

    @on(Input.Submitted, "#q")
    def _search(self) -> None:
        q = self.query_one("#q", Input).value.strip()
        if not q:
            return
        self.query_one("#status", Static).update(Text(f"Searching for “{q}” …", "bold"))
        self.query_one("#results", DataTable).clear()
        self._run_search(q)

    @work(thread=True, exclusive=True)
    def _run_search(self, q: str) -> None:
        try:
            results, used, _ = search(self.track, q)
        except Exception as e:
            self.app.call_from_thread(self.query_one("#status", Static).update,
                                      Text(f"Search failed: {e}", "red"))
            return
        self.app.call_from_thread(self._got, q, results, used)

    def _got(self, q: str, results, used) -> None:
        self.results, self.used, self.query_text = results, used, q
        self._show_used()
        self._fill(results)
        if results:
            self.query_one("#results", DataTable).focus()

    @on(DataTable.RowSelected, "#results")
    def _picked(self, ev: DataTable.RowSelected) -> None:
        c = self.results[int(ev.row_key.value)]
        self.dismiss({"candidate": c, "results": self.results, "used": self.used,
                      "query": self.query_text})


class ConfirmScreen(_Modal):
    def __init__(self, title: str, body: str, ok: str, rename_option: bool = False):
        super().__init__()
        self.title_, self.body, self.ok, self.rename_option = title, body, ok, rename_option

    def compose(self) -> ComposeResult:
        with Vertical(classes="dialog"):
            yield Label(self.title_, classes="dialog-title")
            yield Static(self.body, classes="body")
            if self.rename_option:
                yield Checkbox("Rename files to  Song (Year) - Artist", True, id="rename")
            with Horizontal(classes="buttons"):
                yield Button(self.ok, variant="error", id="ok")
                yield Button("Cancel", variant="primary", id="cancel")

    def on_mount(self) -> None:
        self.query_one("#cancel", Button).focus()

    @on(Button.Pressed, "#ok")
    def _ok(self) -> None:
        rename = self.query_one("#rename", Checkbox).value if self.rename_option else True
        self.dismiss({"rename": rename})

    @on(Button.Pressed, "#cancel")
    def _cancel(self) -> None:
        self.dismiss(None)


# =========================================================================== #
# main app
# =========================================================================== #

class MusicOrganizer(App):
    TITLE = "♫ MUSIC ORGANIZER"
    CSS_PATH = "app.tcss"

    BINDINGS = [
        Binding("o", "open_folder", "Open"),
        Binding("f", "fetch", "Fetch"),
        Binding("space", "approve", "Approve"),
        Binding("a", "approve_matched", "All ◆"),
        Binding("e", "edit", "Edit"),
        Binding("slash", "search", "Search & pick"),
        Binding("c", "candidates", "Other results", show=False),
        Binding("i", "cover_file", "Img file"),
        Binding("g", "cover_generate", "Gen art"),
        Binding("d", "cover_download", "Web art", show=False),
        Binding("k", "cover_keep", "Art: keep old", show=False),
        Binding("v", "cover_view", "View art", show=False),
        Binding("w", "write", "WRITE"),
        Binding("u", "undo", "Undo renames", show=False),
        Binding("r", "rescan", "Rescan", show=False),
        Binding("q", "quit", "Quit"),
    ]

    def __init__(self, folder: Path | None = None):
        super().__init__()
        self.folder = folder
        self.tracks: list[Track] = []
        self.store: DraftStore | None = None
        self.busy = False
        self.preview: UeberzugPreview | None = None
        self.cover_mode = ""

    # ------------------------------------------------------------- layout
    def compose(self) -> ComposeResult:
        yield Header(show_clock=False)
        with Horizontal(id="topbar"):
            yield Label(" FOLDER ", id="folder-tag")
            yield Static("press  o  to choose a music folder", id="folder")
            yield Static("", id="counts")
        with Horizontal(id="main"):
            yield DataTable(id="table", cursor_type="row", zebra_stripes=True)
            with Vertical(id="side"):
                # the cover sits outside the scroll area so ueberzug++ can
                # place the real image at a fixed spot
                yield Static(id="cover")
                if graphics.ImageWidget is not None:
                    yield graphics.ImageWidget(id="cover-img")
                yield Static(id="cover-info")
                with VerticalScroll(id="side-scroll"):
                    yield Static(id="details")
                    yield Static(KEY_HELP, id="keys")
        with Horizontal(id="progress-row"):
            yield ProgressBar(id="progress", show_eta=False)
            yield Static("", id="progress-label")
        yield RichLog(id="log", markup=True, wrap=True, max_lines=500)
        yield Footer(compact=True, show_command_palette=False)

    def on_mount(self) -> None:
        # Use the terminal's own palette and background. Light terminals:
        # MUSIC_ORGANIZER_THEME=ansi-light (any Textual theme name works).
        self.theme = os.environ.get("MUSIC_ORGANIZER_THEME", "ansi-dark")
        # Sharpest available cover preview: terminal graphics (kitty/sixel),
        # then ueberzug++, then coloured block characters.
        if graphics.ImageWidget is not None:
            self.cover_mode = graphics.MODE
        else:
            self.preview = UeberzugPreview.start()
            self.cover_mode = "ueberzug++" if self.preview else ""
        if self.preview:
            self.set_interval(0.25, self._sync_preview)
        t = self.query_one(DataTable)
        self._add_columns()
        self.query_one("#progress-row").display = False
        self.log_line("[b blue]Welcome![/] Workflow: [b]o[/] open folder → [b]f[/] fetch drafts → "
                      "review / [b]e[/] edit / cover keys → [b]space[/] approve → [b]w[/] write.")
        self.log_line("[dim]Drafts are kept in ~/.cache/music-organizer — your songs are not touched "
                      "until you press w and confirm.[/]")
        if self.cover_mode:
            self.log_line(f"[dim]Cover preview: {self.cover_mode}.[/]")
        else:
            self.log_line("[dim]Cover preview: block characters (this terminal has no image "
                          "support; use kitty/foot/wezterm or install ueberzugpp for sharp covers).[/]")
        if not self.folder and (prev := last_folder()):
            self.folder = prev
            self.log_line(f"Reopening your last folder — press [b]o[/] to choose another.")
        if self.folder:
            self.load_folder(self.folder)
        else:
            self.action_open_folder()
        t.focus()

    # ------------------------------------------------------------ helpers
    def log_line(self, msg: str) -> None:
        self.query_one("#log", RichLog).write(msg)

    def current(self) -> Track | None:
        t = self.query_one(DataTable)
        if not self.tracks or t.cursor_row is None or t.cursor_row >= len(self.tracks):
            return None
        return self.tracks[t.cursor_row]

    def _row(self, tr: Track) -> list:
        label, style = STATUS_STYLE[tr.status]
        ok = Text("●", "bold green") if tr.approved else Text("○", "dim")
        new = organizer.target_path(tr)
        if new is None:
            new_txt = Text("—", "dim")
        elif new.name == tr.path.name:
            new_txt = Text(new.name + "  (same)", "dim")
        else:
            new_txt = Text(new.name, "yellow" if tr.edited else "")
        art, art_style = ART_STYLE.get(tr.cover_source, ART_STYLE[""])
        return [ok, Text(label, style), Text(shown(tr.path.name), "default"), new_txt, Text(art, art_style)]

    # ------------------------------------------------------- responsive
    def _cover_px(self) -> int:
        w, h = self.size
        px = 40 if h >= 48 else 32 if h >= 40 else 24
        return min(px, 30) if w < 150 else px

    def on_resize(self, _ev=None) -> None:
        w, h = self.size
        self.screen.set_class(w < 150, "-narrow")
        self.screen.set_class(h < 40, "-short")
        self.call_after_refresh(self._fit_columns)
        self.call_after_refresh(lambda: self.show_details(self.current()))

    def _fit_columns(self) -> None:
        """Share the table's width between the two file-name columns.

        Columns are rebuilt (not resized in place) so no row keeps a stale
        layout from the old width.
        """
        t = self.query_one(DataTable)
        avail = t.scrollable_content_region.width - (2 + 12 + 5) - 2 * 5 - 1
        avail = max(avail, 30)
        fw = avail * 2 // 5
        widths = (fw, avail - fw)
        if widths == getattr(self, "_col_widths", None):
            return
        self._col_widths = widths
        row = t.cursor_row
        t.clear(columns=True)
        self._add_columns(*widths)
        for i, tr in enumerate(self.tracks):
            t.add_row(*self._row(tr), key=str(i))
        if self.tracks:
            t.move_cursor(row=min(row, len(self.tracks) - 1))

    def _add_columns(self, file_w: int = 30, new_w: int = 40) -> None:
        t = self.query_one(DataTable)
        t.add_column("✓", key="ok", width=2)
        t.add_column("State", key="state", width=12)
        t.add_column("Current file", key="file", width=file_w)
        t.add_column("→ Draft file name", key="new", width=new_w)
        t.add_column("Art", key="art", width=5)

    def refresh_track(self, tr: Track) -> None:
        try:
            i = self.tracks.index(tr)
        except ValueError:
            return
        t = self.query_one(DataTable)
        for col, val in zip(("ok", "state", "file", "new", "art"), self._row(tr)):
            t.update_cell(str(i), col, val)
        if tr is self.current():
            self.show_details(tr)
        self.update_counts()

    def update_counts(self) -> None:
        n = len(self.tracks)
        by = lambda s: sum(1 for t in self.tracks if t.status == s)
        appr = sum(1 for t in self.tracks if t.approved)
        self.query_one("#counts", Static).update(Text.assemble(
            (f" {n} songs ", "bold"), (f" ◆ {by(Status.MATCHED)} ", "green"),
            (f" ◇ {by(Status.REVIEW)} ", "yellow"), (f" ✗ {by(Status.NOT_FOUND)} ", "red"),
            (f" ● {appr} approved ", "bold green"), (f" ✓ {by(Status.DONE)} written ", "blue"),
        ))

    def show_details(self, tr: Track | None) -> None:
        cover = self.query_one("#cover", Static)
        info = self.query_one("#cover-info", Static)
        details = self.query_one("#details", Static)
        img = self.query("#cover-img").first() if graphics.ImageWidget is not None else None
        if img is not None:
            show_img = bool(tr and tr.cover)
            img.display = show_img
            cover.display = not show_img
        if tr is None:
            cover.update("")
            info.update("")
            details.update("")
            return
        if img is not None and tr.cover:
            px = self._cover_px()
            try:
                img.image = Image.open(io.BytesIO(tr.cover))
                img.styles.width = px
                # height follows the image's real proportions and the
                # terminal's real cell size, so square covers stay square
                img.styles.height = "auto"
            except Exception:
                img.display, cover.display = False, True
                cover.update(Text("(cover can't be previewed)", "dim"))
        elif tr.cover and self.preview:
            px = self._cover_px()   # reserve a square box; ueberzug++ draws into it
            cover.update(Text("\n".join([" " * px] * (px // 2))))
        elif tr.cover:
            try:
                cover.update(halfblock(tr.cover, self._cover_px()))
            except Exception:
                cover.update(Text("(cover can't be previewed)", "dim"))
        else:
            cover.update(Text("\n\n      no cover in draft\n\n   g generate · i from file\n", "dim"))
        art = {"online": "downloaded", "existing": "already in the file", "generated": "generated",
               "file": "from your image file", "": "none"}[tr.cover_source]
        info.update(Text.assemble(("Cover: ", "dim"), (art, "bold"),
                                  ("   v view", "dim") if tr.cover else ""))
        self.call_after_refresh(self._sync_preview)

        tbl = Table.grid(padding=(0, 1))
        tbl.add_column(style="dim", no_wrap=True)
        tbl.add_column(overflow="fold")
        cur = {"title": tr.cur_title, "artist": tr.cur_artist, "album": tr.cur_album, "year": tr.cur_year}
        m = tr.meta or {}
        for key, label in FIELDS:
            new = str(m.get(key, "") or "")
            old = cur.get(key, "")
            if not m:
                tbl.add_row(label, Text(old or "—", "default"))
            elif old and old != new and key in cur:
                tbl.add_row(label, Text.assemble((new or "—", "bold green"), ("\n  was: ", "dim"),
                                                 (old, "dim strike")))
            else:
                tbl.add_row(label, Text(new or "—", "bold" if new else "dim"))
        label, style = STATUS_STYLE[tr.status]
        head = Text.assemble((label, style), ("   ", ""),
                             ("APPROVED" if tr.approved else "not approved",
                              "bold green" if tr.approved else "dim"),
                             ("  · edited by you" if tr.edited else "", "yellow"))
        new = organizer.target_path(tr)
        foot = Text.assemble(("\nfile  ", "dim"), (shown(tr.path.name), "default"),
                             ("\nnew   ", "dim"), (new.name if new else "—", "bold yellow"),
                             ("\n\n" + tr.message, "dim italic") if tr.message else "")
        from rich.console import Group
        details.update(Group(head, Text(""), tbl, foot))

    @on(DataTable.RowHighlighted)
    def _highlight(self, _ev) -> None:
        self.show_details(self.current())

    def save_drafts_soon(self) -> None:
        """Save during long jobs, at most every 2 seconds."""
        import time
        now = time.monotonic()
        if now - getattr(self, "_last_save", 0.0) >= 2.0:
            self._last_save = now
            self.save_drafts()

    def save_drafts(self) -> None:
        if self.store:
            try:
                self.store.save(self.tracks)
            except OSError as e:
                self.log_line(f"[red]could not save drafts: {e}[/]")

    def _guard(self) -> bool:
        if self.busy:
            self.notify("Busy — wait for the current job to finish", severity="warning")
            return False
        return True

    # -------------------------------------------------------- folder/scan
    def action_open_folder(self) -> None:
        if not self._guard():
            return
        start = self.folder or Path.home() / "Music"
        if not start.is_dir():
            start = Path.home()
        self.push_screen(FolderPicker(start), lambda p: p and self.load_folder(p))

    def action_rescan(self) -> None:
        if self.folder and self._guard():
            self.save_drafts()
            self.load_folder(self.folder)

    def load_folder(self, folder: Path) -> None:
        self.folder = folder.expanduser().resolve()
        self.store = DraftStore(self.folder)
        remember_folder(self.folder)
        self.query_one("#folder", Static).update(Text(str(self.folder), "bold"))
        self.scan_worker(self.folder)

    @work(thread=True, exclusive=True, group="scan")
    def scan_worker(self, folder: Path) -> None:
        self.busy = True
        try:
            self.call_from_thread(self.log_line, f"Scanning [b]{folder}[/] …")
            files = scan(folder)
            tracks = [read_track(p) for p in files]
            restored = self.store.load(tracks) if self.store else 0
            self.call_from_thread(self._populate, tracks, restored)
        finally:
            self.busy = False

    def _populate(self, tracks: list[Track], restored: int) -> None:
        self.tracks = tracks
        t = self.query_one(DataTable)
        t.clear()
        for i, tr in enumerate(tracks):
            t.add_row(*self._row(tr), key=str(i))
        self.update_counts()
        self.show_details(self.current())
        msg = f"Found [b]{len(tracks)}[/] songs."
        if restored:
            msg += f" Restored [b]{restored}[/] saved drafts."
        if tracks:
            msg += " Press [b]f[/] to fetch drafts." if restored < len(tracks) else ""
        else:
            msg += " (mp3, m4a, flac, ogg, opus are supported)"
        self.log_line(msg)
        t.focus()

    # -------------------------------------------------------------- fetch
    def action_fetch(self) -> None:
        if not self._guard():
            return
        todo = [t for t in self.tracks if t.status in (Status.PENDING, Status.NOT_FOUND, Status.ERROR)]
        if not todo:
            tr = self.current()
            if tr and tr.status != Status.DONE:
                todo = [tr]
                self.log_line(f"Re-fetching [b]{shown(tr.path.name)}[/]")
        if not todo:
            self.notify("Nothing to fetch")
            return
        self.fetch_worker(todo)

    @work(thread=True, exclusive=True, group="fetch")
    def fetch_worker(self, todo: list[Track]) -> None:
        self.busy = True
        self.call_from_thread(self._progress_start, len(todo), "Fetching drafts")

        def one(tr: Track) -> None:
            tr.status = Status.SEARCHING
            self.call_from_thread(self.refresh_track, tr)
            try:
                lookup(tr, log=lambda m: self.call_from_thread(self.log_line, f"[dim]{m}[/]"))
                if tr.meta:
                    organizer.prepare_cover(tr)
            except Exception as e:
                tr.status, tr.message = Status.ERROR, f"lookup failed: {e}"
            self.call_from_thread(self.refresh_track, tr)
            self.call_from_thread(self._progress_step)
            self.save_drafts_soon()   # don't lose finished drafts if the app closes mid-fetch

        try:
            with ThreadPoolExecutor(max_workers=4) as ex:
                list(ex.map(one, todo))
        finally:
            self.busy = False
            self.save_drafts()
            self.call_from_thread(self._progress_done)
            m = sum(1 for t in todo if t.status == Status.MATCHED)
            r = sum(1 for t in todo if t.status == Status.REVIEW)
            n = sum(1 for t in todo if t.status == Status.NOT_FOUND)
            self.call_from_thread(
                self.log_line,
                f"Drafts ready: [green]{m} confident[/], [yellow]{r} to check[/], "
                f"[red]{n} not found[/]. Nothing has been written yet — review, approve "
                f"([b]space[/] / [b]a[/]) and press [b]w[/].")

    def _progress_start(self, total: int, label: str) -> None:
        self.query_one("#progress-row").display = True
        self.query_one("#progress", ProgressBar).update(total=total, progress=0)
        self.query_one("#progress-label", Static).update(f" {label}")

    def _progress_step(self) -> None:
        self.query_one("#progress", ProgressBar).advance(1)

    def _progress_done(self) -> None:
        self.query_one("#progress-row").display = False

    # ------------------------------------------------------------ approve
    def action_approve(self) -> None:
        tr = self.current()
        if not tr:
            return
        if not tr.meta:
            self.notify("No draft yet — fetch (f), search (/) or edit (e) first", severity="warning")
            return
        if tr.status == Status.DONE and not tr.approved:
            tr.status = Status.REVIEW  # editing again after a write
        tr.approved = not tr.approved
        self.refresh_track(tr)
        self.save_drafts()
        t = self.query_one(DataTable)
        if t.cursor_row < len(self.tracks) - 1:
            t.move_cursor(row=t.cursor_row + 1)

    def action_approve_matched(self) -> None:
        n = 0
        for tr in self.tracks:
            if tr.status == Status.MATCHED and tr.meta and not tr.approved:
                tr.approved = True
                n += 1
                self.refresh_track(tr)
        self.save_drafts()
        self.log_line(f"Approved {n} confident drafts (◆). Check the ◇ ones yourself.")

    # --------------------------------------------------------------- edit
    def action_edit(self) -> None:
        tr = self.current()
        if not tr or not self._guard():
            return

        def done(m: dict | None) -> None:
            if not m:
                return
            regen = tr.cover_source == "generated" and tr.meta and any(
                (tr.meta.get(k) or "") != m.get(k) for k in ("title", "artist", "year"))
            tr.meta = m
            tr.edited = True
            if tr.status in (Status.NOT_FOUND, Status.PENDING, Status.ERROR, Status.DONE):
                tr.status = Status.REVIEW
            if not tr.message or tr.status == Status.REVIEW:
                tr.message = "edited by hand"
            if regen or not tr.cover:
                organizer.generate_cover(tr)
            self.refresh_track(tr)
            self.save_drafts()
            self.log_line(f"Draft updated: [b]{organizer.new_filename(m, tr.ext)}[/] "
                          f"— press space to approve")

        self.push_screen(EditScreen(tr), done)

    def action_search(self, start_in_results: bool = False) -> None:
        tr = self.current()
        if not tr or not self._guard():
            return

        def done(res: dict | None) -> None:
            if not res:
                return
            c = res["candidate"]
            tr.custom_query = res["query"]
            tr.queries = res["used"]
            tr.candidates = res["results"]
            tr.chosen = c
            tr.meta = final_meta(c)
            tr.edited = False
            tr.approved = False
            tr.status = Status.MATCHED if c.score >= 75 else Status.REVIEW
            tr.message = f"picked by you · {c.source}"
            self.refresh_track(tr)
            self.log_line(f"Picked [b]{tr.meta['title']}[/] — {tr.meta['artist']} "
                          f"({tr.meta['year'] or '?'}) · getting its cover…")
            self.cover_worker(tr, "download")

        self.push_screen(SearchPickScreen(tr, start_in_results), done)

    def action_candidates(self) -> None:
        self.action_search(start_in_results=True)

    # -------------------------------------------------------------- cover
    @work(thread=True, group="cover")
    def cover_worker(self, tr: Track, mode: str) -> None:
        if mode == "download":
            if not organizer.download_cover(tr):
                if tr.cover_source != "online":
                    organizer.generate_cover(tr)
                self.call_from_thread(self.notify, "No online cover found — generated one instead")
        self.save_drafts()
        self.call_from_thread(self.refresh_track, tr)

    def action_cover_generate(self) -> None:
        tr = self.current()
        if not tr:
            return
        if not tr.meta:
            self.notify("Needs a draft first (f, / or e)", severity="warning")
            return
        organizer.generate_cover(tr)
        self.save_drafts()
        self.refresh_track(tr)
        self.log_line("New cover generated — press [b]g[/] again for a different one.")

    def action_cover_download(self) -> None:
        tr = self.current()
        if not tr or not tr.meta:
            return
        if not tr.chosen:
            self.notify("Fetch this song again (f) to get its online cover", severity="warning")
            return
        self.cover_worker(tr, "download")

    def action_cover_keep(self) -> None:
        tr = self.current()
        if tr and organizer.keep_existing_cover(tr):
            self.save_drafts()
            self.refresh_track(tr)
        elif tr:
            self.notify("This file has no embedded cover", severity="warning")

    def action_cover_file(self) -> None:
        tr = self.current()
        if not tr:
            return
        if not tr.meta:
            self.notify("Needs a draft first (f, / or e)", severity="warning")
            return

        def done(p: Path | None) -> None:
            if not p:
                return
            try:
                organizer.cover_from_file(tr, p)
            except (OSError, ValueError) as e:
                self.notify(str(e), severity="error")
                return
            self.save_drafts()
            self.refresh_track(tr)
            self.log_line(f"Cover set from [b]{p.name}[/]")

        self.push_screen(ImagePicker(tr.path.parent), done)

    def action_cover_view(self) -> None:
        tr = self.current()
        if not tr or not tr.cover or not self.store:
            return
        self.save_drafts()
        path = self.store.cover_path(tr)
        opener = shutil.which("xdg-open")
        if path and opener:
            subprocess.Popen([opener, str(path)], stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL, start_new_session=True)
        elif path:
            self.log_line(f"Cover draft: {path}")

    # -------------------------------------------------------------- write
    def action_write(self) -> None:
        if not self._guard():
            return
        todo = [t for t in self.tracks if t.approved and t.meta]
        if not todo:
            self.notify("Nothing approved yet — select a song and press space", severity="warning")
            return
        renames = sum(1 for t in todo if (p := organizer.target_path(t)) and p.name != t.path.name)
        body = (f"This will modify [b]{len(todo)}[/] song file(s):\n\n"
                f" • replace all their tags with the reviewed drafts\n"
                f" • embed the draft cover art\n"
                f" • rename {renames} file(s)\n\n"
                f"Renames can be undone later with [b]u[/]; old tags cannot.")

        def done(res: dict | None) -> None:
            if res:
                self.write_worker(todo, res["rename"])

        self.push_screen(ConfirmScreen("Write approved drafts?", body, f"Write {len(todo)} songs",
                                       rename_option=True), done)

    @work(thread=True, exclusive=True, group="write")
    def write_worker(self, todo: list[Track], rename: bool) -> None:
        self.busy = True
        self.call_from_thread(self._progress_start, len(todo), "Writing")
        ok = 0
        try:
            for tr in todo:
                old = shown(tr.path.name)
                try:
                    organizer.write(tr, rename=rename)
                    ok += 1
                    tr.cur_title, tr.cur_artist = tr.meta["title"], tr.meta["artist"]
                    tr.cur_album, tr.cur_year = tr.meta.get("album", ""), tr.meta.get("year", "")
                    self.call_from_thread(self.log_line, f"[green]✓[/] {old}  →  [b]{tr.path.name}[/]")
                except Exception as e:
                    tr.status, tr.message = Status.ERROR, f"write failed: {e}"
                    self.call_from_thread(self.log_line, f"[red]✗ {old}: {e}[/]")
                self.call_from_thread(self.refresh_track, tr)
                self.call_from_thread(self._progress_step)
                self.save_drafts_soon()
        finally:
            self.busy = False
            self.save_drafts()
            self.call_from_thread(self._progress_done)
            self.call_from_thread(self.log_line, f"Wrote {ok}/{len(todo)} songs.")

    def action_undo(self) -> None:
        if not self.folder or not self._guard():
            return

        def done(res) -> None:
            if not res:
                return
            moved = organizer.undo_renames(self.folder)
            self.log_line(f"Undid {len(moved)} renames (tags stay as written).")
            self.load_folder(self.folder)

        self.push_screen(ConfirmScreen("Undo renames?",
                                       "Rename every file this app renamed in this folder back "
                                       "to its original name. Tags are not changed.", "Undo renames"),
                         done)

    # ------------------------------------------------------ cover preview
    def _sync_preview(self) -> None:
        """Keep the ueberzug++ image matching what's on screen right now."""
        p = self.preview
        if not p:
            return
        if not p.alive:
            self.preview = None
            self.log_line("[dim]ueberzug++ stopped - using the built-in preview.[/]")
            self.show_details(self.current())
            return
        tr = self.current()
        if len(self.screen_stack) > 1 or tr is None or not tr.cover:
            p.hide()     # a dialog is open, or nothing to show
            return
        r = self.query_one("#cover", Static).content_region
        if r.width < 4 or r.height < 2:
            p.hide()
            return
        if not p.show(tr.cover, r.x, r.y, r.width, r.height):
            p.hide()

    def _stop_preview(self) -> None:
        if self.preview:
            self.preview.close()
            self.preview = None

    def on_unmount(self) -> None:
        self.save_drafts()   # however the app ends (q, ctrl+q, closed terminal…)
        self._stop_preview()

    async def action_quit(self) -> None:
        self.save_drafts()
        self._stop_preview()
        self.exit()
