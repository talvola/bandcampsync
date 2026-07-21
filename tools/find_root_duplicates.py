#!/usr/bin/env python
"""
Find albums that exist BOTH at the root of the media directory and inside a label
subdirectory, and report how their track lists relate so the pair can be reviewed by hand.

The purchased-collection sync writes albums flat at the top level when no artist directory
exists, while bandcampfree always writes into media_dir/<Label>/. An album fetched by both
therefore ends up in two places, and neither tool sees the other's copy.

This only reports. Nothing is moved or deleted: deciding which copy to keep needs a human,
because the two are often not identical (bandcamp re-encodes, labels extend compilations,
and album titles are not unique).

Usage:
    uv run python tools/find_root_duplicates.py "N:/Bandcamp (FLAC)"
    uv run python tools/find_root_duplicates.py "N:/Bandcamp (FLAC)" --tsv dupes.tsv
"""

import argparse
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from bandcampsync.media import LocalMedia  # noqa: E402

# Album and track names are heavily unicode (Japanese, Cyrillic, decorated vaporwave
# titles). The Windows console defaults to cp1252 and would abort mid-report.
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except AttributeError:  # pragma: no cover - very old interpreters
    pass

AUDIO_SUFFIXES = {".flac", ".mp3", ".wav", ".m4a", ".aiff", ".alac", ".ogg"}


def album_title(dirname):
    """'Artist - Album' -> 'Album'. Leaves names without a separator alone."""
    return dirname.split(" - ", 1)[1] if " - " in dirname else dirname


def track_keys(directory):
    """Return a set of comparable track identities for one album directory.

    Bandcamp embeds the album title in every filename, and that title differs between two
    copies when a label has renamed the release, so the shared leading prefix is stripped
    before comparing. Falls back to the whole name when there is no common prefix.
    """
    names = [
        p.name
        for p in directory.iterdir()
        if p.is_file() and p.suffix.lower() in AUDIO_SUFFIXES
    ]
    if not names:
        return set()
    prefix = os.path.commonprefix(names) if len(names) > 1 else ""
    cut = prefix.rfind(" - ")
    prefix = prefix[: cut + 3] if cut != -1 else ""
    keys = set()
    for name in names:
        stem = name[len(prefix) :] if prefix and name.startswith(prefix) else name
        stem = stem.rsplit(".", 1)[0]
        keys.add(LocalMedia._normalize_for_match(stem))
    return keys


def relation(root_keys, sub_keys):
    """Describe how two track sets relate, for a human deciding which copy to keep."""
    if not root_keys and not sub_keys:
        return "BOTH-EMPTY", ""
    if not root_keys:
        return "ROOT-EMPTY", ""
    if not sub_keys:
        return "SUB-EMPTY", ""
    if root_keys == sub_keys:
        return "IDENTICAL", "same track list - safe to delete either"
    if root_keys < sub_keys:
        return (
            "ROOT-SUBSET",
            f"subdir has {len(sub_keys - root_keys)} extra - keep subdir",
        )
    if sub_keys < root_keys:
        return "SUB-SUBSET", f"root has {len(root_keys - sub_keys)} extra - keep root"
    overlap = root_keys & sub_keys
    if overlap:
        return "PARTIAL", (
            f"{len(overlap)} shared, {len(root_keys - sub_keys)} only-root, "
            f"{len(sub_keys - root_keys)} only-subdir"
        )
    return "DISJOINT", "no shared tracks - probably different albums, do not merge"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("media_dir")
    parser.add_argument("--tsv", default="", help="also write a tab-separated file")
    parser.add_argument(
        "--min-tracks",
        type=int,
        default=1,
        help="ignore directories with fewer audio files than this",
    )
    args = parser.parse_args()

    media = Path(args.media_dir)
    if not media.is_dir():
        sys.exit(f"Not a directory: {media}")

    # Root-level album directories, and label subdirectories, in one pass.
    root_dirs, label_dirs = {}, {}
    for child in sorted(p for p in media.iterdir() if p.is_dir()):
        subs = [p for p in child.iterdir() if p.is_dir()]
        has_audio = any(
            p.suffix.lower() in AUDIO_SUFFIXES for p in child.iterdir() if p.is_file()
        )
        if has_audio:
            root_dirs.setdefault(
                LocalMedia._normalize_for_match(album_title(child.name)), []
            ).append(child)
        if subs:
            label_dirs[child.name] = subs

    sub_index = {}
    for label, subs in label_dirs.items():
        for sub in subs:
            key = LocalMedia._normalize_for_match(album_title(sub.name))
            sub_index.setdefault(key, []).append((label, sub))

    print(f"root album directories : {sum(len(v) for v in root_dirs.values())}")
    print(f"label subdirectories   : {sum(len(v) for v in sub_index.values())}")
    print(f"labels                 : {len(label_dirs)}\n")

    rows = []
    for key in sorted(set(root_dirs) & set(sub_index)):
        for root in root_dirs[key]:
            rk = track_keys(root)
            if len(rk) < args.min_tracks:
                continue
            for label, sub in sub_index[key]:
                sk = track_keys(sub)
                verdict, note = relation(rk, sk)
                rows.append(
                    (verdict, label, root.name, sub.name, len(rk), len(sk), note)
                )

    order = [
        "IDENTICAL",
        "ROOT-SUBSET",
        "SUB-SUBSET",
        "PARTIAL",
        "DISJOINT",
        "ROOT-EMPTY",
        "SUB-EMPTY",
        "BOTH-EMPTY",
    ]
    rows.sort(key=lambda r: (order.index(r[0]) if r[0] in order else 99, r[1], r[2]))

    current = None
    for verdict, label, rootname, subname, nr, ns, note in rows:
        if verdict != current:
            current = verdict
            print(f"\n=== {verdict} ===")
        print(f"  [{label}]  {note}")
        print(f"     root   ({nr:>3} tracks): {rootname}")
        print(f"     subdir ({ns:>3} tracks): {label}\\{subname}")

    print(f"\n{len(rows)} candidate pair(s)")
    counts = {}
    for r in rows:
        counts[r[0]] = counts.get(r[0], 0) + 1
    for verdict in order:
        if verdict in counts:
            print(f"  {verdict:<12} {counts[verdict]}")

    if args.tsv:
        with open(args.tsv, "wt", encoding="utf-8") as f:
            f.write(
                "verdict\tlabel\troot_dir\tsub_dir\troot_tracks\tsub_tracks\tnote\n"
            )
            for r in rows:
                f.write(
                    "\t".join(
                        str(x) for x in (r[0], r[1], r[2], r[3], r[4], r[5], r[6])
                    )
                    + "\n"
                )
        print(f"\nwrote {args.tsv}")


if __name__ == "__main__":
    main()
