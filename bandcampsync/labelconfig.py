"""
YAML configuration for the free/pay-what-you-want label downloader.

Each label needs its own rules for deciding which albums are wanted. There is no signal
that generalises across labels: Future Avenue tags compilations with an album artist of
"Various Artists" and populates a distinct artist per track, while Tadpole Records uses the
label name as the album artist, leaves every track artist null, and encodes the artist in
the track title as "Artist : Title". Hence a small set of composable predicates rather than
one clever heuristic.
"""

import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import yaml

from .logger import get_logger

log = get_logger("labelconfig")

# Hard ceiling enforced in code regardless of configuration. No rule may ever cause a
# download that costs money.
MAX_PRICE = 0.0

# Labels are inconsistent about this field. The Audio Atelier alone uses "Various Artists",
# "Various Artist" and "Various artist" across its catalogue; UNKNOWN PLEASURES writes it in
# caps; B O G U S COLLECTIVE abbreviates it to "V/A". Match all of those.
# Deliberately does NOT match a bare "VA", which is plausible as a real artist name.
VARIOUS_ARTISTS_REGEX = re.compile(
    r"^\s*(?:various\s+artists?|v\s*[./]\s*a\.?)\s*$", re.IGNORECASE
)

VALID_RULES = {
    "various_artists",
    "track_artists_vary",
    "min_track_artists",
    "title_separator",
    "title_regex",
    "min_tracks",
    "max_tracks",
}


class ConfigError(ValueError):
    pass


@dataclass
class LabelSpec:
    name: str
    url: str
    band_id: int | None = None
    enabled: bool = True
    since: datetime | None = None
    match: dict = field(default_factory=dict)

    @property
    def subdomain_url(self):
        """Base URL of the label, used as the POST target for email_download."""
        return self.url.rstrip("/")


@dataclass
class FreeConfig:
    labels: list = field(default_factory=list)
    media_dir: Path | None = None
    email: str = ""
    country: str = ""
    postcode: str = ""
    media_format: str = "flac"
    request_delay: float = 1.0

    @property
    def enabled_labels(self):
        return [label for label in self.labels if label.enabled]


def _parse_since(value, label_name):
    if value in (None, ""):
        return None
    if isinstance(value, datetime):
        return value.replace(tzinfo=value.tzinfo or timezone.utc)
    if hasattr(value, "year"):  # a datetime.date from the YAML parser
        return datetime(value.year, value.month, value.day, tzinfo=timezone.utc)
    try:
        return datetime.strptime(str(value), "%Y-%m-%d").replace(tzinfo=timezone.utc)
    except ValueError as e:
        raise ConfigError(
            f'Label "{label_name}" has an invalid "since" value {value!r}, '
            f"expected YYYY-MM-DD"
        ) from e


def _validate_match(rules, label_name):
    if not rules:
        return {}
    if not isinstance(rules, dict):
        raise ConfigError(f'Label "{label_name}" has a "match" that is not a mapping')
    unknown = set(rules) - VALID_RULES
    if unknown:
        raise ConfigError(
            f'Label "{label_name}" has unknown match rules: {sorted(unknown)}. '
            f"Valid rules are: {sorted(VALID_RULES)}"
        )
    if "title_regex" in rules:
        try:
            re.compile(rules["title_regex"])
        except re.error as e:
            raise ConfigError(
                f'Label "{label_name}" has an invalid title_regex: {e}'
            ) from e
    return dict(rules)


def load_config(config_path):
    """Load and validate the YAML label configuration."""
    config_path = Path(config_path)
    if not config_path.is_file():
        raise ConfigError(f"Configuration file does not exist: {config_path}")
    with open(config_path, "rt", encoding="utf-8") as f:
        try:
            data = yaml.safe_load(f) or {}
        except yaml.YAMLError as e:
            raise ConfigError(f"Failed to parse {config_path} as YAML: {e}") from e
    if not isinstance(data, dict):
        raise ConfigError(f"{config_path} must contain a YAML mapping at the top level")

    defaults = data.get("defaults") or {}
    raw_labels = data.get("labels")
    if not raw_labels:
        raise ConfigError(f'{config_path} does not define any "labels"')

    labels = []
    seen = set()
    for entry in raw_labels:
        if not isinstance(entry, dict):
            raise ConfigError(f"Each label must be a mapping, got: {entry!r}")
        name = entry.get("name")
        url = entry.get("url")
        if not name:
            raise ConfigError(f'A label entry is missing "name": {entry!r}')
        if not url:
            raise ConfigError(f'Label "{name}" is missing "url"')
        if name in seen:
            raise ConfigError(f'Duplicate label name: "{name}"')
        seen.add(name)
        labels.append(
            LabelSpec(
                name=name,
                url=url,
                band_id=entry.get("band_id"),
                enabled=entry.get("enabled", True),
                since=_parse_since(entry.get("since"), name),
                match=_validate_match(entry.get("match"), name),
            )
        )

    media_dir = defaults.get("media_dir")
    config = FreeConfig(
        labels=labels,
        media_dir=Path(media_dir) if media_dir else None,
        email=defaults.get("email", ""),
        country=defaults.get("country", ""),
        postcode=str(defaults.get("postcode", "")),
        media_format=defaults.get("format", "flac"),
        request_delay=float(defaults.get("request_delay", 1.0)),
    )
    log.info(
        f"Loaded {len(config.labels)} labels from {config_path} "
        f"({len(config.enabled_labels)} enabled)"
    )
    return config


def prefilter(entry, rules):
    """Cheap rule check against a discography entry, before fetching album details.

    band_details returns the album artist and title for the whole catalogue in one
    request, so rules that depend only on those can reject an album without spending a
    per-album request. This is what makes scanning a label with a deep back catalogue
    (Future Avenue has 674 releases) affordable, and it matters much more once dozens of
    labels are configured.

    Only ever returns False when a rule can be evaluated conclusively from the cheap data.
    Rules needing track data (track_artists_vary, title_separator) always pass here and
    are evaluated later by matches().
    """
    if not rules:
        return True, "no rules"
    if rules.get("various_artists"):
        if entry.artist and not VARIOUS_ARTISTS_REGEX.match(entry.artist):
            return False, f"artist is {entry.artist!r}, not Various Artists"
    pattern = rules.get("title_regex")
    if pattern and not re.search(pattern, entry.title):
        return False, f"title does not match {pattern!r}"
    return True, "passed prefilter"


def matches(album, rules):
    """Return (bool, reason) for whether an album satisfies a label's match rules.

    All rules are ANDed. An empty rule set matches everything, which is intended: some
    labels post only a handful of releases and every free one is wanted.
    """
    if not rules:
        return True, "no match rules (all free albums wanted)"

    reasons = []
    if rules.get("various_artists"):
        if not VARIOUS_ARTISTS_REGEX.match(album.artist or ""):
            return False, f"artist is {album.artist!r}, not Various Artists"
        reasons.append("artist is Various Artists")

    if rules.get("track_artists_vary"):
        distinct = album.distinct_track_artists
        if len(distinct) < 2:
            return False, f"only {len(distinct)} distinct track artist(s)"
        reasons.append(f"{len(distinct)} distinct track artists")

    min_artists = rules.get("min_track_artists")
    if min_artists is not None:
        # track_artists_vary only means ">= 2", which matches two-person collaborations
        # and splits as readily as real compilations. Use this to demand a real spread.
        distinct = album.distinct_track_artists
        if len(distinct) < int(min_artists):
            return False, (
                f"{len(distinct)} distinct track artists < "
                f"min_track_artists {min_artists}"
            )
        reasons.append(f"{len(distinct)} >= {min_artists} track artists")

    separator = rules.get("title_separator")
    if separator:
        hits = sum(1 for t in album.track_titles if separator in t)
        if not album.track_titles or hits < max(2, len(album.track_titles) // 2):
            return False, (
                f"only {hits}/{len(album.track_titles)} track titles "
                f"contain {separator!r}"
            )
        reasons.append(f"{hits}/{len(album.track_titles)} titles contain {separator!r}")

    pattern = rules.get("title_regex")
    if pattern:
        if not re.search(pattern, album.title):
            return False, f"title does not match {pattern!r}"
        reasons.append(f"title matches {pattern!r}")

    min_tracks = rules.get("min_tracks")
    if min_tracks is not None and album.num_tracks < int(min_tracks):
        return False, f"{album.num_tracks} tracks < min_tracks {min_tracks}"
    if min_tracks is not None:
        reasons.append(f"{album.num_tracks} >= {min_tracks} tracks")

    max_tracks = rules.get("max_tracks")
    if max_tracks is not None and album.num_tracks > int(max_tracks):
        return False, f"{album.num_tracks} tracks > max_tracks {max_tracks}"

    return True, "; ".join(reasons) or "matched"
