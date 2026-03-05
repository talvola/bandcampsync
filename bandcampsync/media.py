from pathlib import PurePosixPath
from unicodedata import normalize
from bandcampsync.bandcamp import BandcampItem
from .logger import get_logger


log = get_logger("media")


def parse_zip_filename(filename):
    """Split 'Artist - Album.zip' into (artist, album).

    Returns (None, None) on failure.
    """
    if not filename:
        return (None, None)
    # Strip .zip extension
    stem = filename
    if stem.lower().endswith(".zip"):
        stem = stem[:-4]
    parts = stem.split(" - ", 1)
    if len(parts) != 2 or not parts[0].strip() or not parts[1].strip():
        return (None, None)
    return (parts[0].strip(), parts[1].strip())


class LocalMedia:
    """
    A local media directory indexer. This stores media in the following format:

        /media_dir/
        /media_dir/Artist Name
        /media_dir/Artist Name/Album Name
        /media_dir/Artist Name/Album Name/bandcamp_item_id.txt
        /media_dir/Artist Name/Album Name/track1.flac
        /media_dir/Artist Name/Album Name/track2.flac
    """

    ITEM_INDEX_FILENAME = "bandcamp_item_id.txt"

    def __init__(self, media_dir, ignores, skip_item_index, sync_ignore_file, dir_format="artist-album"):
        self.media_dir = media_dir
        self.ignores = ignores
        self.media = {}
        self.item_names = set()
        self.sync_ignore_file = sync_ignore_file
        self.dir_format = dir_format
        log.info(f"Local media directory: {self.media_dir}")

        # If the ignores file is empty, we need to traverse the filesystem anyway
        if not skip_item_index or len(self.ignores.ids) < 1:
            self.index()

    def _clean_path(self, path_str):
        path_str = str(path_str)
        disallowed_punctuation = "\"#%'*/?\\`:"
        normalized_path = normalize("NFKD", path_str)
        outstr = ""
        for c in normalized_path:
            if c not in disallowed_punctuation:
                outstr += c
        # Windows silently strips trailing dots and spaces from directory names
        return outstr.rstrip(". ")

    def clean_format(self, format_str):
        if "-" not in format_str:
            return format_str
        format_parts = format_str.split("-")
        format_prefix = format_parts[0]
        return format_prefix if format_prefix else format_str

    def index(self):
        if self.dir_format == "zip":
            return self._index_zip_format()
        return self._index_artist_album_format()

    def _index_artist_album_format(self):
        for child1 in self.media_dir.iterdir():
            if child1.is_dir():
                for child2 in child1.iterdir():
                    if child2.is_dir():
                        for child3 in child2.iterdir():
                            if child3.name == self.ITEM_INDEX_FILENAME:
                                item_id = self.read_item_id(child3)
                                if item_id is None:
                                    continue
                                if self.sync_ignore_file:
                                    item = BandcampItem(
                                        {
                                            "item_id": item_id,
                                            "band_name": child2.parent.name,
                                            "item_title": child2.name,
                                        }
                                    )
                                    if not self.ignores.is_ignored(item):
                                        self.ignores.add(item)
                                self.media[item_id] = child2
                                self.item_names.add((child2.parent.name, child2.name))
                                log.info(
                                    f"Detected locally downloaded media: {item_id} = {child2}"
                                )
        return True

    def _index_zip_format(self):
        """Index directories in zip format: Artist - Album at depth 1, or Label/Artist - Album at depth 2."""
        for child1 in self.media_dir.iterdir():
            if not child1.is_dir():
                continue
            # Check if child1 itself is an album dir (depth 1: media_dir/Artist - Album/)
            id_file = child1 / self.ITEM_INDEX_FILENAME
            if id_file.is_file():
                item_id = self.read_item_id(id_file)
                if item_id is not None:
                    self.media[item_id] = child1
                    log.info(f"Detected locally downloaded media: {item_id} = {child1}")
            # Always add to item_names for name-based matching
            self.item_names.add(child1.name)
            # Check depth 2: media_dir/Label/Artist - Album/
            for child2 in child1.iterdir():
                if child2.is_dir():
                    self.item_names.add(child2.name)
                    id_file2 = child2 / self.ITEM_INDEX_FILENAME
                    if id_file2.is_file():
                        item_id = self.read_item_id(id_file2)
                        if item_id is not None:
                            self.media[item_id] = child2
                            log.info(f"Detected locally downloaded media: {item_id} = {child2}")
        return True

    def read_item_id(self, filepath):
        with open(filepath, "rt") as f:
            item_id = f.read().strip()
        try:
            return int(item_id)
        except (ValueError, TypeError):
            log.warning(
                f'Invalid item ID in {filepath}: "{item_id}", skipping'
            )
            return None

    def is_locally_downloaded(self, item, local_path):
        if item.item_id in self.media:
            return True
        item_name = (local_path.parent.name, local_path.name)
        if item_name in self.item_names:
            log.info(
                f'Detected album at "{local_path}" but with an item ID mismatch '
                f"({self.ITEM_INDEX_FILENAME} file does not contain {item.item_id}), "
                f"you may want to check this item is correctly downloaded"
            )
            return True
        return False

    def is_locally_downloaded_by_id(self, item):
        """Check by item_id, with name-based fallback (used in zip format before download)."""
        if item.item_id in self.media:
            return True
        # Fallback: check if a directory with the expected name already exists
        # (handles label-disassociated albums that lack tracking files)
        expected_name = self.get_expected_name_for_zip(item)
        if expected_name in self.item_names:
            log.info(
                f'Detected album by name "{expected_name}" but without matching item ID '
                f"(id:{item.item_id}), skipping download"
            )
            return True
        return False

    def get_path_for_purchase(self, item):
        return (
            self.media_dir
            / self._clean_path(item.band_name)
            / self._clean_path(f"{item.item_title}{item.folder_suffix}")
        )

    def get_path_for_zip_purchase(self, item, content_filename):
        """Compute local path for a zip-format purchase.

        Uses the ZIP filename from Content-Disposition to determine the directory name.
        Compares the ZIP artist with band_name to detect label releases.
        """
        zip_artist, zip_album = parse_zip_filename(content_filename)
        if zip_artist and zip_album:
            zip_dirname = PurePosixPath(content_filename).stem
            # Compare artist to band_name to detect label grouping
            if zip_artist.lower() != item.band_name.lower():
                # Label release: Label/Artist - Album
                return self.media_dir / self._clean_path(item.band_name) / zip_dirname
            else:
                # Direct artist release: Artist - Album
                return self.media_dir / zip_dirname
        # Fallback: construct from metadata
        dirname = self._clean_path(f"{item.band_name} - {item.item_title}{item.folder_suffix}")
        return self.media_dir / dirname

    def get_path_for_track_purchase(self, item):
        """Compute local path for a single track in zip format."""
        dirname = self._clean_path(f"{item.band_name} - {item.item_title}{item.folder_suffix}")
        return self.media_dir / dirname

    def get_expected_name_for_zip(self, item):
        """Return the expected directory name for a zip-format purchase.

        Used for name-based fallback matching when no bandcamp_item_id.txt exists.
        """
        return self._clean_path(
            f"{item.band_name} - {item.item_title}{item.folder_suffix}"
        )

    @staticmethod
    def _normalize_for_match(s):
        """Reduce a string to lowercase alphanumeric + spaces for fuzzy comparison.

        Handles differences like colon→hyphen vs colon→removed that arise
        because Bandcamp ZIP filenames and _clean_path use different sanitization.
        """
        return "".join(c.lower() for c in s if c.isalnum() or c == " ").strip()

    def find_zip_item_by_title(self, item):
        """Try to find a locally downloaded item by matching the title portion.

        For label releases, the on-disk name is 'ActualArtist - Title' which
        won't match 'LabelName - Title'. This checks if any indexed directory
        has a title portion (after ' - ') that matches the item's title when
        both are normalized.

        Returns the matching directory name, or None.
        """
        norm_title = self._normalize_for_match(
            f"{item.item_title}{item.folder_suffix}"
        )
        for name in self.item_names:
            parts = name.split(" - ", 1)
            if len(parts) == 2:
                on_disk_title = self._normalize_for_match(parts[1])
                if on_disk_title == norm_title:
                    return name
        return None

    def find_zip_item_by_artist(self, item):
        """Try to find a locally downloaded item by matching the artist prefix.

        Handles cases where metadata drifted (e.g. label dropped from Bandcamp,
        title gained/lost a suffix like 'EP'). Checks if any indexed directory
        starts with 'band_name - ' and the on-disk title is a substring of the
        Bandcamp title or vice versa (using normalized comparison).

        Returns the matching directory name, or None.
        """
        norm_band = self._normalize_for_match(item.band_name)
        norm_title = self._normalize_for_match(
            f"{item.item_title}{item.folder_suffix}"
        )
        for name in self.item_names:
            parts = name.split(" - ", 1)
            if len(parts) != 2:
                continue
            on_disk_band = self._normalize_for_match(parts[0])
            if on_disk_band != norm_band:
                continue
            on_disk_title = self._normalize_for_match(parts[1])
            # Check if one title is a substring of the other
            if on_disk_title in norm_title or norm_title in on_disk_title:
                return name
        return None

    def get_path_for_file(self, local_path, file_name):
        return local_path / self._clean_path(file_name)

    def write_bandcamp_id(self, item, dirpath):
        outfile = dirpath / self.ITEM_INDEX_FILENAME
        log.info(f"Writing bandcamp item id:{item.item_id} to: {outfile}")
        with open(outfile, "wt") as f:
            f.write(f"{item.item_id}\n")
        return True
