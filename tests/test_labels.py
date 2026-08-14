import json
from datetime import datetime, timezone
from pathlib import Path

from bandcampsync.labels import FreeAlbum, album_from_details, _parse_release_date


def _load_payload(name):
    return json.loads(
        (Path(__file__).resolve().parent / "data" / name).read_text(encoding="utf-8")
    )


def _album(**kwargs):
    defaults = dict(
        item_id=1,
        title="Test Album",
        artist="Various Artists",
        url="https://example.bandcamp.com/album/test",
        price=0.0,
        is_set_price=False,
        require_email=False,
        free_download=False,
        num_tracks=10,
    )
    defaults.update(kwargs)
    return FreeAlbum(**defaults)


def test_parses_real_free_compilation_payload():
    album = album_from_details(_load_payload("tralbum-details-free-compilation.json"))
    assert album.item_id == 760931394
    assert album.artist == "Various Artists"
    assert album.price == 0.0
    assert album.is_set_price is False
    assert album.require_email is True
    assert album.num_tracks == 30
    assert album.currency == "EUR"
    assert album.is_free is True


def test_free_download_false_does_not_mean_paid():
    """free_download maps to download_pref == 1, not to "is $0 allowed".

    The real payload has free_download false but price 0.0, and is obtainable for free.
    Gating on free_download would wrongly skip nearly every free album.
    """
    album = album_from_details(_load_payload("tralbum-details-free-compilation.json"))
    assert album.free_download is False
    assert album.is_free is True


def test_non_downloadable_item_is_not_free():
    """Bandcamp reports price=None for items that are not separately purchasable, e.g.
    an individual track of a compilation. None coerces to 0.0, so without the
    has_digital_download check these look free and every download attempt fails."""
    track = album_from_details(
        {
            "id": 519386932,
            "title": "Pemod - Meduza",
            "price": None,
            "is_set_price": False,
            "has_digital_download": False,
        }
    )
    assert track.price == 0.0
    assert track.is_free is False


def test_missing_has_digital_download_defaults_to_downloadable():
    """Absent means the API did not say, and the common case is downloadable."""
    album = album_from_details(
        {"id": 1, "title": "x", "price": 0.0, "num_downloadable_tracks": 3}
    )
    assert album.has_digital_download is True
    assert album.is_free is True


def test_nonzero_price_is_not_free():
    assert _album(price=5.0).is_free is False


def test_set_price_is_not_free_even_at_zero():
    assert _album(price=0.0, is_set_price=True).is_free is False


def test_distinct_track_artists_ignores_nulls():
    album = _album(track_artists=[None, None, None])
    assert album.distinct_track_artists == set()
    album = _album(track_artists=["A", "B", "A", None])
    assert album.distinct_track_artists == {"A", "B"}


def test_parse_release_date_unix_timestamp():
    parsed = _parse_release_date(1783036800)
    assert parsed == datetime(2026, 7, 3, tzinfo=timezone.utc)


def test_parse_release_date_rfc_string():
    parsed = _parse_release_date("20 Jul 2026 00:00:00 GMT")
    assert parsed.year == 2026 and parsed.month == 7 and parsed.day == 20


def test_parse_release_date_handles_junk():
    assert _parse_release_date(None) is None
    assert _parse_release_date("") is None
    assert _parse_release_date("not a date") is None


def test_parse_release_date_handles_pre_1970():
    """Negative timestamps are real (Projekt Records) and datetime.fromtimestamp()
    raises OSError on them on Windows, which crashed a full scan."""
    parsed = _parse_release_date(-86400)
    assert parsed == datetime(1969, 12, 31, tzinfo=timezone.utc)


def test_parse_release_date_handles_absurd_values():
    assert _parse_release_date(10**20) is None
    assert _parse_release_date(-(10**20)) is None


def test_album_from_details_tolerates_missing_tracks():
    """A payload with no track data must parse rather than raise.

    It is not free, though: nothing is downloadable, so there is nothing to obtain for
    zero. See test_zero_downloadable_tracks_is_not_free.
    """
    album = album_from_details({"id": 5, "title": "x", "price": 0.0})
    assert album.num_tracks == 0
    assert album.track_artists == []
    assert album.is_free is False


def test_free_instant_download_with_a_nonzero_minimum_price():
    """download_pref == 1 makes the DIGITAL download free whatever `price` says.

    tralbum_details reports minimum_price as `price`, and a sold-out physical package
    can set that above zero while the download itself costs nothing. Real payload:
    My Proud Mountain's "15 Songs 15 Years" reports price=9.0 EUR, free_download=true,
    is_purchasable=false, merch_sold_out=true, and its album page carries
    download_pref=1 / freeDownloadPage=true.

    Before this was handled the album was classified paid and never queued, and
    freesync's `album.price > MAX_PRICE` ceiling would have rejected it a second time.
    """
    album = album_from_details(
        {
            "id": 3049177532,
            "title": "15 Songs 15 Years",
            "tralbum_artist": "MPM RADIO",
            "price": 9.0,
            "currency": "EUR",
            "is_set_price": False,
            "is_purchasable": False,
            "free_download": True,
            "has_digital_download": True,
            "num_downloadable_tracks": 15,
        }
    )
    assert album.price == 0.0
    assert album.free_download is True
    assert album.is_free is True


def test_free_download_flag_cannot_rescue_an_undownloadable_item():
    """The has_digital_download guard still wins: nothing to download is not free."""
    album = album_from_details(
        {
            "id": 1,
            "title": "Vinyl only",
            "price": 20.0,
            "is_set_price": False,
            "free_download": True,
            "has_digital_download": False,
        }
    )
    assert album.price == 20.0
    assert album.is_free is False


def test_zero_downloadable_tracks_is_not_free():
    """has_digital_download true with nothing to download is not free.

    Vinyl-only, CD and cassette listings do this. Before _digital_price they were only
    reachable at price=None; zeroing the price of every free_download item made a
    priced listing with no tracks look free too, and three reached the queue in the
    2026-08-14 audit. Each would have failed with "Release offers no downloads".
    """
    album = album_from_details(
        {
            "id": 3499470255,
            "title": "SECRET AGENT - A Pair Of Aces (CD)",
            "price": 10.0,
            "is_set_price": False,
            "free_download": True,
            "has_digital_download": True,
            "num_downloadable_tracks": 0,
            "tracks": [],
        }
    )
    assert album.price == 0.0
    assert album.num_tracks == 0
    assert album.is_free is False
