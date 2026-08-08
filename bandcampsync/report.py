import csv
import sys

from .bandcamp import Bandcamp
from .ignores import Ignores
from .media import LocalMedia
from .logger import get_logger


log = get_logger("report")


def classify_item(item, local_media, ignores, dir_format):
    """Classify a Bandcamp purchase as ignored, preorder, downloaded, or missing.

    Returns (status, local_path) where status is one of
    "ignored", "preorder", "downloaded", "missing".
    local_path is the expected local directory (may be None for ignored/preorder).
    """
    if ignores.is_ignored(item):
        return ("ignored", None)

    if item.is_preorder:
        return ("preorder", None)

    if dir_format == "artist-album":
        local_path = local_media.get_path_for_purchase(item)
        if local_media.is_locally_downloaded(item, local_path):
            return ("downloaded", local_path)
    else:
        # zip format: check by ID first, then fall back to name matching
        if local_media.is_locally_downloaded_by_id(item):
            local_path = local_media.media.get(item.item_id)
            return ("downloaded", local_path)
        expected_name = local_media.get_expected_name_for_zip(item)
        if expected_name in local_media.item_names:
            return ("downloaded", None)
        # Fallback: title-suffix match (catches label releases where the
        # on-disk artist differs from band_name)
        match = local_media.find_zip_item_by_title(item)
        if match:
            return ("downloaded", None)

    return ("missing", None)


# Extensions counted as tracks. Bandcamp serves one of these per track depending on the
# requested format; a directory also holds cover art and occasionally a PDF or text file,
# which must not count.
AUDIO_EXTENSIONS = {
    ".flac",
    ".mp3",
    ".m4a",
    ".aac",
    ".ogg",
    ".oga",
    ".opus",
    ".wav",
    ".aif",
    ".aiff",
    ".alac",
    ".wma",
}


def remote_track_count(item):
    """Tracks bandcamp reports for a purchase, or None when the payload does not say.

    num_streamable_tracks rides along in the collection payload load_purchases() already
    fetches, so reading it costs no extra request. It counts *streamable* tracks, which is
    not always the same as downloadable ones - bonus and hidden tracks can appear in a
    download but not the stream, and vice versa. Good enough to nominate candidates, not
    good enough to act on unattended.
    """
    value = item._data.get("num_streamable_tracks")
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def count_local_tracks(path):
    """Count audio files in an album directory.

    Descends one level so multi-disc releases, which extract into per-disc
    subdirectories, are not reported as almost entirely missing.
    """
    count = 0
    try:
        for child in path.iterdir():
            if child.is_file():
                if child.suffix.lower() in AUDIO_EXTENSIONS:
                    count += 1
            elif child.is_dir():
                try:
                    count += sum(
                        1
                        for f in child.iterdir()
                        if f.is_file() and f.suffix.lower() in AUDIO_EXTENSIONS
                    )
                except OSError:
                    continue
    except OSError as e:
        log.warning(f"Could not read {path}: {e}")
        return None
    return count


def resolve_local_path(item, local_media, dir_format):
    """Find the directory holding an item, by ID where possible and name otherwise.

    Returns (path, matched_by) with matched_by one of "id", "name", or None. The
    distinction matters: an id match is exact, a name match is the same lenient
    matching that classify_item uses and can land on the wrong directory.
    """
    if item.item_id in local_media.media:
        return local_media.media[item.item_id], "id"

    if dir_format == "artist-album":
        expected = local_media.get_path_for_purchase(item)
        key = (expected.parent.name, expected.name)
        if key in local_media.item_paths:
            return local_media.item_paths[key], "name"
        return None, None

    expected_name = local_media.get_expected_name_for_zip(item)
    if expected_name in local_media.item_paths:
        return local_media.item_paths[expected_name], "name"
    match = local_media.find_zip_item_by_title(item)
    if match and match in local_media.item_paths:
        return local_media.item_paths[match], "name"
    return None, None


def check_growth(results, local_media, dir_format):
    """Find downloaded albums holding fewer audio files than bandcamp reports tracks.

    These are albums a label keeps adding to after release - running yearly collections,
    monthly tribute compilations - which the sync can never revisit, because dedup asks
    only whether the directory exists and the item_id does not change when tracks are
    added.

    Returns a list of dicts, most-underfilled first. Read-only.
    """
    grown = []
    unresolved = 0
    for item, status, _path in results:
        if status != "downloaded":
            continue
        remote = remote_track_count(item)
        if not remote:
            continue
        path, matched_by = resolve_local_path(item, local_media, dir_format)
        if path is None:
            unresolved += 1
            continue
        local = count_local_tracks(path)
        if local is None or local >= remote:
            continue
        grown.append(
            {
                "item": item,
                "path": path,
                "remote": remote,
                "local": local,
                "missing": remote - local,
                "matched_by": matched_by,
                "has_id_file": (path / local_media.ITEM_INDEX_FILENAME).is_file(),
                "ambiguous": path.name in local_media.ambiguous_names,
            }
        )
    grown.sort(key=lambda r: r["missing"], reverse=True)
    return grown, unresolved


def print_growth_report(grown, unresolved, output=None):
    """Print the growth audit. Candidates only - see remote_track_count on why."""
    lines = ["", "Possibly grown since download", "=" * 30]
    if not grown:
        lines.append("No albums hold fewer audio files than bandcamp reports tracks.")
    else:
        lines.append(
            f"{len(grown)} album(s) hold fewer audio files than bandcamp reports "
            f"tracks. Track counts come from num_streamable_tracks, which does not"
        )
        lines.append(
            "always match what a download contains, so confirm before re-fetching."
        )
        lines.append("")
        for row in grown:
            item = row["item"]
            flags = []
            if row["matched_by"] == "name":
                flags.append("matched by name")
            if not row["has_id_file"]:
                flags.append("no id file")
            if row["ambiguous"]:
                flags.append("AMBIGUOUS: name appears twice on disk")
            suffix = f"  [{'; '.join(flags)}]" if flags else ""
            lines.append(
                f"  - {item.band_name} / {item.item_title} (id:{item.item_id})"
            )
            lines.append(
                f"      {row['local']} local / {row['remote']} remote "
                f"(+{row['missing']}){suffix}"
            )
            lines.append(f"      {row['path']}")
    if unresolved:
        lines.append("")
        lines.append(
            f"{unresolved} downloaded item(s) could not be matched to a directory "
            f"and were not checked."
        )

    text = "\n".join(lines)
    if output:
        output.write(text + "\n")
    else:
        _safe_print(text)


def _safe_print(text):
    """Print text, replacing unencodable characters on Windows."""
    try:
        print(text)
    except UnicodeEncodeError:
        print(
            text.encode(sys.stdout.encoding or "utf-8", errors="replace").decode(
                sys.stdout.encoding or "utf-8", errors="replace"
            )
        )


def print_report(results, output=None):
    """Print a human-readable summary to stdout (or the given file object)."""
    total = len(results)
    counts = {"downloaded": 0, "missing": 0, "ignored": 0, "preorder": 0}
    missing_items = []
    for item, status, _path in results:
        counts[status] += 1
        if status == "missing":
            missing_items.append(item)

    lines = [
        "BandcampSync Collection Report",
        "=" * 30,
        f"Total purchases: {total}",
        f"  Downloaded:    {counts['downloaded']}",
        f"  Missing:       {counts['missing']}",
        f"  Ignored:       {counts['ignored']}",
        f"  Preorders:     {counts['preorder']}",
    ]

    if missing_items:
        lines.append("")
        lines.append("Missing items:")
        for item in missing_items:
            lines.append(
                f"  - {item.band_name} / {item.item_title} (id:{item.item_id})"
            )

    text = "\n".join(lines)
    if output:
        output.write(text + "\n")
    else:
        _safe_print(text)


def write_csv(results, csv_path):
    """Write a CSV report with one row per purchase."""
    with open(csv_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(
            ["item_id", "band_name", "item_title", "item_type", "status", "local_path"]
        )
        for item, status, local_path in results:
            item_type = item._data.get("item_type", "")
            writer.writerow(
                [
                    item.item_id,
                    item.band_name,
                    item.item_title,
                    item_type,
                    status,
                    str(local_path) if local_path else "",
                ]
            )


def generate_report(
    cookies,
    media_dir,
    ign_file_path=None,
    ign_patterns="",
    skip_item_index=False,
    dir_format="artist-album",
    csv_path=None,
    growth=False,
):
    """Generate a collection report comparing Bandcamp purchases to local files.

    This is read-only: it never modifies the ignore file or writes tracking files.
    """
    bandcamp = Bandcamp(cookies=cookies)
    bandcamp.verify_authentication()
    bandcamp.load_purchases()

    ignores = Ignores(ign_file_path=ign_file_path, ign_patterns=ign_patterns)
    local_media = LocalMedia(
        media_dir=media_dir,
        ignores=ignores,
        skip_item_index=skip_item_index,
        sync_ignore_file=False,
        dir_format=dir_format,
    )

    results = []
    for item in bandcamp.purchases:
        status, local_path = classify_item(item, local_media, ignores, dir_format)
        results.append((item, status, local_path))

    print_report(results)

    if growth:
        if skip_item_index and not local_media.item_paths:
            log.warning(
                "--check-growth needs the local index; it cannot run alongside "
                "--skip-item-index with a populated ignore file"
            )
        else:
            grown, unresolved = check_growth(results, local_media, dir_format)
            print_growth_report(grown, unresolved)

    if csv_path:
        write_csv(results, csv_path)
        log.info(f"CSV report written to {csv_path}")

    return results
