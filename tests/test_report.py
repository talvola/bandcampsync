"""Tests for collection report mode."""

import csv
from unittest.mock import patch
from pathlib import Path

import pytest

from bandcampsync.bandcamp import BandcampItem
from bandcampsync.ignores import Ignores
from bandcampsync.media import LocalMedia
from bandcampsync.report import (
    check_growth,
    classify_item,
    generate_report,
    print_growth_report,
    print_report,
    remote_track_count,
    write_csv,
)


def _make_item(
    item_id,
    band_name="Artist",
    item_title="Album",
    is_preorder=False,
    item_type="album",
    folder_suffix="",
):
    data = {
        "item_id": item_id,
        "band_name": band_name,
        "item_title": item_title,
        "is_preorder": is_preorder,
        "item_type": item_type,
        "folder_suffix": folder_suffix,
    }
    return BandcampItem(data)


@pytest.fixture
def ignores():
    return Ignores(ign_file_path=None, ign_patterns="")


# --- classify_item: artist-album format ---


def test_classify_missing_artist_album(tmp_path, ignores):
    lm = LocalMedia(
        media_dir=tmp_path,
        ignores=ignores,
        skip_item_index=False,
        sync_ignore_file=False,
        dir_format="artist-album",
    )
    item = _make_item(1, "NewArtist", "NewAlbum")
    status, path = classify_item(item, lm, ignores, "artist-album")
    assert status == "missing"


def test_classify_downloaded_artist_album(tmp_path, ignores):
    # Create the expected directory structure with tracking file
    artist_dir = tmp_path / "Artist"
    album_dir = artist_dir / "Album"
    album_dir.mkdir(parents=True)
    (album_dir / "bandcamp_item_id.txt").write_text("42\n")

    lm = LocalMedia(
        media_dir=tmp_path,
        ignores=ignores,
        skip_item_index=False,
        sync_ignore_file=False,
        dir_format="artist-album",
    )
    item = _make_item(42, "Artist", "Album")
    status, path = classify_item(item, lm, ignores, "artist-album")
    assert status == "downloaded"
    assert path == album_dir


def test_classify_preorder(tmp_path, ignores):
    lm = LocalMedia(
        media_dir=tmp_path,
        ignores=ignores,
        skip_item_index=False,
        sync_ignore_file=False,
        dir_format="artist-album",
    )
    item = _make_item(1, is_preorder=True)
    status, path = classify_item(item, lm, ignores, "artist-album")
    assert status == "preorder"
    assert path is None


def test_classify_ignored_by_pattern(tmp_path):
    ignores = Ignores(ign_file_path=None, ign_patterns="skipme")
    lm = LocalMedia(
        media_dir=tmp_path,
        ignores=ignores,
        skip_item_index=False,
        sync_ignore_file=False,
        dir_format="artist-album",
    )
    item = _make_item(1, band_name="SkipMe Records")
    status, path = classify_item(item, lm, ignores, "artist-album")
    assert status == "ignored"
    assert path is None


def test_classify_ignored_by_id(tmp_path):
    # Create ignore file with item ID
    ign_path = tmp_path / "ignores.txt"
    ign_path.write_text("99\n")
    ignores = Ignores(ign_file_path=ign_path, ign_patterns="")
    lm = LocalMedia(
        media_dir=tmp_path,
        ignores=ignores,
        skip_item_index=False,
        sync_ignore_file=False,
        dir_format="artist-album",
    )
    item = _make_item(99, "Artist", "Album")
    status, path = classify_item(item, lm, ignores, "artist-album")
    assert status == "ignored"


# --- classify_item: zip format ---


def test_classify_downloaded_zip_by_id(tmp_path, ignores):
    album_dir = tmp_path / "Artist - Album"
    album_dir.mkdir()
    (album_dir / "bandcamp_item_id.txt").write_text("42\n")

    lm = LocalMedia(
        media_dir=tmp_path,
        ignores=ignores,
        skip_item_index=False,
        sync_ignore_file=False,
        dir_format="zip",
    )
    item = _make_item(42, "Artist", "Album")
    status, path = classify_item(item, lm, ignores, "zip")
    assert status == "downloaded"
    assert path == album_dir


def test_classify_downloaded_zip_by_name(tmp_path, ignores):
    # Directory exists but has no tracking file
    album_dir = tmp_path / "Artist - Album"
    album_dir.mkdir()
    (album_dir / "track.flac").write_text("audio")

    lm = LocalMedia(
        media_dir=tmp_path,
        ignores=ignores,
        skip_item_index=False,
        sync_ignore_file=False,
        dir_format="zip",
    )
    item = _make_item(99, "Artist", "Album")
    status, path = classify_item(item, lm, ignores, "zip")
    assert status == "downloaded"


def test_classify_missing_zip(tmp_path, ignores):
    lm = LocalMedia(
        media_dir=tmp_path,
        ignores=ignores,
        skip_item_index=False,
        sync_ignore_file=False,
        dir_format="zip",
    )
    item = _make_item(1, "NewArtist", "NewAlbum")
    status, path = classify_item(item, lm, ignores, "zip")
    assert status == "missing"


def test_classify_downloaded_zip_label_release(tmp_path, ignores):
    """Label release: band_name is label, on-disk artist differs."""
    label_dir = tmp_path / "Side-Line Magazine"
    label_dir.mkdir()
    album_dir = label_dir / "Various Artists - Face The Beat- Session 5"
    album_dir.mkdir()

    lm = LocalMedia(
        media_dir=tmp_path,
        ignores=ignores,
        skip_item_index=False,
        sync_ignore_file=False,
        dir_format="zip",
    )
    # band_name is the label, item_title has colon which gets cleaned
    item = _make_item(99, "Side-Line Magazine", "Face The Beat: Session 5")
    status, path = classify_item(item, lm, ignores, "zip")
    assert status == "downloaded"


def test_classify_missing_zip_substring_title(tmp_path, ignores):
    """Same artist, title is a substring of an existing one — must NOT match.

    Guards against false positives like 'Live at the El Rey' matching
    'Live at the El Rey (20th Anniversary Edition)', or 'VOL. I' matching 'VOL. IX'.
    """
    album_dir = tmp_path / "Collide - Live at the El Rey"
    album_dir.mkdir()

    lm = LocalMedia(
        media_dir=tmp_path,
        ignores=ignores,
        skip_item_index=False,
        sync_ignore_file=False,
        dir_format="zip",
    )
    item = _make_item(99, "Collide", "Live at the El Rey (20th Anniversary Edition)")
    status, path = classify_item(item, lm, ignores, "zip")
    assert status == "missing"


# --- print_report ---


def test_print_report(capsys):
    items = [
        (_make_item(1, "A1", "T1"), "downloaded", Path("/music/A1/T1")),
        (_make_item(2, "A2", "T2"), "missing", None),
        (_make_item(3, "A3", "T3"), "ignored", None),
        (_make_item(4, "A4", "T4"), "preorder", None),
    ]
    print_report(items)
    out = capsys.readouterr().out
    assert "Total purchases: 4" in out
    assert "Downloaded:    1" in out
    assert "Missing:       1" in out
    assert "Ignored:       1" in out
    assert "Preorders:     1" in out
    assert "A2 / T2 (id:2)" in out


def test_print_report_no_missing(capsys):
    items = [
        (_make_item(1, "A1", "T1"), "downloaded", Path("/music/A1/T1")),
    ]
    print_report(items)
    out = capsys.readouterr().out
    assert "Missing items:" not in out


# --- write_csv ---


def test_write_csv(tmp_path):
    csv_path = tmp_path / "report.csv"
    items = [
        (
            _make_item(1, "A1", "T1", item_type="album"),
            "downloaded",
            Path("/music/A1/T1"),
        ),
        (_make_item(2, "A2", "T2", item_type="track"), "missing", None),
    ]
    write_csv(items, csv_path)

    with open(csv_path, newline="", encoding="utf-8") as f:
        reader = csv.reader(f)
        rows = list(reader)

    assert rows[0] == [
        "item_id",
        "band_name",
        "item_title",
        "item_type",
        "status",
        "local_path",
    ]
    assert rows[1][0] == "1"
    assert rows[1][1] == "A1"
    assert rows[1][4] == "downloaded"
    assert rows[2][0] == "2"
    assert rows[2][4] == "missing"
    assert rows[2][5] == ""


# --- generate_report (integration with mocks) ---


def test_generate_report(tmp_path, capsys):
    # Create a local album
    artist_dir = tmp_path / "Artist"
    album_dir = artist_dir / "Album"
    album_dir.mkdir(parents=True)
    (album_dir / "bandcamp_item_id.txt").write_text("1\n")

    purchases = [
        _make_item(1, "Artist", "Album"),
        _make_item(2, "NewArtist", "NewAlbum"),
    ]

    with patch("bandcampsync.report.Bandcamp") as MockBandcamp:
        mock_bc = MockBandcamp.return_value
        mock_bc.verify_authentication.return_value = True
        mock_bc.load_purchases.return_value = True
        mock_bc.purchases = purchases

        results = generate_report(
            cookies="identity=test",
            media_dir=tmp_path,
            dir_format="artist-album",
        )

    assert len(results) == 2
    assert results[0][1] == "downloaded"
    assert results[1][1] == "missing"

    out = capsys.readouterr().out
    assert "Total purchases: 2" in out
    assert "NewArtist / NewAlbum" in out


def test_generate_report_with_csv(tmp_path, capsys):
    csv_path = tmp_path / "report.csv"
    purchases = [_make_item(1, "Artist", "Album")]

    with patch("bandcampsync.report.Bandcamp") as MockBandcamp:
        mock_bc = MockBandcamp.return_value
        mock_bc.verify_authentication.return_value = True
        mock_bc.load_purchases.return_value = True
        mock_bc.purchases = purchases

        generate_report(
            cookies="identity=test",
            media_dir=tmp_path,
            dir_format="artist-album",
            csv_path=csv_path,
        )

    assert csv_path.is_file()
    with open(csv_path, newline="", encoding="utf-8") as f:
        rows = list(csv.reader(f))
    assert len(rows) == 2  # header + 1 item
    assert rows[1][1] == "Artist"


# --- growth audit ---


def _make_growth_item(item_id, tracks, band_name="Artist", item_title="Album"):
    item = _make_item(item_id, band_name, item_title)
    item._data["num_streamable_tracks"] = tracks
    return item


def _album_dir(root, name, flacs, item_id=None, extras=()):
    d = root / name
    d.mkdir(parents=True)
    for i in range(flacs):
        (d / f"{i + 1:02d} Track.flac").write_text("x")
    for extra in extras:
        (d / extra).write_text("x")
    if item_id is not None:
        (d / "bandcamp_item_id.txt").write_text(str(item_id))
    return d


def _zip_media(tmp_path, ignores):
    return LocalMedia(
        media_dir=tmp_path,
        ignores=ignores,
        skip_item_index=False,
        sync_ignore_file=False,
        dir_format="zip",
    )


def test_growth_detects_album_that_gained_tracks(tmp_path, ignores):
    _album_dir(tmp_path, "Artist - Album", flacs=3, item_id=1, extras=["cover.jpg"])
    lm = _zip_media(tmp_path, ignores)
    item = _make_growth_item(1, tracks=8)

    grown, unresolved = check_growth([(item, "downloaded", None)], lm, "zip")

    assert unresolved == 0
    assert len(grown) == 1
    assert (grown[0]["local"], grown[0]["remote"], grown[0]["missing"]) == (3, 8, 5)
    assert grown[0]["matched_by"] == "id"
    assert grown[0]["has_id_file"] is True


def test_growth_ignores_complete_album(tmp_path, ignores):
    _album_dir(tmp_path, "Artist - Album", flacs=8, item_id=1)
    lm = _zip_media(tmp_path, ignores)
    item = _make_growth_item(1, tracks=8)

    grown, _ = check_growth([(item, "downloaded", None)], lm, "zip")

    assert grown == []


def test_growth_ignores_album_with_more_local_than_remote(tmp_path, ignores):
    """Bonus tracks in a download that are not streamable; not a shortfall."""
    _album_dir(tmp_path, "Artist - Album", flacs=12, item_id=1)
    lm = _zip_media(tmp_path, ignores)
    item = _make_growth_item(1, tracks=10)

    grown, _ = check_growth([(item, "downloaded", None)], lm, "zip")

    assert grown == []


def test_growth_counts_multi_disc_subdirectories(tmp_path, ignores):
    d = _album_dir(tmp_path, "Artist - Album", flacs=0, item_id=1)
    for disc in ("Disc 1", "Disc 2"):
        sub = d / disc
        sub.mkdir()
        for i in range(5):
            (sub / f"{i + 1:02d} Track.flac").write_text("x")
    lm = _zip_media(tmp_path, ignores)
    item = _make_growth_item(1, tracks=10)

    grown, _ = check_growth([(item, "downloaded", None)], lm, "zip")

    assert grown == []


def test_growth_flags_name_match_without_id_file(tmp_path, ignores):
    """PRF-style directories carry no id file, so a repair could hit the wrong one."""
    _album_dir(tmp_path, "Artist - Album", flacs=2)
    lm = _zip_media(tmp_path, ignores)
    item = _make_growth_item(1, tracks=6)

    grown, _ = check_growth([(item, "downloaded", None)], lm, "zip")

    assert len(grown) == 1
    assert grown[0]["matched_by"] == "name"
    assert grown[0]["has_id_file"] is False


def test_growth_flags_duplicate_directory_names(tmp_path, ignores):
    """A root-level copy and a label-subdirectory copy of the same name."""
    _album_dir(tmp_path, "Artist - Album", flacs=2)
    _album_dir(tmp_path / "Label", "Artist - Album", flacs=2)
    lm = _zip_media(tmp_path, ignores)
    item = _make_growth_item(1, tracks=6)

    grown, _ = check_growth([(item, "downloaded", None)], lm, "zip")

    assert len(grown) == 1
    assert grown[0]["ambiguous"] is True


def test_growth_skips_items_without_track_count(tmp_path, ignores):
    _album_dir(tmp_path, "Artist - Album", flacs=1, item_id=1)
    lm = _zip_media(tmp_path, ignores)
    item = _make_item(1)  # no num_streamable_tracks at all

    grown, unresolved = check_growth([(item, "downloaded", None)], lm, "zip")

    assert grown == []
    assert unresolved == 0


def test_growth_skips_non_downloaded_statuses(tmp_path, ignores):
    lm = _zip_media(tmp_path, ignores)
    item = _make_growth_item(1, tracks=8)

    grown, unresolved = check_growth(
        [(item, "missing", None), (item, "ignored", None), (item, "preorder", None)],
        lm,
        "zip",
    )

    assert grown == []
    assert unresolved == 0


def test_growth_counts_unresolved_items(tmp_path, ignores):
    lm = _zip_media(tmp_path, ignores)
    item = _make_growth_item(1, tracks=8, band_name="Gone", item_title="Missing")

    grown, unresolved = check_growth([(item, "downloaded", None)], lm, "zip")

    assert grown == []
    assert unresolved == 1


def test_growth_sorts_most_underfilled_first(tmp_path, ignores):
    _album_dir(tmp_path, "Artist - Small", flacs=4, item_id=1)
    _album_dir(tmp_path, "Artist - Big", flacs=1, item_id=2)
    lm = _zip_media(tmp_path, ignores)
    results = [
        (_make_growth_item(1, tracks=6, item_title="Small"), "downloaded", None),
        (_make_growth_item(2, tracks=20, item_title="Big"), "downloaded", None),
    ]

    grown, _ = check_growth(results, lm, "zip")

    assert [r["missing"] for r in grown] == [19, 2]


def test_growth_artist_album_format(tmp_path, ignores):
    _album_dir(tmp_path / "Artist", "Album", flacs=3, item_id=1)
    lm = LocalMedia(
        media_dir=tmp_path,
        ignores=ignores,
        skip_item_index=False,
        sync_ignore_file=False,
        dir_format="artist-album",
    )
    item = _make_growth_item(1, tracks=9)

    grown, _ = check_growth([(item, "downloaded", None)], lm, "artist-album")

    assert len(grown) == 1
    assert grown[0]["missing"] == 6


def test_remote_track_count_handles_junk():
    assert remote_track_count(_make_growth_item(1, tracks=None)) is None
    assert remote_track_count(_make_growth_item(1, tracks="")) is None
    assert remote_track_count(_make_growth_item(1, tracks="7")) == 7


def test_print_growth_report_no_candidates(capsys):
    print_growth_report([], 0)
    assert "No albums hold fewer audio files" in capsys.readouterr().out
