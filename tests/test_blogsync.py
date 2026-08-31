import json

from bandcampsync.blogsync import (
    STATUS_DOWNLOADED,
    STATUS_ERROR,
    STATUS_HAVE,
    STATUS_PAID,
    BlogItem,
    BlogState,
    LocalIndex,
    classify_items,
    extract_items,
    format_report,
    sync_post,
)
from bandcampsync.media import LocalMedia


def embed(url, title, author, item_id, kind="album"):
    attrs = json.dumps(
        {
            "url": url,
            "title": title,
            "author": author,
            "description": "5 track album",
            "embed_url": (
                f"https://bandcamp.com/EmbeddedPlayer/{kind}={item_id}/size=large/"
            ),
            "is_album": kind == "album",
        }
    )
    escaped = attrs.replace("&", "&amp;").replace('"', "&quot;")
    return (
        f'<div data-attrs="{escaped}" data-component-name="BandcampToDOM" '
        f'class="bandcamp-wrap album"><iframe src="x"></iframe></div>'
    )


def test_extracts_url_artist_title_and_id_from_embed():
    page = embed("https://ogierband.bandcamp.com/album/ogier", "Ogier, by Ogier", "Ogier", 3479528542)
    items = extract_items(page)
    assert len(items) == 1
    assert items[0].item_id == 3479528542
    assert items[0].item_type == "a"
    assert items[0].artist == "Ogier"
    assert items[0].url == "https://ogierband.bandcamp.com/album/ogier"


def test_deduplicates_repeated_embeds():
    # Substack renders the post body twice, so every embed is seen at least twice.
    one = embed("https://a.bandcamp.com/album/x", "X, by A", "A", 111)
    assert len(extract_items(one + one)) == 1


def test_extracts_standalone_tracks_as_type_t():
    page = embed("https://a.bandcamp.com/track/x", "X, by A", "A", 222, kind="track")
    assert extract_items(page)[0].item_type == "t"


def test_ignores_embeds_without_a_release_id():
    # A bandcamp embed pointing at an artist page carries no album=/track= id.
    attrs = '{&quot;url&quot;:&quot;https://a.bandcamp.com&quot;,&quot;embed_url&quot;:&quot;https://bandcamp.com/x&quot;}'
    page = f'<div data-attrs="{attrs}" data-component-name="BandcampToDOM"></div>'
    assert extract_items(page) == []


def test_extracts_nothing_from_a_post_with_no_embeds():
    assert extract_items("<html><body><p>no music this month</p></body></html>") == []


class FakeAPI:
    """Stands in for BandcampAPI, returning canned tralbum_details payloads."""

    def __init__(self, payloads, band_id=42):
        self.payloads = payloads
        self.band_id = band_id
        self.resolved = []

    def resolve_band_id(self, url):
        self.resolved.append(url)
        return self.band_id

    def tralbum_details(self, band_id, item_id, item_type="a"):
        return self.payloads[item_id]


def details(price=0.0, tracks=3, is_set=False, digital=True, artist="A", title="T"):
    return {
        "id": 1,
        "band_name": artist,
        "title": title,
        "price": price,
        "is_set_price": is_set,
        "has_digital_download": digital,
        "tracks": [{"title": f"t{i}", "band_name": artist} for i in range(tracks)],
    }


def test_classify_reports_api_error_payload_as_error_not_as_paid():
    # The API answers HTTP 200 with {"error": true} for a bad band_id, and
    # album_from_details turns that into price=0/tracks=0, which looks NOT FREE. An
    # unreachable album must never be reported as "no longer free".
    item = BlogItem(1, "a", "https://a.bandcamp.com/album/x", "T", "A")
    api = FakeAPI({1: {"error": True, "error_message": "bad band_id: '0'"}})
    (_, album, error) = next(iter(classify_items([item], api)))
    assert album is None
    assert "bad band_id" in error


def test_classify_surfaces_exceptions_as_errors():
    class Boom(FakeAPI):
        def resolve_band_id(self, url):
            raise ValueError("no band id here")

    item = BlogItem(1, "a", "https://a.bandcamp.com/album/x", "T", "A")
    (_, album, error) = next(iter(classify_items([item], Boom({}))))
    assert album is None
    assert "no band id here" in error


def test_classify_resolves_each_subdomain_only_once():
    items = [
        BlogItem(1, "a", "https://same.bandcamp.com/album/x", "X", "A"),
        BlogItem(2, "a", "https://same.bandcamp.com/album/y", "Y", "A"),
    ]
    api = FakeAPI({1: details(), 2: details()})
    list(classify_items(items, api))
    assert len(api.resolved) == 1


def album_dir(root, name, item_id=None, tracks=1):
    d = root / name
    d.mkdir(parents=True)
    for i in range(tracks):
        (d / f"{i:02d}.flac").write_bytes(b"x")
    if item_id is not None:
        (d / LocalMedia.ITEM_INDEX_FILENAME).write_text(str(item_id), encoding="utf-8")
    return d


def test_local_index_matches_on_item_id_whatever_the_directory_is_called(tmp_path):
    album_dir(tmp_path, "Something Entirely Different", item_id=999)
    index = LocalIndex(tmp_path)
    item = BlogItem(999, "a", "https://a.bandcamp.com/album/x", "T", "A")
    assert index.find(item) is not None


def test_local_index_matches_by_name_when_no_id_file(tmp_path):
    album_dir(tmp_path, "Bongripper - Live in Leipzeg")
    index = LocalIndex(tmp_path)
    item = BlogItem(1, "a", "https://a.bandcamp.com/album/x", "Live in Leipzeg", "Bongripper")
    assert index.find(item) is not None


def test_local_index_finds_albums_inside_label_directories(tmp_path):
    # The blog's albums sit at the root AND inside label dirs; both must be seen.
    album_dir(tmp_path / "The Swamp Records", "Cripta Blue - Chaos at the Shangri La")
    index = LocalIndex(tmp_path)
    item = BlogItem(1, "a", "https://a.bandcamp.com/album/x", "Chaos at the Shangri La", "Cripta Blue")
    found = index.find(item)
    assert found is not None and found.parent.name == "The Swamp Records"


def test_local_index_ignores_case_and_punctuation(tmp_path):
    album_dir(tmp_path, "BLACK SUN VOID - Funeral Sun!")
    index = LocalIndex(tmp_path)
    item = BlogItem(1, "a", "https://a.bandcamp.com/album/x", "Funeral Sun", "Black sun Void")
    assert index.find(item) is not None


def test_local_index_does_not_invent_a_match(tmp_path):
    album_dir(tmp_path, "Some Other Band - Some Other Record")
    index = LocalIndex(tmp_path)
    item = BlogItem(1, "a", "https://a.bandcamp.com/album/x", "Cerium", "The Moondig")
    assert index.find(item) is None


def test_local_index_survives_an_unreadable_id_file(tmp_path):
    d = album_dir(tmp_path, "A - B")
    (d / LocalMedia.ITEM_INDEX_FILENAME).write_text("None", encoding="utf-8")
    index = LocalIndex(tmp_path)  # must not raise
    assert index.by_id == {}


class Config:
    def __init__(self, media_dir):
        self.media_dir = media_dir
        self.email = "e@example.com"
        self.country = "US"
        self.postcode = "94110"
        self.media_format = "flac"
        self.request_delay = 0


def test_sync_reports_paid_items_and_does_not_download_them(tmp_path, mocker):
    page = embed("https://a.bandcamp.com/album/x", "T, by A", "A", 1)
    mocker.patch("bandcampsync.blogsync.fetch_post", return_value=page)
    download = mocker.patch("bandcampsync.blogsync.download_item")
    api = FakeAPI({1: details(price=5.0)})
    state = BlogState(path=tmp_path / "s.json")

    results = sync_post("u", Config(tmp_path), state, api=api, index=LocalIndex(tmp_path))

    assert [r.status for r in results] == [STATUS_PAID]
    download.assert_not_called()
    assert state.pending == {}


def test_sync_skips_the_download_but_still_queues_an_album_already_on_disk(tmp_path, mocker):
    album_dir(tmp_path, "A - T")
    page = embed("https://a.bandcamp.com/album/x", "T, by A", "A", 1)
    mocker.patch("bandcampsync.blogsync.fetch_post", return_value=page)
    download = mocker.patch("bandcampsync.blogsync.download_item")
    api = FakeAPI({1: details()})
    state = BlogState(path=tmp_path / "s.json")

    results = sync_post("u", Config(tmp_path), state, api=api, index=LocalIndex(tmp_path))

    assert [r.status for r in results] == [STATUS_HAVE]
    download.assert_not_called()
    # Having it already is exactly when nothing else would add it to the collection.
    assert 1 in state.pending


def test_sync_downloads_a_free_missing_album_and_queues_it(tmp_path, mocker):
    page = embed("https://a.bandcamp.com/album/x", "T, by A", "A", 1)
    mocker.patch("bandcampsync.blogsync.fetch_post", return_value=page)
    mocker.patch(
        "bandcampsync.blogsync.download_item", return_value=(tmp_path / "A - T", 1234)
    )
    api = FakeAPI({1: details()})
    state = BlogState(path=tmp_path / "s.json")

    results = sync_post("u", Config(tmp_path), state, api=api, index=LocalIndex(tmp_path))

    assert [r.status for r in results] == [STATUS_DOWNLOADED]
    assert results[0].bytes_downloaded == 1234
    assert 1 in state.pending


def test_report_mode_downloads_nothing_and_writes_no_state(tmp_path, mocker):
    page = embed("https://a.bandcamp.com/album/x", "T, by A", "A", 1)
    mocker.patch("bandcampsync.blogsync.fetch_post", return_value=page)
    download = mocker.patch("bandcampsync.blogsync.download_item")
    api = FakeAPI({1: details()})
    state_path = tmp_path / "s.json"
    state = BlogState(path=state_path)

    sync_post("u", Config(tmp_path), state, report_only=True, api=api, index=LocalIndex(tmp_path))

    download.assert_not_called()
    assert not state_path.exists()


def test_a_failed_download_does_not_stop_the_rest(tmp_path, mocker):
    page = embed("https://a.bandcamp.com/album/x", "X, by A", "A", 1) + embed(
        "https://b.bandcamp.com/album/y", "Y, by B", "B", 2
    )
    mocker.patch("bandcampsync.blogsync.fetch_post", return_value=page)
    mocker.patch(
        "bandcampsync.blogsync.download_item",
        side_effect=[ValueError("boom"), (tmp_path / "B - Y", 10)],
    )
    api = FakeAPI({1: details(), 2: details()})
    state = BlogState(path=tmp_path / "s.json")

    results = sync_post("u", Config(tmp_path), state, api=api, index=LocalIndex(tmp_path))

    assert [r.status for r in results] == [STATUS_ERROR, STATUS_DOWNLOADED]
    assert 1 not in state.pending and 2 in state.pending


def test_state_round_trips_with_integer_keys(tmp_path):
    # Keyed by int like FreeState; a str key silently misses.
    state = BlogState(path=tmp_path / "s.json")
    state.queue(BlogItem(7, "a", "u", "T", "A"), tmp_path / "A - T")
    state.save()
    assert BlogState.load(tmp_path / "s.json").pending[7]["artist"] == "A"


def test_state_does_not_requeue_something_already_added(tmp_path):
    state = BlogState(path=tmp_path / "s.json")
    state.added[7] = {"artist": "A", "title": "T"}
    state.queue(BlogItem(7, "a", "u", "T", "A"))
    assert state.pending == {}


def test_report_separates_could_not_check_from_no_longer_free():
    from bandcampsync.blogsync import BlogResult

    results = [
        BlogResult(BlogItem(1, "a", "u1", "T1", "A1"), STATUS_PAID, detail="5.0USD"),
        BlogResult(BlogItem(2, "a", "u2", "T2", "A2"), STATUS_ERROR, detail="timeout"),
    ]
    text = format_report("post", results)
    assert "No longer free:  1" in text
    assert "Could not check: 1" in text
    assert "not necessarily paid" in text


def test_strips_by_suffix_when_author_transliteration_differs():
    # Bandcamp's oEmbed author can be Latin while the title's suffix is Cyrillic
    # (Nosferator, September 2026). An exact-author strip alone leaves the credit on.
    page = embed(
        "https://nosferator.bandcamp.com/album/-",
        "\u0421\u043c\u043e\u0443\u043a \u0421\u0430\u043a\u0438\u043d \u0424\u0440\u0438\u043a, by \u041d\u043e\u0441\u0444\u0435\u0440\u0430\u0442\u043e\u0440",
        "Nosferator",
        154633428,
    )
    assert extract_items(page)[0].title == "\u0421\u043c\u043e\u0443\u043a \u0421\u0430\u043a\u0438\u043d \u0424\u0440\u0438\u043a"


def test_keeps_a_title_that_is_only_a_by_clause():
    # Nothing before the separator means it is not a "<title>, by <artist>" composite.
    page = embed("https://a.bandcamp.com/album/x", ", by A", "A", 5)
    assert extract_items(page)[0].title == ", by A"


def test_report_prefers_bandcamps_names_over_the_blogs():
    from bandcampsync.blogsync import BlogResult
    from bandcampsync.labels import FreeAlbum

    album = FreeAlbum(
        item_id=1, title="Real Title", artist="Real Artist", url="u", price=0.0,
        is_set_price=False, require_email=False, free_download=False, num_tracks=3,
    )
    result = BlogResult(BlogItem(1, "a", "u", "Blog Title", "Blog Artist"),
                        STATUS_DOWNLOADED, album=album)
    text = format_report("post", [result])
    assert "Real Artist / Real Title" in text
    assert "Blog Artist" not in text


def test_queue_records_item_type_so_tracks_can_be_told_apart(tmp_path):
    state = BlogState(path=tmp_path / "s.json")
    state.queue(BlogItem(7, "t", "u", "Doom Blues", "November Fire"))
    assert state.pending[7]["item_type"] == "t"


def test_state_does_not_requeue_something_already_skipped(tmp_path):
    # Without this a standalone track is retried on every single run, forever.
    state = BlogState(path=tmp_path / "s.json")
    state.skipped[7] = {"reason": "standalone track"}
    state.queue(BlogItem(7, "t", "u", "T", "A"))
    assert state.pending == {}


def test_skipped_round_trips(tmp_path):
    state = BlogState(path=tmp_path / "s.json")
    state.skipped[7] = {"artist": "A", "title": "T", "reason": "why"}
    state.save()
    assert BlogState.load(tmp_path / "s.json").skipped[7]["reason"] == "why"
