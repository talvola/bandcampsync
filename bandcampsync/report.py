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


def _safe_print(text):
    """Print text, replacing unencodable characters on Windows."""
    try:
        print(text)
    except UnicodeEncodeError:
        print(text.encode(sys.stdout.encoding or "utf-8", errors="replace").decode(
            sys.stdout.encoding or "utf-8", errors="replace"
        ))


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
            lines.append(f"  - {item.band_name} / {item.item_title} (id:{item.item_id})")

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
    if csv_path:
        write_csv(results, csv_path)
        log.info(f"CSV report written to {csv_path}")

    return results
