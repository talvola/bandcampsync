import json
from pathlib import Path

import pytest
from bandcampsync.bandcamp import (
    Bandcamp,
    BandcampError,
    BandcampItem,
    BandcampNoDigitalDownload,
)


def _load_payload(name):
    fixture_path = Path(__file__).resolve().parent / "data" / name
    return json.loads(fixture_path.read_text(encoding="utf-8", errors="ignore"))


def _create_bandcamp():
    bandcamp = Bandcamp("identity=test")
    bandcamp.is_authenticated = True
    bandcamp.user_id = 1
    return bandcamp


def _download_key(item):
    return f"{item['sale_item_type']}{item['sale_item_id']}"


@pytest.fixture
def bandcamp():
    return _create_bandcamp()


@pytest.fixture
def digital_payload():
    return _load_payload("collection-item-digital-only.json")


@pytest.fixture
def physical_payload():
    return _load_payload("collection-item-physical-only.json")


@pytest.fixture
def digital_item(digital_payload):
    return digital_payload["items"][0]


@pytest.fixture
def physical_item(physical_payload):
    return physical_payload["items"][0]


# Tests if an item is a physical purchase (though may also include a digital component).
def test_is_physical_purchase_true(physical_item):
    assert BandcampItem(physical_item).is_physical_purchase() is True


# Tests if an item is _not_ a physical purchase (though doesn't necessarily include a digital component).
def test_is_physical_purchase_false(digital_item):
    assert BandcampItem(digital_item).is_physical_purchase() is False


# Tests if a download URL can be retrieved for a digital purchase.
def test_resolve_download_url_digital(bandcamp, digital_payload, digital_item):
    item = BandcampItem(digital_item)
    download_url = bandcamp._resolve_download_url(
        item,
        digital_payload["redownload_urls"],
    )
    expected_key = _download_key(digital_item)
    assert download_url == digital_payload["redownload_urls"][expected_key]


# Tests that no download URL can be retrieved for a physical-only purchase.
def test_resolve_download_url_physical(bandcamp, physical_payload, physical_item):
    item = BandcampItem(physical_item)
    download_url = bandcamp._resolve_download_url(
        item,
        physical_payload["redownload_urls"],
    )
    assert download_url is None


def _stub_download_page(mocker, bandcamp, digital_item):
    mocker.patch.object(bandcamp, "_request")
    mocker.patch.object(
        bandcamp,
        "_extract_pagedata_from_soup",
        return_value={"digital_items": [digital_item]},
    )


# A physical-only package can still be handed a download page; its entry has no
# "downloads" key and says includes_digital: false. That is not a parse error.
def test_get_download_file_url_physical_only(mocker, bandcamp):
    item = BandcampItem({"item_id": 1657249189, "download_url": "http://x/download"})
    _stub_download_page(
        mocker,
        bandcamp,
        {
            "item_id": 1657249189,
            "type": "package",
            "package_type_name": "Compact Disc (CD)",
            "includes_digital": False,
        },
    )
    with pytest.raises(BandcampNoDigitalDownload, match="Compact Disc"):
        bandcamp.get_download_file_url(item)


# A missing "downloads" key without that signal is still a parse error.
def test_get_download_file_url_missing_downloads_is_error(mocker, bandcamp):
    item = BandcampItem({"item_id": 7, "download_url": "http://x/download"})
    _stub_download_page(mocker, bandcamp, {"item_id": 7, "type": "album"})
    with pytest.raises(BandcampError) as exc:
        bandcamp.get_download_file_url(item)
    assert not isinstance(exc.value, BandcampNoDigitalDownload)
