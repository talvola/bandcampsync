"""Rename slug-named single-track files to use 'Artist - Title.flac' naming.

Finds single-track directories where the FLAC file has a URL slug name
(from direct track downloads, not ZIP extraction) and renames it to match
the directory name.

Usage:
    python fix_track_names.py [media_dir]              # Dry run (report only)
    python fix_track_names.py [media_dir] --execute    # Actually rename files
"""

import io
import os
import sys
from pathlib import Path

if sys.stdout.encoding != "utf-8":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

IGNORABLE = {"Thumbs.db", "desktop.ini", ".DS_Store", "bandcamp_item_id.txt"}


def find_misnamed_tracks(media_dir):
    """Find single-track directories where the FLAC has a slug name."""
    base = Path(media_dir)
    results = []

    dirs_to_check = []
    for entry in os.scandir(str(base)):
        if not entry.is_dir():
            continue
        # Top-level album dir
        if " - " in entry.name:
            dirs_to_check.append((entry.path, None))
        # Label subdir
        for sub in os.scandir(entry.path):
            if sub.is_dir() and " - " in sub.name:
                dirs_to_check.append((sub.path, entry.name))

    for dirpath, label in dirs_to_check:
        dirname = os.path.basename(dirpath)

        # Get audio files
        audio_files = []
        for f in os.scandir(dirpath):
            if f.is_file() and f.name not in IGNORABLE and f.name.lower().endswith(".flac"):
                audio_files.append(f.name)

        # Only single-track directories
        if len(audio_files) != 1:
            continue

        current = audio_files[0]
        expected = f"{dirname}.flac"

        # Skip if already correctly named
        if current == expected:
            continue

        stem = current.rsplit(".", 1)[0]

        # Skip files from ZIP extraction (start with dir name or contain ' - ')
        if current.startswith(dirname) or " - " in stem:
            continue

        results.append({
            "dir": dirpath,
            "label": label,
            "dirname": dirname,
            "current": current,
            "expected": expected,
        })

    return results


def fix_tracks(results, execute=False):
    mode = "EXECUTING" if execute else "DRY RUN"
    print(f"\n{'=' * 60}")
    print(f"TRACK RENAME ({mode})")
    print(f"{'=' * 60}")
    print(f"Tracks to rename: {len(results)}\n")

    renamed = 0
    errors = 0

    for r in results:
        label_str = f" (in {r['label']})" if r["label"] else ""
        print(f"  {r['dirname']}{label_str}")
        print(f"    {r['current']}")
        print(f"    -> {r['expected']}")

        if execute:
            src = Path(r["dir"]) / r["current"]
            dst = Path(r["dir"]) / r["expected"]
            try:
                src.rename(dst)
                print(f"    [OK]")
                renamed += 1
            except OSError as e:
                print(f"    [ERROR] {e}")
                errors += 1
        print()

    print(f"{'-' * 60}")
    if execute:
        print(f"Renamed: {renamed}, Errors: {errors}")
    else:
        print("Run with --execute to apply changes.")


if __name__ == "__main__":
    media_dir = r"N:\Bandcamp (FLAC)"
    args = sys.argv[1:]
    positional = [a for a in args if not a.startswith("--")]
    if positional:
        media_dir = positional[0]

    do_execute = "--execute" in args

    results = find_misnamed_tracks(media_dir)
    fix_tracks(results, execute=do_execute)
