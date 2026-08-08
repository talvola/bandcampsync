import pytest

from bandcampsync.labels import FreeAlbum
from bandcampsync.labelconfig import ConfigError, load_config, matches


def _album(**kwargs):
    defaults = dict(
        item_id=1,
        title="Atmospheric Progressive #021 (Free Download)",
        artist="Various Artists",
        url="https://example.bandcamp.com/album/test",
        price=0.0,
        is_set_price=False,
        require_email=False,
        free_download=False,
        num_tracks=30,
    )
    defaults.update(kwargs)
    return FreeAlbum(**defaults)


# --- Future Avenue shape: album artist "Various Artists", per-track artists populated ---


def test_various_artists_matches():
    ok, _ = matches(_album(), {"various_artists": True})
    assert ok is True


def test_various_artists_tolerates_real_world_spellings():
    """The Audio Atelier uses all three of these across its own catalogue."""
    for spelling in (
        "Various Artists",
        "Various Artist",
        "Various artist",
        "various artists",
        "  Various Artists  ",
    ):
        ok, _ = matches(_album(artist=spelling), {"various_artists": True})
        assert ok is True, spelling


def test_various_artists_matches_common_abbreviations():
    """B O G U S COLLECTIVE credits its compilations to "V/A"."""
    for spelling in ("V/A", "v/a", "V.A.", "V / A", "V.A"):
        ok, _ = matches(_album(artist=spelling), {"various_artists": True})
        assert ok is True, spelling


def test_various_artists_matches_non_english_credits():
    """South America Avenue credited Progressive Pulse 012 to "Varios Artistas" while the
    rest of the same series says "Various Artists"; the free one was silently skipped."""
    for spelling in (
        "Varios Artistas",
        "varios artistas",
        "Vários Artistas",
        "Varios Artista",
        "VV.AA.",
        "VV. AA.",
        "vvaa",
        "AA.VV.",
        "AAVV",
    ):
        ok, _ = matches(_album(artist=spelling), {"various_artists": True})
        assert ok is True, spelling


def test_various_artists_tolerates_trailing_punctuation():
    for spelling in ("VARIOUS ARTISTS!", "Various Artists.", "V/A!"):
        ok, _ = matches(_album(artist=spelling), {"various_artists": True})
        assert ok is True, spelling


def test_various_artists_does_not_match_a_bare_va():
    """ "VA" on its own is plausible as a real artist name, so it must not match."""
    ok, _ = matches(_album(artist="VA"), {"various_artists": True})
    assert ok is False


def test_various_artists_still_rejects_near_misses():
    for spelling in (
        "Various Artists Collective",
        "The Various Artists",
        "Various",
        # Widening for Varios Artistas must not start swallowing real band names that
        # merely begin the same way.
        "Varios Artistas Collective",
        "Aavv Sound System",
        "Vario",
    ):
        ok, _ = matches(_album(artist=spelling), {"various_artists": True})
        assert ok is False, spelling


def test_various_artists_rejects_single_artist():
    ok, reason = matches(_album(artist="Magros"), {"various_artists": True})
    assert ok is False
    assert "Magros" in reason


def test_track_artists_vary_matches():
    album = _album(track_artists=["A", "B", "C"])
    ok, _ = matches(album, {"track_artists_vary": True})
    assert ok is True


def test_track_artists_vary_rejects_all_null():
    """The Tadpole Records shape: every track artist is null."""
    album = _album(track_artists=[None] * 30)
    ok, reason = matches(album, {"track_artists_vary": True})
    assert ok is False
    assert "0 distinct" in reason


# --- Tadpole shape: artist is the label, artist encoded in the track title ---


def test_title_separator_matches_tadpole_shape():
    album = _album(
        artist="Tadpole Records",
        track_titles=[
            "Haest : This Tired Boat Is Sinking",
            "Vanilla Giver : Light Up Your Life",
            "Someone : A Song",
            "Another : Another Song",
        ],
    )
    ok, _ = matches(album, {"title_separator": " : "})
    assert ok is True


def test_title_separator_rejects_when_few_titles_match():
    album = _album(track_titles=["No separator here", "Nor here", "Only : one", "Nope"])
    ok, reason = matches(album, {"title_separator": " : "})
    assert ok is False
    assert "1/4" in reason


def test_future_avenue_rules_reject_tadpole_album():
    """The two labels share no signal; rules must not cross-match."""
    tadpole = _album(artist="Tadpole Records", track_artists=[None] * 30)
    ok, _ = matches(tadpole, {"various_artists": True, "track_artists_vary": True})
    assert ok is False


def test_min_track_artists_rejects_collaborations():
    """Real Children of Vapor cases: two- and three-person collaborations that
    track_artists_vary alone wrongly accepted as compilations."""
    collab = _album(
        artist="Ecco City & Saturn ARS", track_artists=["Ecco City", "Saturn ARS"]
    )
    ok, reason = matches(collab, {"track_artists_vary": True})
    assert ok is True  # the loose rule accepts it
    ok, reason = matches(collab, {"min_track_artists": 5})
    assert ok is False
    assert "min_track_artists" in reason


def test_min_track_artists_accepts_real_compilation():
    comp = _album(track_artists=[f"Artist {i}" for i in range(30)])
    ok, _ = matches(comp, {"min_track_artists": 5})
    assert ok is True


def test_min_track_artists_ignores_null_artists():
    album = _album(track_artists=[None] * 30)
    ok, _ = matches(album, {"min_track_artists": 5})
    assert ok is False


def test_min_tracks_rejects_short_release():
    ok, reason = matches(_album(num_tracks=2), {"min_tracks": 8})
    assert ok is False
    assert "min_tracks" in reason


def test_max_tracks_rejects_long_release():
    ok, _ = matches(_album(num_tracks=50), {"max_tracks": 40})
    assert ok is False


def test_title_regex():
    ok, _ = matches(_album(), {"title_regex": r"(?i)atmospheric"})
    assert ok is True
    ok, _ = matches(_album(), {"title_regex": r"(?i)sampler"})
    assert ok is False


def test_empty_rules_match_everything():
    ok, reason = matches(_album(artist="Anyone"), {})
    assert ok is True
    assert "all free albums" in reason


def test_rules_are_anded():
    album = _album(track_artists=["A", "B"], num_tracks=3)
    ok, _ = matches(album, {"track_artists_vary": True, "min_tracks": 8})
    assert ok is False


# --- config loading ---


def _write(tmp_path, text):
    path = tmp_path / "labels.yaml"
    path.write_text(text, encoding="utf-8")
    return path


def test_load_minimal_config(tmp_path):
    path = _write(
        tmp_path,
        """
defaults:
  media_dir: "/tmp/media"
labels:
  - name: Future Avenue
    url: https://futureavenuelabel.bandcamp.com/
    match:
      various_artists: true
""",
    )
    config = load_config(path)
    assert len(config.labels) == 1
    assert config.labels[0].name == "Future Avenue"
    assert config.labels[0].match == {"various_artists": True}
    assert config.enabled_labels == config.labels


def test_disabled_labels_excluded(tmp_path):
    path = _write(
        tmp_path,
        """
labels:
  - name: A
    url: https://a.bandcamp.com/
  - name: B
    url: https://b.bandcamp.com/
    enabled: false
""",
    )
    config = load_config(path)
    assert len(config.labels) == 2
    assert [label.name for label in config.enabled_labels] == ["A"]


def test_unknown_match_rule_rejected(tmp_path):
    path = _write(
        tmp_path,
        """
labels:
  - name: A
    url: https://a.bandcamp.com/
    match:
      various_artits: true
""",
    )
    with pytest.raises(ConfigError, match="unknown match rules"):
        load_config(path)


def test_invalid_regex_rejected(tmp_path):
    path = _write(
        tmp_path,
        """
labels:
  - name: A
    url: https://a.bandcamp.com/
    match:
      title_regex: "([unclosed"
""",
    )
    with pytest.raises(ConfigError, match="invalid title_regex"):
        load_config(path)


def test_missing_url_rejected(tmp_path):
    path = _write(
        tmp_path,
        """
labels:
  - name: A
""",
    )
    with pytest.raises(ConfigError, match="missing"):
        load_config(path)


def test_duplicate_label_rejected(tmp_path):
    path = _write(
        tmp_path,
        """
labels:
  - name: A
    url: https://a.bandcamp.com/
  - name: A
    url: https://a2.bandcamp.com/
""",
    )
    with pytest.raises(ConfigError, match="Duplicate"):
        load_config(path)


def test_no_labels_rejected(tmp_path):
    path = _write(tmp_path, "defaults:\n  media_dir: /tmp\n")
    with pytest.raises(ConfigError, match="does not define any"):
        load_config(path)


def test_since_parsed(tmp_path):
    path = _write(
        tmp_path,
        """
labels:
  - name: A
    url: https://a.bandcamp.com/
    since: 2024-01-01
""",
    )
    config = load_config(path)
    assert config.labels[0].since.year == 2024
