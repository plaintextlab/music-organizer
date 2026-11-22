# music-organizer

A terminal (TUI) app for Linux that fixes your music library: correct tags,
album art and clean file names, from **free sources only** (no API keys, no
accounts). Made with Hindi, Bengali and English music in mind.

```
Tanha Dil [Songs.PK].mp3      →   Tanha Dil (2006) - Shaan.mp3
Tum_Hi_Ho-(Mr-Jatt.com).m4a   →   Tum Hi Ho (2013) - Arijit Singh.m4a
```

## Run it

Nothing to install by hand: just run the launcher.

```bash
./run.sh            # opens the app, pick a folder with  o
./run.sh ~/Music    # or open a folder directly
```

The first run creates a private Python environment in `.venv/` inside this
folder and installs what it needs (takes a few seconds, only once). It doesn't
install anything system-wide. Later runs start instantly. To run it from
anywhere, symlink it onto your PATH:

```bash
ln -s "$PWD/run.sh" ~/.local/bin/music-organizer
```

Needs Python 3.10+ (with `python3-venv` on Debian/Ubuntu). Supports
**mp3, m4a, flac, ogg, opus** (subfolders included).

## How it works — drafts first, nothing written until you confirm

1. **`o`** choose a folder (or pass it on the command line).
2. **`f`** fetch drafts. Each song is looked up online and gets a *draft*:
   proposed tags, new file name and a cover. Your files are **not touched**.
   Drafts are saved in `~/.cache/music-organizer/drafts/` (covers as `.jpg`
   you can open in any image viewer) and are restored next time you open the folder.
3. Review. The right panel shows the cover and the draft next to the current tags.
   - ◆ **draft** – confident match  ◇ **check** – please look  ✗ **no match**
4. Fix anything you don't like (all of these only change the draft):

   | key | action |
   |---|---|
   | `e` | edit every field by hand (title, artists, album, year, genre, …) |
   | `/` | search again with your own text, e.g. `Pehla Nasha - Udit Narayan` |
   | `c` | pick a different match from the list of results |
   | `i` | use an image file from disk as the cover |
   | `g` | generate a new cover (random colours/style each press) |
   | `d` | go back to the downloaded cover |
   | `k` | keep the cover already embedded in the file |
   | `v` | open the draft cover full size in your image viewer |

5. **`space`** approve a song (or **`a`** approve all ◆ confident ones).
6. **`w`** write. A confirmation dialog shows exactly what will change; only
   approved songs are written. You can untick renaming there.

`u` (or `./run.sh --undo FOLDER`) renames files back to their original
names. Tag changes can't be undone, which is why everything goes through drafts.

## What gets written

- Title, Artist(s), Album/Movie, Album artist (music director for film songs),
  Year, Genre/Language, Composer, Label, and the front cover.
- All old tags are removed first (site watermarks like `www.SongsPk.com`, junk comments).
- MP3s are saved as ID3v2.3 for compatibility with phones, cars and Windows.
- **English only**: anything in Devanagari/Bengali or other scripts is
  transliterated (`तन्हा दिल` → `Tanha Dil`), including what you type in the editor.
- File name: `Song (Year) - Artist.ext` (up to 3 artists).

## Sources (all free, no key)

- **JioSaavn** public web API – best for Hindi/Bengali/Indian music
- **iTunes Search API** – large catalogue (India + US stores)
- **MusicBrainz + Cover Art Archive** – open data fallback (rate limited to 1 req/s)

Matches are scored on title, artist, duration, and how many sources agree;
remixes, lo-fi/cover versions and compilations are ranked down unless your
file name says so.

If no cover exists anywhere, a cover is generated with the artist name and year.
