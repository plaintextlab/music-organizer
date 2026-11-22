"""Textual TUI: scan -> fetch drafts -> review / edit -> approve -> write."""

from __future__ import annotations

import io
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
from textual.theme import Theme
from textual.widgets import (Button, Checkbox, DataTable, DirectoryTree, Footer, Header,
                             Input, Label, OptionList, ProgressBar, RichLog, Static)
from textual.widgets.option_list import Option

from . import organizer
from .drafts import DraftStore
from .matcher import blank_meta, final_meta, lookup
from .models import Status, Track
from .romanize import to_english
from .scanner import filename_parts, read_track, scan

IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".bmp", ".gif"}

NEON = Theme(
    name="music-neon",
    primary="#ff4fd8",
    secondary="#00e5ff",
    accent="#ffd166",
    foreground="#e9e4ff",
    background="#0e0a1c",
    surface="#161029",
    panel="#20173d",
    success="#3ddc97",
    warning="#ffb020",
    error="#ff5470",
    dark=True,
)

STATUS_STYLE = {
    Status.PENDING: ("·  scanned", "dim"),
    Status.SEARCHING: ("⟳ searching", "bold #00e5ff"),
    Status.MATCHED: ("◆ draft", "bold #3ddc97"),
    Status.REVIEW: ("◇ check", "bold #ffb020"),
    Status.NOT_FOUND: ("✗ no match", "bold #ff5470"),
    Status.DONE: ("✓ written", "bold #8a7dff"),
    Status.ERROR: ("! error", "bold #ff5470"),
}
ART_STYLE = {"online": ("web", "#00e5ff"), "existing": ("kept", "#b8b0d8"),
             "generated": ("gen", "#ff4fd8"), "file": ("file", "#ffd166"), "": ("—", "dim")}

FIELDS = [("title", "Title"), ("artist", "Artist(s)"), ("album", "Album / Movie"),
          ("albumartist", "Album artist"), ("year", "Year"), ("genre", "Genre"),
          ("composer", "Composer"), ("label", "Label")]


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
            yield Label(f"Edit draft · {self.track.path.name}", classes="dialog-title")
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
                                                                (name, "bold #ffd166")))

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


class SearchScreen(_Modal):
    def __init__(self, track: Track, guess: str):
        super().__init__()
        self.track, self.guess = track, guess

    def compose(self) -> ComposeResult:
        with Vertical(classes="dialog"):
            yield Label("Search online", classes="dialog-title")
            yield Static(f"{self.track.path.name}", classes="hint")
            yield Input(self.guess, id="q", placeholder="song name - artist")
            yield Static("Tip: 'Song - Artist' or 'Song Movie' works best.", classes="hint")
            with Horizontal(classes="buttons"):
                yield Button("Search", variant="primary", id="ok")
                yield Button("Cancel", id="cancel")

    @on(Input.Submitted)
    @on(Button.Pressed, "#ok")
    def _ok(self) -> None:
        q = self.query_one("#q", Input).value.strip()
        self.dismiss(q or None)

    @on(Button.Pressed, "#cancel")
    def _cancel(self) -> None:
        self.dismiss(None)


class CandidatesScreen(_Modal):
    def __init__(self, track: Track):
        super().__init__()
        self.track = track

    def compose(self) -> ComposeResult:
        with Vertical(classes="dialog wide"):
            yield Label("Pick the right match", classes="dialog-title")
            opts = []
            for i, c in enumerate(self.track.candidates):
                m = final_meta(c)
                line = Text.assemble(
                    (f"{min(100, c.score):3.0f}% ", "bold #3ddc97" if c.score >= 75 else "#ffb020"),
                    (f"{c.source:<11} ", "dim"),
                    (m["title"], "bold"), ("  ·  ", "dim"), (m["artist"], "#00e5ff"),
                    ("  ·  ", "dim"), (m["album"] or "—", ""), (f"  ({m['year'] or '?'})", "#ffd166"),
                )
                opts.append(Option(line, id=str(i)))
            yield OptionList(*opts, id="cands")
            with Horizontal(classes="buttons"):
                yield Button("Cancel", id="cancel")

    @on(OptionList.OptionSelected)
    def _picked(self, ev: OptionList.OptionSelected) -> None:
        self.dismiss(int(ev.option.id))

    @on(Button.Pressed, "#cancel")
    def _cancel(self) -> None:
        self.dismiss(None)


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
        Binding("f", "fetch", "Fetch drafts"),
        Binding("space", "approve", "Approve"),
        Binding("a", "approve_matched", "Approve all ◆"),
        Binding("e", "edit", "Edit"),
        Binding("slash", "search", "Search"),
        Binding("c", "candidates", "Matches"),
        Binding("i", "cover_file", "Art: file"),
        Binding("g", "cover_generate", "Art: generate"),
        Binding("d", "cover_download", "Art: web"),
        Binding("k", "cover_keep", "Art: keep old", show=False),
        Binding("v", "cover_view", "View art"),
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

    # ------------------------------------------------------------- layout
    def compose(self) -> ComposeResult:
        yield Header(show_clock=False)
        with Horizontal(id="topbar"):
            yield Label(" FOLDER ", id="folder-tag")
            yield Static("press  o  to choose a music folder", id="folder")
            yield Static("", id="counts")
        with Horizontal(id="main"):
            yield DataTable(id="table", cursor_type="row", zebra_stripes=True)
            with VerticalScroll(id="side"):
                yield Static(id="cover")
                yield Static(id="cover-info")
                yield Static(id="details")
        with Horizontal(id="progress-row"):
            yield ProgressBar(id="progress", show_eta=False)
            yield Static("", id="progress-label")
        yield RichLog(id="log", markup=True, wrap=True, max_lines=500)
        yield Footer()

    def on_mount(self) -> None:
        self.register_theme(NEON)
        self.theme = "music-neon"
        t = self.query_one(DataTable)
        t.add_column("✓", key="ok", width=2)
        t.add_column("State", key="state", width=12)
        t.add_column("Current file", key="file")
        t.add_column("→ Draft file name", key="new")
        t.add_column("Art", key="art", width=5)
        self.query_one("#progress-row").display = False
        self.log_line("[b #ff4fd8]Welcome![/] Workflow: [b]o[/] open folder → [b]f[/] fetch drafts → "
                      "review / [b]e[/] edit / cover keys → [b]space[/] approve → [b]w[/] write.")
        self.log_line("[dim]Drafts are kept in ~/.cache/music-organizer — your songs are not touched "
                      "until you press w and confirm.[/]")
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
        ok = Text("●", "bold #3ddc97") if tr.approved else Text("○", "dim")
        new = organizer.target_path(tr)
        if new is None:
            new_txt = Text("—", "dim")
        elif new.name == tr.path.name:
            new_txt = Text(new.name + "  (same)", "dim")
        else:
            new_txt = Text(new.name, "#ffd166" if tr.edited else "")
        art, art_style = ART_STYLE.get(tr.cover_source, ART_STYLE[""])
        return [ok, Text(label, style), Text(tr.path.name, "#b8b0d8"), new_txt, Text(art, art_style)]

    def refresh_track(self, tr: Track) -> None:
        try:
            i = self.tracks.index(tr)
        except ValueError:
            return
        t = self.query_one(DataTable)
        for col, val in zip(("ok", "state", "file", "new", "art"), self._row(tr)):
            t.update_cell(str(i), col, val, update_width=col in ("file", "new"))
        if tr is self.current():
            self.show_details(tr)
        self.update_counts()

    def update_counts(self) -> None:
        n = len(self.tracks)
        by = lambda s: sum(1 for t in self.tracks if t.status == s)
        appr = sum(1 for t in self.tracks if t.approved)
        self.query_one("#counts", Static).update(Text.assemble(
            (f" {n} songs ", "bold"), (f" ◆ {by(Status.MATCHED)} ", "#3ddc97"),
            (f" ◇ {by(Status.REVIEW)} ", "#ffb020"), (f" ✗ {by(Status.NOT_FOUND)} ", "#ff5470"),
            (f" ● {appr} approved ", "bold #3ddc97"), (f" ✓ {by(Status.DONE)} written ", "#8a7dff"),
        ))

    def show_details(self, tr: Track | None) -> None:
        cover = self.query_one("#cover", Static)
        info = self.query_one("#cover-info", Static)
        details = self.query_one("#details", Static)
        if tr is None:
            cover.update("")
            info.update("")
            details.update("")
            return
        if tr.cover:
            try:
                cover.update(halfblock(tr.cover))
            except Exception:
                cover.update(Text("(cover can't be previewed)", "dim"))
        else:
            cover.update(Text("\n\n      no cover in draft\n\n   g generate · i from file\n", "dim"))
        art = {"online": "downloaded", "existing": "already in the file", "generated": "generated",
               "file": "from your image file", "": "none"}[tr.cover_source]
        info.update(Text.assemble(("Cover: ", "dim"), (art, "bold"),
                                  ("   v opens it full size", "dim") if tr.cover else ""))

        tbl = Table.grid(padding=(0, 1))
        tbl.add_column(style="dim", no_wrap=True)
        tbl.add_column(overflow="fold")
        cur = {"title": tr.cur_title, "artist": tr.cur_artist, "album": tr.cur_album, "year": tr.cur_year}
        m = tr.meta or {}
        for key, label in FIELDS:
            new = str(m.get(key, "") or "")
            old = cur.get(key, "")
            if not m:
                tbl.add_row(label, Text(old or "—", "#b8b0d8"))
            elif old and old != new and key in cur:
                tbl.add_row(label, Text.assemble((new or "—", "bold #3ddc97"), ("\n  was: ", "dim"),
                                                 (old, "dim strike")))
            else:
                tbl.add_row(label, Text(new or "—", "bold" if new else "dim"))
        label, style = STATUS_STYLE[tr.status]
        head = Text.assemble((label, style), ("   ", ""),
                             ("APPROVED" if tr.approved else "not approved",
                              "bold #3ddc97" if tr.approved else "dim"),
                             ("  · edited by you" if tr.edited else "", "#ffd166"))
        new = organizer.target_path(tr)
        foot = Text.assemble(("\nfile  ", "dim"), (tr.path.name, "#b8b0d8"),
                             ("\nnew   ", "dim"), (new.name if new else "—", "bold #ffd166"),
                             ("\n\n" + tr.message, "dim italic") if tr.message else "")
        from rich.console import Group
        details.update(Group(head, Text(""), tbl, foot))

    @on(DataTable.RowHighlighted)
    def _highlight(self, _ev) -> None:
        self.show_details(self.current())

    def save_drafts(self) -> None:
        if self.store:
            try:
                self.store.save(self.tracks)
            except OSError as e:
                self.log_line(f"[#ff5470]could not save drafts: {e}[/]")

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
        self.query_one("#folder", Static).update(Text(str(self.folder), "bold #00e5ff"))
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
                self.log_line(f"Re-fetching [b]{tr.path.name}[/]")
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
                f"Drafts ready: [#3ddc97]{m} confident[/], [#ffb020]{r} to check[/], "
                f"[#ff5470]{n} not found[/]. Nothing has been written yet — review, approve "
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

    def action_search(self) -> None:
        tr = self.current()
        if not tr or not self._guard():
            return
        guess = tr.custom_query or (f"{tr.meta['title']} - {tr.meta['artist']}" if tr.meta
                                    else filename_parts(tr.path)[0])

        def done(q: str | None) -> None:
            if q:
                tr.custom_query = q
                self.log_line(f"Searching for [b]{q}[/] …")
                self.fetch_worker([tr])

        self.push_screen(SearchScreen(tr, guess), done)

    def action_candidates(self) -> None:
        tr = self.current()
        if not tr or not self._guard():
            return
        if not tr.candidates:
            self.notify("No matches loaded for this song — press / to search", severity="warning")
            return

        def done(i: int | None) -> None:
            if i is None:
                return
            c = tr.candidates[i]
            tr.chosen = c
            tr.meta = final_meta(c)
            tr.edited = False
            tr.status = Status.MATCHED if c.score >= 75 else Status.REVIEW
            tr.message = f"picked by you · {c.source}"
            self.cover_worker(tr, "download")

        self.push_screen(CandidatesScreen(tr), done)

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
                old = tr.path.name
                try:
                    organizer.write(tr, rename=rename)
                    ok += 1
                    tr.cur_title, tr.cur_artist = tr.meta["title"], tr.meta["artist"]
                    tr.cur_album, tr.cur_year = tr.meta.get("album", ""), tr.meta.get("year", "")
                    self.call_from_thread(self.log_line, f"[#3ddc97]✓[/] {old}  →  [b]{tr.path.name}[/]")
                except Exception as e:
                    tr.status, tr.message = Status.ERROR, f"write failed: {e}"
                    self.call_from_thread(self.log_line, f"[#ff5470]✗ {old}: {e}[/]")
                self.call_from_thread(self.refresh_track, tr)
                self.call_from_thread(self._progress_step)
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

    async def action_quit(self) -> None:
        self.save_drafts()
        self.exit()
