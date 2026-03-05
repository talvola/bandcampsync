"""Find duplicate directories in the music library.

Detects cases where the same album exists both at the top level
(e.g., "Artist - Album") and inside a label subdirectory
(e.g., "LabelName/Artist - Album").

Compares file names and sizes to confirm duplicates.

Usage:
    python find_duplicates.py [media_dir]              # Report only
    python find_duplicates.py [media_dir] --review     # Detailed review of non-safe items
    python find_duplicates.py [media_dir] --resolve    # Dry-run: show what would be done
    python find_duplicates.py [media_dir] --resolve --execute  # Actually resolve safe duplicates
"""

import io
import os
import shutil
import sys
import unicodedata
from pathlib import Path
from collections import defaultdict

# Force UTF-8 output on Windows
if sys.stdout.encoding != "utf-8":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

ITEM_INDEX_FILENAME = "bandcamp_item_id.txt"

# Characters that bandcampsync's _clean_path() strips from filenames
SANITIZED_CHARS = set("#%'*/?\\:")

IGNORABLE_FILES = {"Thumbs.db", "desktop.ini", ".DS_Store"}


def sanitize_name(name):
    """Normalize a filename for comparison.

    Applies the same stripping as bandcampsync's _clean_path(), plus:
    - NFC Unicode normalization
    - Strips combining diacritical marks (handles decomposed vs precomposed)
    - Normalizes smart quotes and accent marks to nothing (like _clean_path strips ')
    """
    # First NFC-normalize so we have a consistent base
    name = unicodedata.normalize("NFC", name)
    # Strip the characters _clean_path removes
    name = "".join(c for c in name if c not in SANITIZED_CHARS)
    # Decompose to NFD so we can strip combining marks
    name = unicodedata.normalize("NFD", name)
    # Remove combining diacritical marks (category "M")
    name = "".join(c for c in name if unicodedata.category(c) != "Mn")
    # Re-compose
    name = unicodedata.normalize("NFC", name)
    # Normalize various quote-like and accent characters to nothing
    name = name.replace("\u00B4", "")  # acute accent ´
    name = name.replace("\u2018", "")  # left single quote '
    name = name.replace("\u2019", "")  # right single quote '
    # Normalize ellipsis
    name = name.replace("\u2026", "...")
    # Collapse whitespace (stripping combining marks can leave extra spaces)
    name = " ".join(name.split())
    return name


def names_match_after_sanitize(name_a, name_b):
    """Check if two filenames match after applying path sanitization.

    Also tries matching with spaces removed to handle cases where
    combining accents acting as apostrophes leave orphan spaces.
    """
    sa, sb = sanitize_name(name_a), sanitize_name(name_b)
    if sa == sb:
        return True
    # Fallback: compare without spaces (handles 'Paddy s' vs 'Paddys')
    return sa.replace(" ", "") == sb.replace(" ", "")


def build_sanitized_mapping(names_a, names_b):
    """Try to pair up filenames that differ only by sanitized characters.

    Returns dict mapping name_a -> name_b for matched pairs, or None if
    not all unpaired names can be matched.
    """
    mapping = {}
    unmatched_b = set(names_b)
    for na in names_a:
        for nb in list(unmatched_b):
            if names_match_after_sanitize(na, nb):
                mapping[na] = nb
                unmatched_b.discard(nb)
                break
    return mapping if not unmatched_b else None


def get_file_inventory(dirpath):
    """Return dict of {filename: size} for all files in a directory (non-recursive)."""
    inventory = {}
    try:
        for entry in os.scandir(dirpath):
            if entry.is_file():
                if entry.name == ITEM_INDEX_FILENAME:
                    continue
                try:
                    inventory[entry.name] = entry.stat().st_size
                except OSError:
                    pass
    except OSError:
        pass
    return inventory


def get_item_id(dirpath):
    """Read the bandcamp_item_id.txt from a directory, or return None."""
    id_file = Path(dirpath) / ITEM_INDEX_FILENAME
    if id_file.is_file():
        try:
            return int(id_file.read_text().strip())
        except (ValueError, OSError):
            pass
    return None


def find_label_dirs(media_dir):
    """Find directories that contain subdirectories with Artist - Album pattern."""
    labels = {}
    for entry in os.scandir(media_dir):
        if not entry.is_dir():
            continue
        album_subdirs = []
        try:
            for sub in os.scandir(entry.path):
                if sub.is_dir() and " - " in sub.name:
                    album_subdirs.append(sub.name)
        except OSError:
            continue
        if album_subdirs:
            labels[entry.name] = album_subdirs
    return labels


def classify_overlap(top_files, label_files):
    """Classify the overlap between two file inventories."""
    if not top_files and not label_files:
        return "both-empty", ""

    # Filter out ignorable files (Thumbs.db, etc.) for classification
    top_clean = {k: v for k, v in top_files.items() if k not in IGNORABLE_FILES}
    label_clean = {k: v for k, v in label_files.items() if k not in IGNORABLE_FILES}

    if not top_clean and label_clean:
        return "empty-top", f"top has no real files, label has {len(label_clean)}"
    if top_clean and not label_clean:
        return "empty-label", f"label has no real files, top has {len(top_clean)}"
    if not top_clean and not label_clean:
        return "both-empty", ""

    if top_clean == label_clean:
        return "exact", ""

    top_names = set(top_clean.keys())
    label_names = set(label_clean.keys())

    if top_names == label_names:
        diffs = []
        for name in top_names:
            if top_clean[name] != label_clean[name]:
                diff = abs(top_clean[name] - label_clean[name])
                diffs.append((name, diff))
        max_diff = max(d[1] for d in diffs) if diffs else 0
        return "same-files-diff-sizes", f"max diff: {max_diff:,} bytes"

    common = top_names & label_names
    only_top = top_names - label_names
    only_label = label_names - top_names

    # Before declaring partial-overlap, check if unmatched files pair up
    # after applying the same character sanitization bandcampsync uses
    if only_top and only_label and len(only_top) == len(only_label):
        mapping = build_sanitized_mapping(only_top, only_label)
        if mapping:
            n = len(common) + len(mapping)
            return "sanitized-match", f"all {n} files match ({len(mapping)} after sanitization)"

    # Also check if only_top/only_label are just ignorable leftovers
    # that survived the initial filter (shouldn't happen, but defensive)

    if not only_top and only_label:
        return "superset-label", f"label has {len(only_label)} extra files"
    if only_top and not only_label:
        return "superset-top", f"top has {len(only_top)} extra files"

    if len(top_clean) == len(label_clean) and len(common) >= len(top_clean) * 0.7:
        return "filename-drift", f"{len(common)}/{len(top_clean)} files match"

    ratio = max(len(top_clean), len(label_clean)) / max(1, min(len(top_clean), len(label_clean)))
    if ratio > 2.0:
        return "different-content", f"top: {len(top_clean)} files, label: {len(label_clean)} files"

    if common:
        return "partial-overlap", f"{len(common)} shared, {len(only_top)} only-top, {len(only_label)} only-label"
    return "no-file-overlap", ""


def find_duplicates(media_dir):
    """Find duplicate directories that exist both at top level and in label subdirs."""
    media_path = Path(media_dir)

    top_level_dirs = set()
    for entry in os.scandir(media_dir):
        if entry.is_dir():
            top_level_dirs.add(entry.name)

    print(f"Found {len(top_level_dirs)} top-level directories")

    labels = find_label_dirs(media_dir)
    print(f"Found {len(labels)} potential label directories")

    duplicates = []
    for label_name, album_subdirs in sorted(labels.items()):
        for album_name in sorted(album_subdirs):
            if album_name in top_level_dirs:
                top_path = media_path / album_name
                label_path = media_path / label_name / album_name

                top_files = get_file_inventory(top_path)
                label_files = get_file_inventory(label_path)
                match, detail = classify_overlap(top_files, label_files)

                top_id = get_item_id(top_path)
                label_id = get_item_id(label_path)

                duplicates.append({
                    "album": album_name,
                    "label": label_name,
                    "top_path": str(top_path),
                    "label_path": str(label_path),
                    "match": match,
                    "detail": detail,
                    "top_files": top_files,
                    "label_files": label_files,
                    "top_file_count": len(top_files),
                    "label_file_count": len(label_files),
                    "top_size": sum(top_files.values()),
                    "label_size": sum(label_files.values()),
                    "top_id": top_id,
                    "label_id": label_id,
                })

    return duplicates


SAFE_TYPES = {"exact", "same-files-diff-sizes", "filename-drift", "sanitized-match", "empty-top"}


def print_report(duplicates):
    """Print human-readable duplicate report."""
    if not duplicates:
        print("\nNo duplicates found.")
        return

    by_match = defaultdict(list)
    for d in duplicates:
        by_match[d["match"]].append(d)

    total_recoverable = sum(
        min(d["top_size"], d["label_size"])
        for d in duplicates if d["match"] in SAFE_TYPES
    )

    print(f"\n{'='*60}")
    print("DUPLICATE DIRECTORY REPORT")
    print(f"{'='*60}")
    print(f"Total duplicates found: {len(duplicates)}")
    print(f"Space recoverable (safe matches): {total_recoverable / (1024**3):.1f} GB")
    print()
    for match_type, items in sorted(by_match.items()):
        print(f"  {match_type}: {len(items)}")

    order = ["exact", "same-files-diff-sizes", "filename-drift",
             "sanitized-match", "empty-top", "empty-label",
             "superset-label", "superset-top", "partial-overlap",
             "different-content", "both-empty", "no-file-overlap"]
    for match_type in order:
        items = by_match.get(match_type, [])
        if not items:
            continue
        print(f"\n{'-'*60}")
        print(f"Match type: {match_type} ({len(items)} items)")
        print(f"{'-'*60}")
        for d in items:
            detail = f" -- {d['detail']}" if d['detail'] else ""
            print(f"\n  Album: {d['album']}")
            print(f"  Label: {d['label']}{detail}")
            print(f"  Top-level:  {d['top_file_count']} files, {d['top_size']:,} bytes (id: {d['top_id']})")
            print(f"  Label dir:  {d['label_file_count']} files, {d['label_size']:,} bytes (id: {d['label_id']})")

    by_label = defaultdict(list)
    for d in duplicates:
        by_label[d["label"]].append(d)

    print(f"\n{'='*60}")
    print("DUPLICATES BY LABEL")
    print(f"{'='*60}")
    for label, items in sorted(by_label.items(), key=lambda x: -len(x[1])):
        label_total = sum(min(d["top_size"], d["label_size"]) for d in items if d["match"] in SAFE_TYPES)
        label_total_str = f" ({label_total / (1024**3):.1f} GB recoverable)" if label_total > 0 else ""
        print(f"\n  {label}: {len(items)} duplicates{label_total_str}")
        for d in items:
            print(f"    - {d['album']} [{d['match']}]")


def print_review(duplicates):
    """Print detailed file-by-file comparison for non-safe items."""
    non_safe = [d for d in duplicates if d["match"] not in SAFE_TYPES]
    if not non_safe:
        print("\nAll duplicates are safe matches - nothing to review.")
        return

    print(f"\n{'='*60}")
    print(f"DETAILED REVIEW: {len(non_safe)} non-safe items")
    print(f"{'='*60}")

    for d in non_safe:
        print(f"\n{'='*60}")
        print(f"Album: {d['album']}")
        print(f"Label: {d['label']}")
        print(f"Match: {d['match']} -- {d['detail']}")
        print(f"Top ID: {d['top_id']}  |  Label ID: {d['label_id']}")
        print()

        top_files = d["top_files"]
        label_files = d["label_files"]
        top_names = set(top_files.keys())
        label_names = set(label_files.keys())
        common = top_names & label_names
        only_top = sorted(top_names - label_names)
        only_label = sorted(label_names - top_names)

        if common:
            print("  SHARED FILES:")
            for name in sorted(common):
                ts = top_files[name]
                ls = label_files[name]
                marker = "" if ts == ls else f"  ** SIZE DIFF: {abs(ts - ls):,} bytes"
                print(f"    {name} ({ts:,} / {ls:,}){marker}")

        if only_top:
            print(f"\n  ONLY IN TOP-LEVEL ({len(only_top)}):")
            for name in only_top:
                print(f"    {name} ({top_files[name]:,})")

        if only_label:
            print(f"\n  ONLY IN LABEL DIR ({len(only_label)}):")
            for name in only_label:
                print(f"    {name} ({label_files[name]:,})")


def resolve_duplicates(duplicates, execute=False):
    """Resolve safe duplicates by copying tracking to label dir and removing top-level.

    For safe matches (exact, same-files-diff-sizes, filename-drift):
      - Copy bandcamp_item_id.txt from top-level to label dir
      - Delete top-level directory

    For superset-label:
      - Copy bandcamp_item_id.txt from top-level to label dir
      - Delete top-level directory (label has more content)

    For superset-top:
      - Skip (top has more content, needs manual review)
    """
    SKIP_ALBUMS = {
        "VAAV Social Club - The Contras of Falling in Love",
        "The Frixion - To Hell and Back",
    }
    safe = [d for d in duplicates if d["match"] in SAFE_TYPES and d["album"] not in SKIP_ALBUMS]
    superset_label = [d for d in duplicates if d["match"] == "superset-label" and d["album"] not in SKIP_ALBUMS]
    resolvable = safe + superset_label

    if not resolvable:
        print("\nNo resolvable duplicates found.")
        return

    mode = "EXECUTING" if execute else "DRY RUN"
    print(f"\n{'='*60}")
    print(f"DUPLICATE RESOLUTION ({mode})")
    print(f"{'='*60}")
    print(f"Resolvable items: {len(resolvable)}")

    total_freed = 0
    resolved = 0
    errors = 0

    for d in resolvable:
        top_path = Path(d["top_path"])
        label_path = Path(d["label_path"])
        item_id = d["top_id"]

        label_id = d["label_id"]
        action_needed = []

        if item_id and label_id is None:
            action_needed.append(f"Write {ITEM_INDEX_FILENAME} (id:{item_id}) to {label_path}")
        elif item_id and label_id and label_id != item_id:
            print(f"\n  SKIP (ID mismatch): {d['album']}")
            print(f"    Top ID: {item_id}, Label ID: {label_id}")
            continue

        action_needed.append(f"Delete {top_path}")
        freed = d["top_size"]
        total_freed += freed

        print(f"\n  [{d['match']}] {d['album']}")
        print(f"    Label: {d['label']}")
        for action in action_needed:
            print(f"    -> {action}")
        print(f"    Space freed: {freed / (1024**2):.0f} MB")

        if execute:
            try:
                # Write tracking file to label dir
                if label_id is None:
                    id_file = label_path / ITEM_INDEX_FILENAME
                    id_file.write_text(f"{item_id}\n")
                    print(f"    [OK] Wrote {id_file}")

                # Delete top-level directory
                shutil.rmtree(str(top_path))
                print(f"    [OK] Deleted {top_path}")
                resolved += 1
            except OSError as e:
                print(f"    [ERROR] {e}")
                errors += 1

    print(f"\n{'-'*60}")
    print(f"Total space to free: {total_freed / (1024**3):.1f} GB")
    if execute:
        print(f"Resolved: {resolved}, Errors: {errors}")
    else:
        print("Run with --execute to apply changes.")


if __name__ == "__main__":
    media_dir = r"N:\Bandcamp (FLAC)"
    do_review = False
    do_resolve = False
    do_execute = False

    args = sys.argv[1:]
    # First non-flag arg is media_dir
    positional = [a for a in args if not a.startswith("--")]
    if positional:
        media_dir = positional[0]

    do_review = "--review" in args
    do_resolve = "--resolve" in args
    do_execute = "--execute" in args

    duplicates = find_duplicates(media_dir)
    print_report(duplicates)

    if do_review:
        print_review(duplicates)

    if do_resolve:
        resolve_duplicates(duplicates, execute=do_execute)
