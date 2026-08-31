from bandcampsync.blogplex import PlexCollection, drain_pending
from bandcampsync.blogsync import BlogItem, BlogState


def test_local_path_is_rewritten_to_the_form_plex_reports():
    # N: is the SMB mount of the same files the server serves from /share/Music.
    assert PlexCollection.local_to_plex_path(r"N:\Bandcamp (FLAC)\A - B") == (
        "/share/Music/Bandcamp (FLAC)/A - B"
    )


def test_local_path_outside_the_media_root_is_not_rewritten():
    assert PlexCollection.local_to_plex_path(r"C:\somewhere\else") == ""
    assert PlexCollection.local_to_plex_path("") == ""


class FakePlex:
    """Stands in for PlexCollection: canned search results and a recording add()."""

    def __init__(self, found=None, members=None, dirs=None):
        self.found = found or {}
        self._members = members or {}
        self.dirs = dirs or {}
        self.added = []

    def members(self):
        return self._members

    def find_album(self, artist, title, local_path=None):
        return self.found.get((artist, title))

    def add(self, keys):
        self.added.extend(keys)
        return len(keys)


def queued(state, item_id, artist, title, item_type="a"):
    state.queue(BlogItem(item_id, item_type, "u", title, artist))


def test_drain_adds_only_what_is_not_already_in_the_collection(tmp_path):
    state = BlogState(path=tmp_path / "s.json")
    queued(state, 1, "A", "X")
    queued(state, 2, "B", "Y")
    plex = FakePlex(found={("A", "X"): "111", ("B", "Y"): "222"}, members={"222": "Y"})

    to_add, unresolved, unaddable = drain_pending(state, plex, apply=True)

    assert [rk for _, _, rk in to_add] == ["111"]
    assert plex.added == ["111"]
    # Both leave the queue: one was added, the other was already a member.
    assert state.pending == {}
    assert set(state.added) == {1, 2}
    assert not unresolved and not unaddable


def test_drain_leaves_an_unidentified_album_queued(tmp_path):
    state = BlogState(path=tmp_path / "s.json")
    queued(state, 1, "A", "X")
    plex = FakePlex(found={})

    to_add, unresolved, unaddable = drain_pending(state, plex, apply=True)

    assert to_add == [] and unaddable == []
    assert [i for i, _ in unresolved] == [1]
    assert 1 in state.pending  # waits for the next scan


def test_drain_retires_a_standalone_track_instead_of_retrying_it_forever(tmp_path):
    # A track has no album row in Plex and cannot join an album-subtype collection,
    # so it would otherwise be looked up on every run and never resolve.
    state = BlogState(path=tmp_path / "s.json")
    queued(state, 1, "November Fire", "Doom Blues", item_type="t")
    plex = FakePlex(found={})

    to_add, unresolved, unaddable = drain_pending(state, plex, apply=True)

    assert to_add == [] and unresolved == []
    assert [i for i, _, _ in unaddable] == [1]
    assert 1 in state.skipped and 1 not in state.pending
    assert "track" in state.skipped[1]["reason"]


def test_drain_never_looks_up_a_track(tmp_path):
    class Boom(FakePlex):
        def find_album(self, *a, **kw):
            raise AssertionError("must not search Plex for a standalone track")

    state = BlogState(path=tmp_path / "s.json")
    queued(state, 1, "A", "T", item_type="t")
    drain_pending(state, Boom(), apply=False)


def test_drain_writes_nothing_when_not_applying(tmp_path):
    state = BlogState(path=tmp_path / "s.json")
    queued(state, 1, "A", "X")
    queued(state, 2, "B", "Y", item_type="t")
    plex = FakePlex(found={("A", "X"): "111"})

    to_add, _, unaddable = drain_pending(state, plex, apply=False)

    assert [rk for _, _, rk in to_add] == ["111"]
    assert len(unaddable) == 1
    assert plex.added == []
    assert set(state.pending) == {1, 2}  # nothing moved
    assert not (tmp_path / "s.json").exists()


def test_drain_on_an_empty_queue_does_nothing(tmp_path):
    state = BlogState(path=tmp_path / "s.json")
    assert drain_pending(state, FakePlex(), apply=True) == ([], [], [])
