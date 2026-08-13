"""
Discovery and classification of free / pay-what-you-want albums on record label
bandcamp.com pages.

This is the counterpart to sync.py: rather than syncing items already purchased into an
account collection, it watches label pages for newly posted free albums. Claiming an album
at a price of 0 does not add it to the account collection (bandcamp requires a non-zero
minimum for that), so free albums must be discovered, requested and downloaded separately.

Everything needed is available from bandcamp's undocumented mobile API, which returns clean
JSON and lists a label's entire discography in a single request. No HTML scraping is needed.
"""

import json
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from urllib.parse import urlsplit

from curl_cffi import requests

from .logger import get_logger

log = get_logger("labels")


API_BASE = "https://bandcamp.com/api/mobile/24"
# Matches the data-band attribute on any page of a label, used to resolve a band_id from
# a label URL the first time it is seen.
BAND_ID_REGEX = re.compile(r'data-band="[^"]*?&quot;id&quot;:\s*(\d+)')
BAND_ID_REGEX_PLAIN = re.compile(r'data-band=\'[^\']*?"id":\s*(\d+)')


class LabelError(ValueError):
    pass


class RateLimited(LabelError):
    """Raised when bandcamp.com rate limits us past the retry budget.

    Distinct from LabelError so a scan can abort cleanly rather than marking hundreds of
    albums as individually failed.
    """


@dataclass
class FreeAlbum:
    """A single album on a label page, with everything needed to classify it."""

    item_id: int
    title: str
    artist: str
    url: str
    price: float
    is_set_price: bool
    require_email: bool
    free_download: bool
    num_tracks: int
    # bandcamp's own code: "a" for an album, "t" for a standalone track. Single tracks
    # are released free on their own often enough to matter, and every request that
    # touches an item has to name its type correctly.
    item_type: str = "a"
    # Default True: absent means the API did not say, and the common case is downloadable.
    has_digital_download: bool = True
    currency: str = ""
    release_date: datetime | None = None
    track_artists: list = field(default_factory=list)
    track_titles: list = field(default_factory=list)
    label_name: str = ""

    @property
    def is_free(self):
        """True when the album can actually be obtained for 0.

        Gate on price and is_set_price. Note that `free_download` maps to bandcamp's
        download_pref == 1 ("truly free, instant download") and is False for the far more
        common name-your-price-with-no-minimum case, so it must NOT be used to decide
        whether 0 is allowed.

        has_digital_download matters because bandcamp reports price=None for items that
        are not separately purchasable at all - individual tracks of a compilation, for
        instance. Those would otherwise look free (None becomes 0.0) and every download
        attempt would fail.
        """
        return self.price == 0.0 and not self.is_set_price and self.has_digital_download

    @property
    def distinct_track_artists(self):
        return {a for a in self.track_artists if a}

    def __repr__(self):
        return (
            f"<FreeAlbum {self.item_id} {self.artist!r} - {self.title!r} "
            f"price={self.price} free={self.is_free} tracks={self.num_tracks}>"
        )


class BandcampAPI:
    """Thin client for bandcamp's mobile API.

    A delay is applied between requests; this is an undocumented API and a run across many
    labels can otherwise issue a large number of calls in a short window.
    """

    def __init__(self, delay=1.0, timeout=60, max_retries=5, backoff=4.0):
        self.session = requests.Session(impersonate="chrome")
        self.delay = delay
        self.timeout = timeout
        self.max_retries = max_retries
        self.backoff = backoff
        self._last_request = 0.0
        # Consecutive rate-limited responses, used to give up rather than hammer the API.
        self.consecutive_429 = 0

    def _throttle(self):
        if self.delay <= 0:
            return
        elapsed = time.monotonic() - self._last_request
        if elapsed < self.delay:
            time.sleep(self.delay - elapsed)
        self._last_request = time.monotonic()

    def _get(self, url, is_json=True):
        """GET with backoff on HTTP 429.

        A full first scan of a large label issues hundreds of requests and bandcamp will
        rate limit; 429 must be waited out rather than treated as a per-album failure,
        otherwise a scan silently reports free albums as errors.
        """
        response = None
        for attempt in range(self.max_retries + 1):
            self._throttle()
            try:
                response = self.session.get(url, timeout=self.timeout)
            except Exception as e:
                raise LabelError(f"Failed to make HTTP request to {url}: {e}") from e
            if response.status_code != 429:
                self.consecutive_429 = 0
                break
            self.consecutive_429 += 1
            if attempt >= self.max_retries:
                raise RateLimited(
                    f"Rate limited by bandcamp.com after {attempt + 1} attempts: {url}"
                )
            wait = self._retry_after(response) or self.backoff * (2**attempt)
            log.warning(
                f"Rate limited (429), waiting {wait:.0f}s "
                f"(attempt {attempt + 1}/{self.max_retries})"
            )
            time.sleep(wait)

        if response.status_code != 200:
            raise LabelError(
                f"Failed to make HTTP request to {url}: "
                f"unexpected status code: {response.status_code}"
            )
        if not is_json:
            return response.text

        try:
            return json.loads(response.text)
        except Exception as e:
            raise LabelError(f"Failed to parse response from {url} as JSON: {e}") from e

    @staticmethod
    def _retry_after(response):
        """Seconds to wait per a Retry-After header, if the server sent a usable one."""
        value = response.headers.get("Retry-After") if response.headers else None
        if not value:
            return None
        try:
            return max(0.0, float(value))
        except (TypeError, ValueError):
            return None

    def resolve_band_id(self, label_url):
        """Resolve a label's numeric band_id from its bandcamp URL."""
        parts = urlsplit(label_url)
        url = f"{parts.scheme or 'https'}://{parts.netloc}/music"
        body = self._get(url, is_json=False)
        for regex in (BAND_ID_REGEX, BAND_ID_REGEX_PLAIN):
            match = regex.search(body)
            if match:
                return int(match.group(1))
        raise LabelError(
            f"Failed to locate a band id in {url}, the page may not be a label page or "
            f"bandcamp.com may have changed their markup"
        )

    def band_details(self, band_id):
        """Return a label's full discography. Unpaginated, one request, no prices."""
        return self._get(f"{API_BASE}/band_details?band_id={band_id}")

    def tralbum_details(self, band_id, item_id, item_type="a"):
        """Return full metadata for one album or track, including price and tracks."""
        return self._get(
            f"{API_BASE}/tralbum_details?band_id={band_id}"
            f"&tralbum_type={item_type}&tralbum_id={item_id}"
        )


EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)


def _parse_release_date(value):
    """band_details gives an RFC-1123 string, tralbum_details a unix timestamp."""
    if value in (None, ""):
        return None
    if isinstance(value, (int, float)):
        # Not datetime.fromtimestamp(): it raises OSError on Windows for negative
        # timestamps, and labels with pre-1970 back catalogue really do have them
        # (Projekt Records crashed a full scan this way). Arithmetic on the epoch works
        # for negatives on every platform.
        try:
            return EPOCH + timedelta(seconds=float(value))
        except (OverflowError, ValueError, OSError):
            log.warning(f"Out-of-range release timestamp: {value!r}")
            return None
    for fmt in ("%d %b %Y %H:%M:%S %Z", "%d %b %Y %H:%M:%S"):
        try:
            return datetime.strptime(str(value).strip(), fmt).replace(
                tzinfo=timezone.utc
            )
        except ValueError:
            continue
    log.warning(f"Could not parse release date: {value!r}")
    return None


def _digital_price(details):
    """The price of the DIGITAL download, which is not always the `price` field.

    tralbum_details reports the album's minimum_price as `price`, and that describes the
    cheapest way to get the release, physical included. When download_pref == 1 (which
    the API exposes as free_download) the digital download is free regardless of what
    that number says: My Proud Mountain's "15 Songs 15 Years" reports price=9.0 EUR
    because a sold-out physical package set a EUR 9 minimum, while its album page carries
    download_pref=1 and freeDownloadPage=true and the download costs nothing.

    Normalising here rather than in is_free is deliberate: freesync also enforces
    `album.price > MAX_PRICE` as a hard ceiling against ever paying for anything, and
    that ceiling must keep comparing against the real digital price.

    The converse trap still stands and is why free_download can only ever ADD a way of
    being free, never be required: it is False for ordinary name-your-price-at-0 albums,
    which are the common case.
    """
    price = float(details.get("price") or 0.0)
    if details.get("free_download") and details.get("has_digital_download", True):
        return 0.0
    return price


def album_from_details(details, label_name=""):
    """Build a FreeAlbum from a tralbum_details API response."""
    tracks = details.get("tracks") or []
    return FreeAlbum(
        item_id=details.get("id"),
        title=details.get("title") or "",
        artist=details.get("tralbum_artist") or "",
        url=details.get("bandcamp_url") or "",
        price=_digital_price(details),
        is_set_price=bool(details.get("is_set_price")),
        require_email=bool(details.get("require_email")),
        free_download=bool(details.get("free_download")),
        item_type=(details.get("type") or "a"),
        has_digital_download=bool(details.get("has_digital_download", True)),
        num_tracks=int(details.get("num_downloadable_tracks") or len(tracks)),
        currency=details.get("currency") or "",
        release_date=_parse_release_date(details.get("release_date")),
        track_artists=[t.get("band_name") for t in tracks],
        track_titles=[t.get("title") or "" for t in tracks],
        label_name=label_name or (details.get("label") or ""),
    )


@dataclass
class DiscoEntry:
    """One entry from a label's discography listing.

    Cheap: comes from the single band_details request. Carries no price, but does carry
    the album artist and title, which is enough to skip many albums without spending a
    per-album request.
    """

    item_id: int
    item_type: str
    title: str
    artist: str = ""
    release_date: datetime | None = None


def list_discography(api, band_id):
    """Return a label's discography as DiscoEntry objects, newest first. One request."""
    data = api.band_details(band_id)
    out = []
    for entry in data.get("discography") or []:
        out.append(
            DiscoEntry(
                item_id=entry.get("item_id"),
                item_type="a" if entry.get("item_type") == "album" else "t",
                title=entry.get("title") or "",
                artist=entry.get("artist_name") or entry.get("band_name") or "",
                release_date=_parse_release_date(entry.get("release_date")),
            )
        )
    return out
