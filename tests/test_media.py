"""Tests for LocalMedia zip format support."""

from unittest.mock import Mock
import pytest

from bandcampsync.media import parse_zip_filename, LocalMedia
from bandcampsync.ignores import Ignores


@pytest.fixture
def ignores(tmp_path):
    return Ignores(ign_file_path=None, ign_patterns="")


# --- parse_zip_filename ---


def test_parse_zip_filename_basic():
    assert parse_zip_filename("Artist - Album.zip") == ("Artist", "Album")


def test_parse_zip_filename_with_spaces():
    assert parse_zip_filename("Some Artist - Some Album Title.zip") == (
        "Some Artist",
        "Some Album Title",
    )


def test_parse_zip_filename_dash_in_album():
    assert parse_zip_filename("Artist - Album - Deluxe Edition.zip") == (
        "Artist",
        "Album - Deluxe Edition",
    )


def test_parse_zip_filename_no_zip_extension():
    assert parse_zip_filename("Artist - Album") == ("Artist", "Album")


def test_parse_zip_filename_no_separator():
    assert parse_zip_filename("JustAName.zip") == (None, None)


def test_parse_zip_filename_empty():
    assert parse_zip_filename("") == (None, None)
    assert parse_zip_filename(None) == (None, None)


def test_parse_zip_filename_empty_parts():
    assert parse_zip_filename(" - Album.zip") == (None, None)
    assert parse_zip_filename("Artist - .zip") == (None, None)


# --- _index_zip_format ---


def test_index_zip_format_flat(tmp_path, ignores):
    """Depth 1: media_dir/Artist - Album/"""
    album_dir = tmp_path / "Artist - Album"
    album_dir.mkdir()
    (album_dir / "track1.flac").write_text("audio")
    (album_dir / "bandcamp_item_id.txt").write_text("123\n")

    lm = LocalMedia(
        media_dir=tmp_path,
        ignores=ignores,
        skip_item_index=False,
        sync_ignore_file=False,
        dir_format="zip",
    )

    assert 123 in lm.media
    assert "Artist - Album" in lm.item_names


def test_index_zip_format_nested(tmp_path, ignores):
    """Depth 2: media_dir/Label/Artist - Album/"""
    label_dir = tmp_path / "Label"
    label_dir.mkdir()
    album_dir = label_dir / "Artist - Album"
    album_dir.mkdir()
    (album_dir / "track1.flac").write_text("audio")
    (album_dir / "bandcamp_item_id.txt").write_text("456\n")

    lm = LocalMedia(
        media_dir=tmp_path,
        ignores=ignores,
        skip_item_index=False,
        sync_ignore_file=False,
        dir_format="zip",
    )

    assert 456 in lm.media
    assert "Artist - Album" in lm.item_names
    assert "Label" in lm.item_names


def test_index_zip_format_no_id_file(tmp_path, ignores):
    """Directories without bandcamp_item_id.txt are still indexed by name."""
    album_dir = tmp_path / "Artist - Album"
    album_dir.mkdir()
    (album_dir / "track1.flac").write_text("audio")

    lm = LocalMedia(
        media_dir=tmp_path,
        ignores=ignores,
        skip_item_index=False,
        sync_ignore_file=False,
        dir_format="zip",
    )

    assert len(lm.media) == 0
    assert "Artist - Album" in lm.item_names


def test_index_zip_format_mixed(tmp_path, ignores):
    """Mix of flat and nested directories."""
    # Flat
    flat_dir = tmp_path / "Artist1 - Album1"
    flat_dir.mkdir()
    (flat_dir / "bandcamp_item_id.txt").write_text("100\n")
    # Nested
    label_dir = tmp_path / "Label"
    label_dir.mkdir()
    nested_dir = label_dir / "Artist2 - Album2"
    nested_dir.mkdir()
    (nested_dir / "bandcamp_item_id.txt").write_text("200\n")

    lm = LocalMedia(
        media_dir=tmp_path,
        ignores=ignores,
        skip_item_index=False,
        sync_ignore_file=False,
        dir_format="zip",
    )

    assert 100 in lm.media
    assert 200 in lm.media
    assert "Artist1 - Album1" in lm.item_names
    assert "Label" in lm.item_names
    assert "Artist2 - Album2" in lm.item_names


# --- is_locally_downloaded_by_id ---


def test_is_locally_downloaded_by_id(tmp_path, ignores):
    album_dir = tmp_path / "Artist - Album"
    album_dir.mkdir()
    (album_dir / "bandcamp_item_id.txt").write_text("999\n")

    lm = LocalMedia(
        media_dir=tmp_path,
        ignores=ignores,
        skip_item_index=False,
        sync_ignore_file=False,
        dir_format="zip",
    )

    item_found = Mock(item_id=999)
    item_missing = Mock(item_id=888)
    assert lm.is_locally_downloaded_by_id(item_found) is True
    assert lm.is_locally_downloaded_by_id(item_missing) is False


def test_is_locally_downloaded_by_id_name_fallback(tmp_path, ignores):
    """When no tracking file exists but directory name matches, return True."""
    # Create dir without bandcamp_item_id.txt (e.g. label-subdir album)
    label_dir = tmp_path / "OldLabel"
    label_dir.mkdir()
    album_dir = label_dir / "Artist - Album"
    album_dir.mkdir()

    lm = LocalMedia(
        media_dir=tmp_path,
        ignores=ignores,
        skip_item_index=False,
        sync_ignore_file=False,
        dir_format="zip",
    )

    # No tracking file, so item_id won't match, but name should
    item = Mock(item_id=12345, band_name="Artist", item_title="Album", folder_suffix="")
    assert lm.is_locally_downloaded_by_id(item) is True

    # Completely unknown album should still return False
    item_unknown = Mock(item_id=99999, band_name="Nobody", item_title="Nothing", folder_suffix="")
    assert lm.is_locally_downloaded_by_id(item_unknown) is False


# --- get_path_for_zip_purchase ---


def test_get_path_for_zip_purchase_flat(tmp_path, ignores):
    """Non-label release: artist matches band_name."""
    lm = LocalMedia(
        media_dir=tmp_path,
        ignores=ignores,
        skip_item_index=True,
        sync_ignore_file=False,
        dir_format="zip",
    )

    item = Mock(band_name="MyBand", item_title="MyAlbum", folder_suffix="", item_id=1)
    result = lm.get_path_for_zip_purchase(item, "MyBand - MyAlbum.zip")
    assert result == tmp_path / "MyBand - MyAlbum"


def test_get_path_for_zip_purchase_label(tmp_path, ignores):
    """Label release: artist differs from band_name."""
    lm = LocalMedia(
        media_dir=tmp_path,
        ignores=ignores,
        skip_item_index=True,
        sync_ignore_file=False,
        dir_format="zip",
    )

    item = Mock(band_name="CoolLabel", item_title="MyAlbum", folder_suffix="", item_id=1)
    result = lm.get_path_for_zip_purchase(item, "ActualArtist - MyAlbum.zip")
    assert result == tmp_path / "CoolLabel" / "ActualArtist - MyAlbum"


def test_get_path_for_zip_purchase_existing_band_dir_treated_as_label(tmp_path, ignores):
    """If a directory matching band_name already exists, treat it as a label dir
    even when the ZIP artist matches band_name (label compilation case)."""
    label_dir = tmp_path / "Start-track.com"
    label_dir.mkdir()

    lm = LocalMedia(
        media_dir=tmp_path,
        ignores=ignores,
        skip_item_index=True,
        sync_ignore_file=False,
        dir_format="zip",
    )

    item = Mock(band_name="Start-track.com", item_title="START THE TRACK: VOL. XI",
                folder_suffix="", item_id=1)
    result = lm.get_path_for_zip_purchase(item, "Start-track.com - START THE TRACK- VOL. XI.zip")
    assert result == label_dir / "Start-track.com - START THE TRACK- VOL. XI"


def test_get_path_for_zip_purchase_label_case_insensitive(tmp_path, ignores):
    """Case-insensitive comparison for label detection."""
    lm = LocalMedia(
        media_dir=tmp_path,
        ignores=ignores,
        skip_item_index=True,
        sync_ignore_file=False,
        dir_format="zip",
    )

    item = Mock(band_name="myband", item_title="Album", folder_suffix="", item_id=1)
    result = lm.get_path_for_zip_purchase(item, "MyBand - Album.zip")
    assert result == tmp_path / "MyBand - Album"


def test_get_path_for_zip_purchase_fallback(tmp_path, ignores):
    """Fallback when Content-Disposition filename is missing."""
    lm = LocalMedia(
        media_dir=tmp_path,
        ignores=ignores,
        skip_item_index=True,
        sync_ignore_file=False,
        dir_format="zip",
    )

    item = Mock(band_name="Artist", item_title="Album", folder_suffix="", item_id=1)
    result = lm.get_path_for_zip_purchase(item, None)
    assert result == tmp_path / "Artist - Album"


def test_get_path_for_zip_purchase_fallback_with_suffix(tmp_path, ignores):
    """Fallback uses folder_suffix."""
    lm = LocalMedia(
        media_dir=tmp_path,
        ignores=ignores,
        skip_item_index=True,
        sync_ignore_file=False,
        dir_format="zip",
    )

    item = Mock(band_name="Artist", item_title="Album", folder_suffix=" (2)", item_id=1)
    result = lm.get_path_for_zip_purchase(item, None)
    assert result == tmp_path / "Artist - Album (2)"


# --- get_path_for_track_purchase ---


def test_get_path_for_track_purchase(tmp_path, ignores):
    lm = LocalMedia(
        media_dir=tmp_path,
        ignores=ignores,
        skip_item_index=True,
        sync_ignore_file=False,
        dir_format="zip",
    )

    item = Mock(band_name="Artist", item_title="TrackName", folder_suffix="", item_id=1)
    result = lm.get_path_for_track_purchase(item)
    assert result == tmp_path / "Artist - TrackName"
