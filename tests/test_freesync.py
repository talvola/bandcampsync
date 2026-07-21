import time
from datetime import datetime, timezone

from bandcampsync.labels import DiscoEntry, FreeAlbum
from bandcampsync.labelconfig import LabelSpec, prefilter
from bandcampsync.freesync import FreeState, LabelIndex, RECHECK_DAYS, resolve_cutoff


def _album(**kwargs):
    defaults = dict(
        item_id=42,
        title="Atmospheric Progressive #021 (Free Download)",
        artist="Various Artists",
        url="https://example.bandcamp.com/album/test",
        price=0.0,
        is_set_price=False,
        require_email=True,
        free_download=False,
        num_tracks=30,
    )
    defaults.update(kwargs)
    return FreeAlbum(**defaults)


# --- prefilter: reject on cheap metadata before spending a request ---


def test_prefilter_rejects_wrong_artist():
    entry = DiscoEntry(item_id=1, item_type="a", title="Daydreams", artist="Magros")
    keep, reason = prefilter(entry, {"various_artists": True})
    assert keep is False
    assert "Magros" in reason


def test_prefilter_keeps_various_artists():
    entry = DiscoEntry(item_id=1, item_type="a", title="Comp", artist="Various Artists")
    keep, _ = prefilter(entry, {"various_artists": True})
    assert keep is True


def test_prefilter_passes_rules_it_cannot_evaluate():
    """track_artists_vary needs track data, so it must not reject at prefilter time."""
    entry = DiscoEntry(item_id=1, item_type="a", title="Comp", artist="Tadpole Records")
    keep, _ = prefilter(entry, {"track_artists_vary": True, "title_separator": " : "})
    assert keep is True


def test_prefilter_keeps_when_artist_unknown():
    """An empty artist must not be rejected; fall through to the full check."""
    entry = DiscoEntry(item_id=1, item_type="a", title="Comp", artist="")
    keep, _ = prefilter(entry, {"various_artists": True})
    assert keep is True


def test_prefilter_title_regex():
    entry = DiscoEntry(item_id=1, item_type="a", title="Winter Sampler 2021")
    assert prefilter(entry, {"title_regex": "Sampler"})[0] is True
    assert prefilter(entry, {"title_regex": "Compilation"})[0] is False


# --- scan cutoff ---


class _Index:
    def __init__(self, mtime=None):
        self._mtime = mtime

    def newest_mtime(self):
        return self._mtime


def _spec(**kwargs):
    defaults = dict(name="L", url="https://l.bandcamp.com/")
    defaults.update(kwargs)
    return LabelSpec(**defaults)


def test_first_scan_has_no_cutoff(tmp_path):
    """The first scan of a label must examine everything, to catch up on manual gaps."""
    state = FreeState(tmp_path / "s.json")
    cutoff, why = resolve_cutoff(
        _spec(), state, _Index(datetime.now(timezone.utc)), False
    )
    assert cutoff is None
    assert "first scan" in why


def test_full_scan_overrides_cutoff(tmp_path):
    state = FreeState(tmp_path / "s.json")
    state.label("L")["first_scan_done"] = True
    cutoff, why = resolve_cutoff(
        _spec(), state, _Index(datetime.now(timezone.utc)), True
    )
    assert cutoff is None
    assert "full scan" in why


def test_subsequent_scan_uses_recorded_release_date(tmp_path):
    state = FreeState(tmp_path / "s.json")
    state.label("L")["first_scan_done"] = True
    seen = datetime(2026, 7, 3, tzinfo=timezone.utc)
    state.label("L")["newest_release_seen"] = int(seen.timestamp())
    cutoff, why = resolve_cutoff(_spec(), state, _Index(None), False)
    assert cutoff == seen
    assert "already examined" in why


def test_recorded_release_date_beats_disk_mtime(tmp_path):
    """File mtime must not win: downloading anything updates it, which would push the
    cutoff past older releases that were never fetched."""
    state = FreeState(tmp_path / "s.json")
    state.label("L")["first_scan_done"] = True
    seen = datetime(2025, 4, 21, tzinfo=timezone.utc)
    state.label("L")["newest_release_seen"] = int(seen.timestamp())
    fresh_mtime = datetime(2026, 7, 20, tzinfo=timezone.utc)
    cutoff, _ = resolve_cutoff(_spec(), state, _Index(fresh_mtime), False)
    assert cutoff == seen


def test_disk_mtime_used_only_as_fallback(tmp_path):
    state = FreeState(tmp_path / "s.json")
    state.label("L")["first_scan_done"] = True
    mtime = datetime(2026, 5, 24, tzinfo=timezone.utc)
    cutoff, why = resolve_cutoff(_spec(), state, _Index(mtime), False)
    assert cutoff == mtime
    assert "newest local file" in why


def test_pending_survives_state_roundtrip(tmp_path):
    """A wanted-but-not-downloaded album must not be forgotten between runs."""
    path = tmp_path / "s.json"
    state = FreeState(path)
    state.pending[760931394] = "Future Avenue"
    state.save()
    assert FreeState(path).pending == {760931394: "Future Avenue"}


def test_configured_since_beats_disk_mtime(tmp_path):
    state = FreeState(tmp_path / "s.json")
    state.label("L")["first_scan_done"] = True
    since = datetime(2024, 1, 1, tzinfo=timezone.utc)
    cutoff, why = resolve_cutoff(
        _spec(since=since),
        state,
        _Index(datetime(2026, 5, 24, tzinfo=timezone.utc)),
        False,
    )
    assert cutoff == since
    assert "configured since" in why


def test_no_local_files_means_no_cutoff(tmp_path):
    state = FreeState(tmp_path / "s.json")
    state.label("L")["first_scan_done"] = True
    cutoff, _ = resolve_cutoff(_spec(), state, _Index(None), False)
    assert cutoff is None


# --- classification cache ---


def test_free_classification_cached_indefinitely(tmp_path):
    state = FreeState(tmp_path / "s.json")
    state.cache(_album(), is_free=True)
    state.items[42]["checked_ts"] = 0  # ancient
    assert state.cached(42) is not None


def test_paid_classification_expires_for_recheck(tmp_path):
    """Prices can change, so a not-free verdict must not be cached forever."""
    state = FreeState(tmp_path / "s.json")
    state.cache(_album(price=5.0), is_free=False)
    assert state.cached(42) is not None
    state.items[42]["checked_ts"] = time.time() - (RECHECK_DAYS + 1) * 86400
    assert state.cached(42) is None


def test_state_roundtrip(tmp_path):
    path = tmp_path / "s.json"
    state = FreeState(path)
    state.label("L")["band_id"] = 123
    state.cache(_album(), is_free=True)
    state.save()

    reloaded = FreeState(path)
    assert reloaded.labels["L"]["band_id"] == 123
    assert reloaded.cached(42)["title"].startswith("Atmospheric")


def test_state_survives_corrupt_file(tmp_path):
    path = tmp_path / "s.json"
    path.write_text("{not valid json", encoding="utf-8")
    state = FreeState(path)
    assert state.labels == {}
    assert state.items == {}


# --- local index ---


def test_index_matches_by_id_and_by_name(tmp_path):
    label_dir = tmp_path / "Future Avenue"
    with_id = (
        label_dir / "Various Artists - Atmospheric Progressive #020 (Free Download)"
    )
    with_id.mkdir(parents=True)
    (with_id / "bandcamp_item_id.txt").write_text("305708411\n")
    name_only = (
        label_dir / "Various Artists - Atmospheric Progressive #019 (Free Download)"
    )
    name_only.mkdir(parents=True)

    index = LabelIndex(tmp_path, "Future Avenue")
    assert 305708411 in index.ids

    by_id = _album(
        item_id=305708411, title="Atmospheric Progressive #020 (Free Download)"
    )
    assert index.find(by_id)[0] is not None

    by_name = _album(item_id=999, title="Atmospheric Progressive #019 (Free Download)")
    assert index.find(by_name)[0] == name_only.name

    missing = _album(item_id=1, title="Atmospheric Progressive #021 (Free Download)")
    assert index.find(missing)[0] is None


def test_index_normalisation_tolerates_punctuation_drift(tmp_path):
    """On-disk names come from zip filenames and drift from the remote title."""
    label_dir = tmp_path / "Future Avenue"
    (
        label_dir / "Various Artists - Best Of 2023 - The Originals (Free Download)"
    ).mkdir(parents=True)
    index = LabelIndex(tmp_path, "Future Avenue")
    remote = _album(item_id=7, title="Best Of 2023 | The Originals (Free Download)")
    assert index.find(remote)[0] is not None


def test_index_handles_missing_label_dir(tmp_path):
    index = LabelIndex(tmp_path, "Nonexistent Label")
    assert index.ids == set()
    assert index.newest_mtime() is None
    assert index.find(_album())[0] is None


# --- repair: extract only what is missing ---


def test_extract_missing_adds_only_absent_files(tmp_path):
    import zipfile
    from bandcampsync.freedownload import extract_missing

    local = tmp_path / "album"
    local.mkdir()
    (local / "01 One.flac").write_text("original one")
    (local / "02 Two.flac").write_text("original two")

    archive = tmp_path / "album.zip"
    with zipfile.ZipFile(archive, "w") as zf:
        zf.writestr("01 One.flac", "REPLACED")
        zf.writestr("02 Two.flac", "REPLACED")
        zf.writestr("03 Three.flac", "the missing track")
        zf.writestr("cover.jpg", "art")

    added = extract_missing(archive, local)

    assert sorted(added) == ["03 Three.flac", "cover.jpg"]
    # Existing files must not be rewritten.
    assert (local / "01 One.flac").read_text() == "original one"
    assert (local / "02 Two.flac").read_text() == "original two"
    assert (local / "03 Three.flac").read_text() == "the missing track"


def test_extract_missing_flattens_nested_entries(tmp_path):
    import zipfile
    from bandcampsync.freedownload import extract_missing

    local = tmp_path / "album"
    local.mkdir()
    archive = tmp_path / "album.zip"
    with zipfile.ZipFile(archive, "w") as zf:
        zf.writestr("nested/dir/04 Four.flac", "data")

    added = extract_missing(archive, local)
    assert added == ["04 Four.flac"]
    assert (local / "04 Four.flac").is_file()


def test_extract_missing_noop_when_complete(tmp_path):
    import zipfile
    from bandcampsync.freedownload import extract_missing

    local = tmp_path / "album"
    local.mkdir()
    (local / "01 One.flac").write_text("x")
    archive = tmp_path / "album.zip"
    with zipfile.ZipFile(archive, "w") as zf:
        zf.writestr("01 One.flac", "y")

    assert extract_missing(archive, local) == []


def test_extract_missing_matches_across_album_rename(tmp_path):
    """The real Weedian failure: the label renamed 'Trip to Poland' to
    'Trip to Poland II', and the album title is embedded in every filename. Comparing
    full filenames matched nothing and duplicated the entire 119-track directory."""
    import zipfile
    from bandcampsync.freedownload import extract_missing

    local = tmp_path / "album"
    local.mkdir()
    for n, t in [
        ("01", "Tortuga - Esoteric Order"),
        ("02", "Weedpecker - Reality Fades"),
    ]:
        (local / f"WEEDIAN - Trip to Poland (+100 Bands) - {n} {t}.flac").write_text(
            "old"
        )

    archive = tmp_path / "album.zip"
    with zipfile.ZipFile(archive, "w") as zf:
        for n, t in [
            ("01", "Tortuga - Esoteric Order"),
            ("02", "Weedpecker - Reality Fades"),
            ("03", "Pemod - Meduza"),
        ]:
            zf.writestr(
                f"WEEDIAN - Trip to Poland II (+100 Bands) - {n} {t}.flac", "new"
            )

    added = extract_missing(archive, local)

    assert len(added) == 1
    assert "Pemod - Meduza" in added[0]
    assert len(list(local.glob("*.flac"))) == 3


def test_extract_missing_refuses_runaway_duplication(tmp_path):
    """Guard: if names have drifted beyond reconciliation, stop rather than double the
    directory."""
    import zipfile
    import pytest
    from bandcampsync.freedownload import AcquireError, extract_missing

    local = tmp_path / "album"
    local.mkdir()
    for i in range(10):
        (local / f"Completely Different Name {i:02d}.flac").write_text("x")

    archive = tmp_path / "album.zip"
    with zipfile.ZipFile(archive, "w") as zf:
        for i in range(10):
            zf.writestr(f"Unrelated Naming Scheme {i:02d}.flac", "y")

    with pytest.raises(AcquireError, match="Refusing to duplicate"):
        extract_missing(archive, local, max_additions=2)


def test_pending_cleared_when_album_stops_being_free(tmp_path, monkeypatch):
    """A pending album that turns out not to be free must be dropped from pending,
    otherwise it bypasses the date cutoff and is reconsidered on every future run."""
    from bandcampsync import freesync
    from bandcampsync.labelconfig import LabelSpec
    from bandcampsync.labels import DiscoEntry

    state = freesync.FreeState(tmp_path / "s.json")
    state.label("L")["first_scan_done"] = True
    state.pending[7] = "L"

    entry = DiscoEntry(item_id=7, item_type="a", title="Streaming Only", artist="X")
    monkeypatch.setattr(freesync, "list_discography", lambda api, band_id: [entry])
    # price 0 but not downloadable -> not free
    monkeypatch.setattr(
        freesync,
        "album_from_details",
        lambda details, label_name="": FreeAlbum(
            item_id=7,
            title="Streaming Only",
            artist="X",
            url="",
            price=0.0,
            is_set_price=False,
            require_email=False,
            free_download=False,
            num_tracks=1,
            has_digital_download=False,
        ),
    )

    class _API:
        def tralbum_details(self, *a, **k):
            return {}

    spec = LabelSpec(name="L", url="https://l.bandcamp.com/", band_id=1)
    results, _index, _meta = freesync.scan_label(_API(), spec, state, tmp_path)

    assert results[0][1] == freesync.STATUS_NOT_FREE
    assert 7 not in state.pending


def test_pending_cleared_when_cached_not_free(tmp_path, monkeypatch):
    """Same, via the cached-classification shortcut rather than a fresh fetch."""
    from bandcampsync import freesync
    from bandcampsync.labelconfig import LabelSpec
    from bandcampsync.labels import DiscoEntry

    state = freesync.FreeState(tmp_path / "s.json")
    state.label("L")["first_scan_done"] = True
    state.pending[9] = "L"
    state.items[9] = {"title": "Paid", "is_free": False, "checked_ts": 2**31}

    entry = DiscoEntry(item_id=9, item_type="a", title="Paid", artist="X")
    monkeypatch.setattr(freesync, "list_discography", lambda api, band_id: [entry])

    spec = LabelSpec(name="L", url="https://l.bandcamp.com/", band_id=1)
    results, _index, _meta = freesync.scan_label(object(), spec, state, tmp_path)

    assert results[0][1] == freesync.STATUS_NOT_FREE
    assert 9 not in state.pending


# --- duplicate detection: report, never auto-merge ---


def _mkdirs(root, label, names):
    for n in names:
        (root / label / n).mkdir(parents=True)
    return root


def test_finds_catalogue_number_duplicates(tmp_path):
    """Real RDC Music case: the label added a catalogue number to the title."""
    from bandcampsync.freesync import find_duplicate_dirs

    _mkdirs(
        tmp_path, "RDC Music", ["Monzanto - Monzanto", "Monzanto - Monzanto -RDC 26"]
    )
    pairs = find_duplicate_dirs(tmp_path, "RDC Music")
    assert len(pairs) == 1


def test_finds_free_suffix_duplicates(tmp_path):
    """Real Projekt cases: '(free!)' and '(name-your-price)' suffixes."""
    from bandcampsync.freesync import find_duplicate_dirs

    _mkdirs(
        tmp_path,
        "Projekt Records",
        [
            "Various Artists - The Hues of Infinity",
            "Various Artists - The Hues of Infinity (free!)",
            "Various Projekt Artists - Projekt2022",
            "Various Projekt Artists - Projekt2022 (name-your-price)",
        ],
    )
    pairs = find_duplicate_dirs(tmp_path, "Projekt Records")
    assert len(pairs) == 2


def test_does_not_flag_sequels_as_duplicates(tmp_path):
    """Critical: Trip to Poland and Trip to Poland II are different albums. Merging
    them would lose music, so a trailing volume marker must never match."""
    from bandcampsync.freesync import find_duplicate_dirs

    _mkdirs(
        tmp_path,
        "Weedian",
        [
            "WEEDIAN - Trip to Poland",
            "WEEDIAN - Trip to Poland II (+100 Bands)",
            "WEEDIAN - Trip to Germany",
            "WEEDIAN - Trip to Germany II (+180 Bands)",
            "WEEDIAN - Volume I",
            "WEEDIAN - Volume II",
        ],
    )
    assert find_duplicate_dirs(tmp_path, "Weedian") == []


def test_does_not_flag_unrelated_titles(tmp_path):
    from bandcampsync.freesync import find_duplicate_dirs

    _mkdirs(
        tmp_path,
        "Future Avenue",
        [
            "Various Artists - Atmospheric Progressive #020 (Free Download)",
            "Various Artists - Atmospheric Progressive #021 (Free Download)",
        ],
    )
    assert find_duplicate_dirs(tmp_path, "Future Avenue") == []


def test_missing_label_dir_yields_no_pairs(tmp_path):
    from bandcampsync.freesync import find_duplicate_dirs

    assert find_duplicate_dirs(tmp_path, "Nonexistent") == []


def test_volume_suffix_is_not_a_catalogue_number(tmp_path):
    """'Album' vs 'Album vol 2' must not match, even though it shares the shape of a
    catalogue number like 'RDC 26'."""
    from bandcampsync.freesync import _looks_like_same_release

    assert _looks_like_same_release("some album", "some album vol 2") is False
    assert _looks_like_same_release("some album", "some album part 3") is False
    assert _looks_like_same_release("some album", "some album rdc 26") is True


def test_index_does_not_lose_directories_to_title_collisions(tmp_path):
    """The Audio Atelier has four distinct releases titled 'Best of 2025'. A dict keyed
    by title silently dropped all but one, so albums that were not on disk got reported
    as downloaded."""
    label_dir = tmp_path / "The Audio Atelier"
    (label_dir / "Noetic Resonance - Best Of 2025 (Free Download)").mkdir(parents=True)
    (label_dir / "Various Artist - Best of 2025 (Free Download)").mkdir(parents=True)

    index = LabelIndex(tmp_path, "The Audio Atelier")
    total = sum(len(v) for v in index.by_title.values())
    assert total == 2, "both directories must be indexed"

    album = _album(item_id=999, title="Best of 2025 (Free Download)")
    name, ambiguous = index.find(album)
    assert name is None
    assert ambiguous is True, "must not guess between same-titled directories"


def test_id_match_beats_title_collision(tmp_path):
    """Once id files exist the ambiguity is resolved."""
    label_dir = tmp_path / "L"
    a = label_dir / "Artist A - Best of 2025"
    b = label_dir / "Artist B - Best of 2025"
    a.mkdir(parents=True)
    b.mkdir(parents=True)
    (a / "bandcamp_item_id.txt").write_text("111\n")
    (b / "bandcamp_item_id.txt").write_text("222\n")

    index = LabelIndex(tmp_path, "L")
    assert index.find(_album(item_id=111, title="Best of 2025")) == (a.name, False)
    assert index.find(_album(item_id=222, title="Best of 2025")) == (b.name, False)
    # A third same-titled release is genuinely missing, not ambiguous: both local
    # directories are already claimed by other ids.
    assert index.find(_album(item_id=333, title="Best of 2025")) == (None, False)


def test_state_load_tolerates_malformed_keys(tmp_path):
    """A single bad key must not make the whole state file unreadable: that would reset
    every label to a first full scan and re-fetch thousands of albums."""
    import json

    path = tmp_path / "s.json"
    path.write_text(
        json.dumps(
            {
                "labels": {"L": {"band_id": 1}},
                "items": {"123": {"title": "ok"}, "None": {"title": ""}},
                "pending": {"456": "L", "junk": "L"},
            }
        ),
        encoding="utf-8",
    )
    state = FreeState(path)
    assert 123 in state.items
    assert state.items[123]["title"] == "ok"
    assert state.pending == {456: "L"}


def test_cache_refuses_album_without_item_id(tmp_path):
    """Storing under a None key is what corrupted the state file in the first place."""
    state = FreeState(tmp_path / "s.json")
    album = FreeAlbum(
        item_id=None,
        title="",
        artist="",
        url="",
        price=0.0,
        is_set_price=False,
        require_email=False,
        free_download=False,
        num_tracks=0,
    )
    state.cache(album, True)
    assert state.items == {}


def test_skipped_items_survive_state_roundtrip(tmp_path):
    """A release with no digital download can never succeed; recording it stops the
    tool retrying it on every run forever."""
    path = tmp_path / "s.json"
    state = FreeState(path)
    state.skipped[555] = "no flac download"
    state.pending[555] = "L"
    state.save()

    reloaded = FreeState(path)
    assert reloaded.skipped == {555: "no flac download"}


def test_scan_does_not_requeue_skipped_items(tmp_path, monkeypatch):
    from bandcampsync import freesync
    from bandcampsync.labelconfig import LabelSpec
    from bandcampsync.labels import DiscoEntry

    state = freesync.FreeState(tmp_path / "s.json")
    state.label("L")["first_scan_done"] = True
    state.skipped[7] = "no flac download"
    state.pending[7] = "L"

    entry = DiscoEntry(item_id=7, item_type="a", title="Tape Only", artist="X")
    monkeypatch.setattr(freesync, "list_discography", lambda api, band_id: [entry])
    monkeypatch.setattr(
        freesync,
        "album_from_details",
        lambda details, label_name="": FreeAlbum(
            item_id=7,
            title="Tape Only",
            artist="X",
            url="",
            price=0.0,
            is_set_price=False,
            require_email=False,
            free_download=False,
            num_tracks=5,
        ),
    )

    class _API:
        def tralbum_details(self, *a, **k):
            return {}

    spec = LabelSpec(name="L", url="https://l.bandcamp.com/", band_id=1)
    results, _index, _meta = freesync.scan_label(_API(), spec, state, tmp_path)

    assert results[0][1] == freesync.STATUS_ERROR
    assert "skipped" in results[0][2]
    assert 7 not in state.pending


def test_pending_albums_ignores_skipped(tmp_path):
    from bandcampsync import freesync
    from bandcampsync.labelconfig import FreeConfig, LabelSpec

    state = freesync.FreeState(tmp_path / "s.json")
    state.pending[1] = "L"
    state.pending[2] = "L"
    state.skipped[2] = "no flac download"
    state.items[1] = {"title": "ok", "url": "https://x.bandcamp.com/album/a"}
    state.items[2] = {"title": "tape", "url": "https://x.bandcamp.com/album/b"}

    config = FreeConfig(labels=[LabelSpec(name="L", url="https://l.bandcamp.com/")])
    out = freesync.pending_albums(config, state)
    assert [a.item_id for _lab, a in out] == [1]
