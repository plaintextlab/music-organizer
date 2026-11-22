"""Entry point:  music-organizer [FOLDER] | --undo FOLDER"""

from __future__ import annotations

import argparse
from pathlib import Path


def main() -> None:
    ap = argparse.ArgumentParser(
        prog="music-organizer",
        description="Fix song tags, album art and file names from free online sources. "
                    "Changes are drafted first; nothing is written until you approve.")
    ap.add_argument("folder", nargs="?", help="music folder to open")
    ap.add_argument("--undo", metavar="FOLDER",
                    help="rename files in FOLDER back to their original names and exit")
    ap.add_argument("--discard-drafts", metavar="FOLDER",
                    help="delete the saved drafts for FOLDER and exit")
    args = ap.parse_args()

    if args.undo:
        from .organizer import undo_renames
        for src, dst in undo_renames(Path(args.undo).expanduser()):
            print(f"{src.name}  ->  {dst.name}")
        return
    if args.discard_drafts:
        from .drafts import DraftStore
        st = DraftStore(Path(args.discard_drafts).expanduser())
        st.discard()
        print(f"removed {st.dir}")
        return

    folder = Path(args.folder).expanduser() if args.folder else None
    if folder and not folder.is_dir():
        ap.error(f"not a folder: {folder}")

    from .app import MusicOrganizer
    MusicOrganizer(folder).run()


if __name__ == "__main__":
    main()
