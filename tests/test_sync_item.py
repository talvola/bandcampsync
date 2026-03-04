"""Tests for Syncer's sync_item functionality and retry logic."""

from unittest.mock import Mock, patch, MagicMock
from pathlib import Path
import pytest
from bandcampsync.sync import Syncer
from bandcampsync.bandcamp import BandcampError
from bandcampsync.download import DownloadBadStatusCode, DownloadInvalidContentType


@pytest.fixture
def mock_bandcamp():
    with patch("bandcampsync.sync.Bandcamp") as mock_class:
        mock_instance = Mock()
        mock_instance.purchases = []
        mock_instance.verify_authentication.return_value = True
        mock_instance.load_purchases.return_value = True
        mock_class.return_value = mock_instance
        yield mock_instance


def _make_syncer(mock_bandcamp, tmp_path, **kwargs):
    defaults = dict(
        cookies="identity=test",
        dir_path=tmp_path,
        media_format="flac",
        temp_dir_root=str(tmp_path),
        ign_file_path=None,
        ign_patterns="",
        notify_url=None,
        max_retries=2,
        retry_wait=0,
    )
    defaults.update(kwargs)
    with patch("bandcampsync.sync.asyncio.run") as mock_run:
        s = Syncer(**defaults)
        if mock_run.called:
            coro = mock_run.call_args[0][0]
            coro.close()
    return s


@pytest.fixture
def syncer(mock_bandcamp, tmp_path):
    return _make_syncer(mock_bandcamp, tmp_path)


@pytest.fixture
def syncer_zip(mock_bandcamp, tmp_path):
    return _make_syncer(mock_bandcamp, tmp_path, dir_format="zip")


def test_sync_item_success(syncer, mock_bandcamp, tmp_path):
    item = Mock(
        is_preorder=False,
        band_name="Artist",
        item_title="Album",
        item_id=1,
        item_type="album",
        folder_suffix="",
        download_url="http://example.com/download",
    )

    mock_bandcamp.get_download_file_url.return_value = "http://example.com/file"
    mock_bandcamp.check_download_stat.return_value = "http://example.com/file_ok"

    with (
        patch("bandcampsync.sync.download_file") as mock_download,
        patch("bandcampsync.sync.is_zip_file", return_value=True),
        patch("bandcampsync.sync.unzip_file") as mock_unzip,
        patch("bandcampsync.sync.TemporaryDirectory") as mock_temp_dir,
    ):
        # Setup temp directory mock
        temp_dir_path = tmp_path / "temp_extract"
        temp_dir_path.mkdir()
        mock_temp_dir.return_value.__enter__.return_value = str(temp_dir_path)

        # Create a fake file in the temp directory
        (temp_dir_path / "track1.flac").write_text("audio data")

        with patch("bandcampsync.sync.move_file") as mock_move:
            result = syncer.sync_item(item)

            assert result is True
            assert syncer.new_items_downloaded is True
            mock_download.assert_called_once()
            mock_unzip.assert_called_once()
            mock_move.assert_called_once()


def test_sync_item_retries_and_succeeds(syncer, mock_bandcamp, tmp_path):
    item = Mock(
        is_preorder=False,
        band_name="Artist",
        item_title="Album",
        item_id=1,
        item_type="album",
        folder_suffix="",
        download_url="http://example.com/download",
    )

    # Fail once, then succeed
    mock_bandcamp.get_download_file_url.side_effect = [
        BandcampError("first fail"),
        "http://example.com/file",
    ]
    mock_bandcamp.check_download_stat.return_value = "http://example.com/file_ok"

    with (
        patch("bandcampsync.sync.download_file"),
        patch("bandcampsync.sync.is_zip_file", return_value=True),
        patch("bandcampsync.sync.unzip_file"),
        patch("bandcampsync.sync.TemporaryDirectory") as mock_temp_dir,
        patch("bandcampsync.sync.time.sleep") as mock_sleep,
    ):
        temp_dir_path = tmp_path / "temp_extract"
        temp_dir_path.mkdir()
        mock_temp_dir.return_value.__enter__.return_value = str(temp_dir_path)
        (temp_dir_path / "track1.flac").write_text("audio data")

        with patch("bandcampsync.sync.move_file"):
            result = syncer.sync_item(item)

            assert result is True
            assert mock_bandcamp.get_download_file_url.call_count == 2
            mock_sleep.assert_called_once_with(syncer.retry_wait)


def test_sync_item_fails_after_max_retries(syncer, mock_bandcamp):
    item = Mock(
        is_preorder=False,
        band_name="Artist",
        item_title="Album",
        item_id=1,
        item_type="album",
        folder_suffix="",
        download_url="http://example.com/download",
    )

    # Always fail
    mock_bandcamp.get_download_file_url.side_effect = BandcampError("persistent fail")

    with patch("bandcampsync.sync.time.sleep") as mock_sleep:
        result = syncer.sync_item(item)

        assert result is False
        assert mock_bandcamp.get_download_file_url.call_count == syncer.max_retries
        assert mock_sleep.call_count == syncer.max_retries - 1


def test_sync_item_track_success(syncer, mock_bandcamp, tmp_path):
    item = Mock(
        is_preorder=False,
        band_name="Artist",
        item_title="TrackTitle",
        item_id=1,
        item_type="track",
        folder_suffix="",
        url_hints={"slug": "track-slug"},
        download_url="http://example.com/download",
    )

    mock_bandcamp.get_download_file_url.return_value = "http://example.com/file"
    mock_bandcamp.check_download_stat.return_value = "http://example.com/file_ok"

    with (
        patch("bandcampsync.sync.download_file"),
        patch("bandcampsync.sync.is_zip_file", return_value=False),
        patch("bandcampsync.sync.copy_file") as mock_copy,
    ):
        result = syncer.sync_item(item)

        assert result is True
        assert syncer.new_items_downloaded is True
        # Check if copy_file was called with expected destination name (using slug)
        args, _ = mock_copy.call_args
        assert "track-slug.flac" in str(args[1])


# --- Zip format sync_item tests ---


def test_sync_item_zip_label_detection(syncer_zip, mock_bandcamp, tmp_path):
    """In zip mode, label release creates Label/Artist - Album dir."""
    item = Mock(
        is_preorder=False,
        band_name="CoolLabel",
        item_title="MyAlbum",
        item_id=42,
        item_type="album",
        folder_suffix="",
        download_url="http://example.com/download",
    )

    mock_bandcamp.get_download_file_url.return_value = "http://example.com/file"
    mock_bandcamp.check_download_stat.return_value = "http://example.com/file_ok"

    with (
        patch("bandcampsync.sync.download_file", return_value="ActualArtist - MyAlbum.zip"),
        patch("bandcampsync.sync.is_zip_file", return_value=True),
        patch("bandcampsync.sync.unzip_file"),
        patch("bandcampsync.sync.TemporaryDirectory") as mock_temp_dir,
    ):
        temp_dir_path = tmp_path / "temp_extract"
        temp_dir_path.mkdir()
        mock_temp_dir.return_value.__enter__.return_value = str(temp_dir_path)
        (temp_dir_path / "track1.flac").write_text("audio data")

        with patch("bandcampsync.sync.move_file"):
            result = syncer_zip.sync_item(item)

            assert result is True
            # Verify the directory was created under Label/Artist - Album
            expected_dir = tmp_path / "CoolLabel" / "ActualArtist - MyAlbum"
            assert expected_dir.is_dir()


def test_sync_item_zip_flat(syncer_zip, mock_bandcamp, tmp_path):
    """In zip mode, non-label release creates flat Artist - Album dir."""
    item = Mock(
        is_preorder=False,
        band_name="MyBand",
        item_title="MyAlbum",
        item_id=42,
        item_type="album",
        folder_suffix="",
        download_url="http://example.com/download",
    )

    mock_bandcamp.get_download_file_url.return_value = "http://example.com/file"
    mock_bandcamp.check_download_stat.return_value = "http://example.com/file_ok"

    with (
        patch("bandcampsync.sync.download_file", return_value="MyBand - MyAlbum.zip"),
        patch("bandcampsync.sync.is_zip_file", return_value=True),
        patch("bandcampsync.sync.unzip_file"),
        patch("bandcampsync.sync.TemporaryDirectory") as mock_temp_dir,
    ):
        temp_dir_path = tmp_path / "temp_extract"
        temp_dir_path.mkdir()
        mock_temp_dir.return_value.__enter__.return_value = str(temp_dir_path)
        (temp_dir_path / "track1.flac").write_text("audio data")

        with patch("bandcampsync.sync.move_file"):
            result = syncer_zip.sync_item(item)

            assert result is True
            expected_dir = tmp_path / "MyBand - MyAlbum"
            assert expected_dir.is_dir()


def test_sync_item_zip_existing_dir_skip(syncer_zip, mock_bandcamp, tmp_path):
    """In zip mode, existing directory is detected and extraction is skipped."""
    # Pre-create the target directory with a file
    existing_dir = tmp_path / "MyBand - MyAlbum"
    existing_dir.mkdir()
    (existing_dir / "track1.flac").write_text("existing audio")

    item = Mock(
        is_preorder=False,
        band_name="MyBand",
        item_title="MyAlbum",
        item_id=42,
        item_type="album",
        folder_suffix="",
        download_url="http://example.com/download",
    )

    mock_bandcamp.get_download_file_url.return_value = "http://example.com/file"
    mock_bandcamp.check_download_stat.return_value = "http://example.com/file_ok"

    with (
        patch("bandcampsync.sync.download_file", return_value="MyBand - MyAlbum.zip"),
        patch("bandcampsync.sync.is_zip_file", return_value=True),
        patch("bandcampsync.sync.unzip_file") as mock_unzip,
    ):
        result = syncer_zip.sync_item(item)

        # Should NOT extract (returns False since no new media was truly downloaded)
        assert result is False
        mock_unzip.assert_not_called()
        # But should write tracking file
        id_file = existing_dir / "bandcamp_item_id.txt"
        assert id_file.is_file()
        assert id_file.read_text().strip() == "42"


def test_sync_item_zip_track(syncer_zip, mock_bandcamp, tmp_path):
    """In zip mode, single track uses Artist - TrackName dir."""
    item = Mock(
        is_preorder=False,
        band_name="Artist",
        item_title="TrackTitle",
        item_id=55,
        item_type="track",
        folder_suffix="",
        url_hints={"slug": "track-slug"},
        download_url="http://example.com/download",
    )

    mock_bandcamp.get_download_file_url.return_value = "http://example.com/file"
    mock_bandcamp.check_download_stat.return_value = "http://example.com/file_ok"

    with (
        patch("bandcampsync.sync.download_file", return_value=None),
        patch("bandcampsync.sync.is_zip_file", return_value=False),
        patch("bandcampsync.sync.copy_file") as mock_copy,
    ):
        result = syncer_zip.sync_item(item)

        assert result is True
        args, _ = mock_copy.call_args
        assert "Artist - TrackTitle" in str(args[1])
        assert "track-slug.flac" in str(args[1])


def test_sync_item_zip_fallback_no_content_disposition(syncer_zip, mock_bandcamp, tmp_path):
    """In zip mode, missing Content-Disposition falls back to metadata-based name."""
    item = Mock(
        is_preorder=False,
        band_name="Artist",
        item_title="Album",
        item_id=77,
        item_type="album",
        folder_suffix="",
        download_url="http://example.com/download",
    )

    mock_bandcamp.get_download_file_url.return_value = "http://example.com/file"
    mock_bandcamp.check_download_stat.return_value = "http://example.com/file_ok"

    with (
        patch("bandcampsync.sync.download_file", return_value=None),
        patch("bandcampsync.sync.is_zip_file", return_value=True),
        patch("bandcampsync.sync.unzip_file"),
        patch("bandcampsync.sync.TemporaryDirectory") as mock_temp_dir,
    ):
        temp_dir_path = tmp_path / "temp_extract"
        temp_dir_path.mkdir()
        mock_temp_dir.return_value.__enter__.return_value = str(temp_dir_path)
        (temp_dir_path / "track1.flac").write_text("audio data")

        with patch("bandcampsync.sync.move_file"):
            result = syncer_zip.sync_item(item)

            assert result is True
            expected_dir = tmp_path / "Artist - Album"
            assert expected_dir.is_dir()
