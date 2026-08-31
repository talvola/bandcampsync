"""Download the free albums listed in a Fuzzy Cracklins blog post.

A third tool alongside the collection sync and the label watcher, sharing their machinery
but keyed differently. bandcampfree watches *labels* and discovers releases; this watches
one *blog post*, which hands us an explicit list and asks two questions of each item:

  1. is it still free?  (the blog is written ahead of time and prices change)
  2. do we already have it, under any name, anywhere in the tree?

Everything free-and-missing is downloaded to the root of the media directory, and
everything on the list - downloaded or already present - is queued for membership of a
Plex collection.

Why the Plex membership is a queue rather than part of the download: an album has no Plex
ratingKey until Plex has scanned it, and we deliberately do not drive Plex's scanner. So
each run first drains what previous runs queued (by then scanned), then processes the
post. Same shape as freesync's state.pending.
"""

import html
import json
import re
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

from curl_cffi import requests

from .freedownload import AcquireError, request_download, resolve_and_download
from .labels import BandcampAPI, album_from_details
from .logger import get_logger
from .media import LocalMedia

log = get_logger("blogsync")

# Substack renders each bandcamp embed as a div carrying the oEmbed payload in data-attrs.
# That payload is the whole reason this tool is cheap: it holds the canonical album URL,
# the artist, the title AND the numeric item id (inside embed_url), so no album page has
# to be scraped and no title has to be matched back to an id.
EMBED_RE = re.compile(r'data-attrs="([^"]*)"[^>]*data-component-name="BandcampToDOM"')
EMBED_ID_RE = re.compile(r"/(album|track)=(\d+)")

# Statuses reported per item.
STATUS_DOWNLOADED = "downloaded"
STATUS_HAVE = "have"  # already on disk, nothing fetched
STATUS_PAID = "paid"  # the blog says free, bandcamp disagrees now
STATUS_ERROR = "error"  # could not be checked at all - NOT the same as paid


@dataclass
class BlogItem:
    """One bandcamp embed in a post, before any pricing or dedup."""

    item_id: int
    item_type: str
    url: str
    title: str
    artist: str
    blurb: str = ""

    @property
    def subdomain_url(self):
        parts = urlsplit(self.url)
        return f"{parts.scheme}://{parts.netloc}" if parts.netloc else self.url


@dataclass
class BlogResult:
    """What happened to one item."""

    item: BlogItem
    status: str
    album: object = None
    local_path: Path | None = None
    detail: str = ""
    bytes_downloaded: int = 0


@dataclass
class BlogState:
    """Items awaiting addition to the Plex collection, keyed by bandcamp item id.

    Keyed by int, matching FreeState. A str key silently misses.
    """

    path: Path | None = None
    pending: dict = field(default_factory=dict)
    added: dict = field(default_factory=dict)
    # Items that can never join the collection, with a reason. Same idea as
    # freesync's state.skipped: without it they are retried on every single run.
    skipped: dict = field(default_factory=dict)

    @classmethod
    def load(cls, path):
        path = Path(path)
        if not path.is_file():
            return cls(path=path)
        with open(path, "rt", encoding="utf-8") as f:
            data = json.load(f) or {}
        return cls(
            path=path,
            pending={int(k): v for k, v in (data.get("pending") or {}).items()},
            added={int(k): v for k, v in (data.get("added") or {}).items()},
            skipped={int(k): v for k, v in (data.get("skipped") or {}).items()},
        )

    def save(self):
        if not self.path:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        with open(tmp, "wt", encoding="utf-8") as f:
            json.dump(
                {
                    "pending": {str(k): v for k, v in self.pending.items()},
                    "added": {str(k): v for k, v in self.added.items()},
                    "skipped": {str(k): v for k, v in self.skipped.items()},
                },
                f,
                indent=2,
                ensure_ascii=False,
            )
        tmp.replace(self.path)

    def queue(self, item, local_path=None, album=None):
        """Queue an item for the Plex collection.

        Stores bandcamp's artist/title in preference to the blog's: the Plex search
        matches against tags, which came from the same metadata bandcamp serves.
        """
        if item.item_id in self.added or item.item_id in self.skipped:
            return
        self.pending[item.item_id] = {
            "artist": (album.artist if album else "") or item.artist,
            "title": (album.title if album else "") or item.title,
            "url": item.url,
            "path": str(local_path) if local_path else "",
            # Carried so the collection step can tell a standalone track from an album
            # without re-fetching the post: a track has no album row in Plex and could
            # not join an album-subtype collection anyway.
            "item_type": item.item_type,
        }


def fetch_post(url, timeout=60):
    response = requests.get(url, impersonate="chrome", timeout=timeout)
    if response.status_code != 200:
        raise ValueError(f"Failed to fetch {url}: HTTP {response.status_code}")
    return response.text


def extract_items(page):
    """Every bandcamp album/track embedded in a post, de-duplicated, in document order.

    Substack emits the post body twice (once as HTML, once inside a JSON blob), so every
    embed is seen at least twice; item_id de-duplicates them.
    """
    items, seen = [], set()
    for match in EMBED_RE.finditer(page):
        try:
            data = json.loads(html.unescape(match.group(1)))
        except json.JSONDecodeError:
            continue
        ids = EMBED_ID_RE.search(data.get("embed_url") or "")
        if not ids:
            # A bandcamp embed pointing at an artist page rather than a release. Nothing
            # to download, and no id to key it by.
            continue
        item_id = int(ids.group(2))
        if item_id in seen:
            continue
        seen.add(item_id)
        artist = data.get("author") or ""
        items.append(
            BlogItem(
                item_id=item_id,
                item_type="a" if ids.group(1) == "album" else "t",
                url=data.get("url") or "",
                title=_strip_by_suffix(data.get("title") or "", artist),
                artist=artist,
                blurb=data.get("description") or "",
            )
        )
    return items


def _strip_by_suffix(title, artist):
    """Turn oEmbed's "Cerium, by The Moondig" back into "Cerium".

    Substack stores the composite form. Left in place it poisons every name comparison
    downstream - the local index and the Plex title search both look for the bare title.

    The exact ", by <author>" suffix is tried first, then the last ", by " as a fallback:
    the two fields are not always the same string. Nosferator's September release carries
    a Latin author ("Nosferator") and a Cyrillic suffix ("by Носфератор"), so an exact
    match alone leaves the credit stuck on the title. Bandcamp always generates this
    field as "<title>, by <artist>", so the final separator is reliable; a title that
    genuinely ends in ", by ..." is the rare loss and still downloads fine, since the
    real title comes from the API.

    Both branches keep the original when stripping would leave nothing: an empty title
    matches no directory and every Plex search, so it is worse than the composite.
    """
    suffix = f", by {artist}"
    if artist and title.endswith(suffix) and title[: -len(suffix)].strip():
        return title[: -len(suffix)].strip()
    head, sep, _ = title.rpartition(", by ")
    if sep and head.strip():
        return head.strip()
    return title


def _norm(value):
    """Fold a name for comparison: case, accents and punctuation all vary on disk."""
    value = unicodedata.normalize("NFKD", value or "").casefold()
    return re.sub(r"[^a-z0-9Ѐ-ӿ]+", "", value)


class LocalIndex:
    """Where a blog item might already exist on disk.

    Two independent keys, because neither alone is enough. The id files are exact but
    thousands of directories lack one (hand-downloaded back catalogue never had them).
    The names are lenient but miss re-orderings and date suffixes. Both are cheap off a
    single depth-2 pass, which is the only affordable shape over SMB - never walk the
    whole tree.
    """

    def __init__(self, media_dir):
        self.media_dir = Path(media_dir)
        self.by_id = {}
        self.by_name = {}
        self._build()

    def _record(self, path):
        self.by_name.setdefault(_norm(path.name), path)
        id_file = path / LocalMedia.ITEM_INDEX_FILENAME
        try:
            raw = id_file.read_text(encoding="utf-8").strip()
        except (OSError, ValueError):
            return
        # "None" really occurs on disk - an older writer stringified a missing id.
        if raw.isdigit():
            self.by_id.setdefault(int(raw), path)

    def _build(self):
        dirs = 0
        for entry in self.media_dir.iterdir():
            if not entry.is_dir() or entry.name.startswith("."):
                continue
            dirs += 1
            self._record(entry)
            # A label grouping directory holds album directories one level down. Album
            # directories are named "Artist - Album", so the separator distinguishes
            # them cheaply enough to avoid descending into thousands of leaf dirs.
            if " - " in entry.name:
                continue
            try:
                for sub in entry.iterdir():
                    if sub.is_dir():
                        self._record(sub)
            except OSError as e:
                log.warning(f"Could not list {entry}: {e}")
        log.info(
            f"Indexed {len(self.by_name)} directories under {self.media_dir} "
            f"({dirs} top level, {len(self.by_id)} with an item id file)"
        )

    def find(self, item, album=None):
        """Return the local directory holding this item, or None.

        Exact id first. Falls back to names, trying the canonical "Artist - Album" form
        before a containment test, so a specific match always beats a loose one.

        album, when given, supplies bandcamp's own artist and title, which are what the
        directory was named from. The blog's credit is a human's and can differ.
        """
        hit = self.by_id.get(item.item_id)
        if hit:
            return hit
        candidates = [(item.artist, item.title)]
        if album is not None:
            candidates.insert(0, (album.artist, album.title))
        for raw_artist, raw_title in candidates:
            artist, title = _norm(raw_artist), _norm(raw_title)
            if not artist or not title:
                continue
            for key in (_norm(f"{raw_artist} - {raw_title}"), artist + title):
                if key in self.by_name:
                    return self.by_name[key]
            for key, path in self.by_name.items():
                if artist in key and title in key:
                    return path
        return None


def classify_items(items, api, label_name="", on_progress=None):
    """Price every item through the same predicate bandcampfree uses.

    Yields (item, album, error) triples. album is None when error is set.
    """
    band_ids = {}
    for item in items:
        host = urlsplit(item.url).netloc
        album = error = None
        try:
            if host not in band_ids:
                band_ids[host] = api.resolve_band_id(item.url)
            details = api.tralbum_details(band_ids[host], item.item_id, item.item_type)
            # Not optional. The API answers 200 with {"error": true} for a bad band_id,
            # and album_from_details turns that into price=0.0/num_tracks=0, which reads
            # as NOT FREE. Without this an unreachable album is reported as "now paid",
            # which is exactly the signal this tool exists to produce.
            if details.get("error"):
                error = f"bandcamp API error: {details.get('error_message')}"
            else:
                album = album_from_details(details, label_name)
        except Exception as e:
            error = f"{type(e).__name__}: {e}"
        if on_progress:
            on_progress(item, album, error)
        yield item, album, error


def download_item(item, album, config, gmail_reader=None, temp_dir=None):
    """Acquire one free album, placing it at the root of the media directory.

    Returns (local_path, bytes). The album's own subdomain is the endpoint - a blog list
    is a list of unrelated artists, so there is no label URL to fall back on.
    """
    url = request_download(
        item.subdomain_url, album, config.email, config.country, config.postcode
    )
    if url is None:
        reader = gmail_reader() if callable(gmail_reader) else gmail_reader
        if reader is None:
            raise AcquireError(
                f"{album.title!r} requires an emailed download link but Gmail is not "
                f"configured. Run with --gmail-auth first."
            )
        url = reader.wait_for_link(album.item_id)
    stats = {}
    local_path = resolve_and_download(
        url,
        album,
        album.artist or item.artist,
        config.media_dir,
        media_format=config.media_format,
        temp_dir=temp_dir,
        stats=stats,
        root_level=True,
    )
    return local_path, stats.get("bytes", 0)


def sync_post(
    post_url,
    config,
    state,
    report_only=False,
    gmail_reader=None,
    temp_dir=None,
    api=None,
    index=None,
):
    """Process one blog post. Returns a list of BlogResult, in post order."""
    log.info(f"Fetching {post_url}")
    items = extract_items(fetch_post(post_url))
    log.info(f"Found {len(items)} bandcamp item(s) in the post")
    if not items:
        return []

    api = api or BandcampAPI(delay=config.request_delay)
    index = index if index is not None else LocalIndex(config.media_dir)

    results = []
    for item, album, error in classify_items(items, api):
        if error:
            log.error(f"Could not check {item.artist} / {item.title}: {error}")
            results.append(BlogResult(item, STATUS_ERROR, detail=error))
            continue

        existing = index.find(item, album)
        if existing:
            # Still queued for the collection: having it on disk is exactly the case
            # where it belongs in the collection but nothing would otherwise add it.
            artist, title = _display(item, album)
            log.info(f"Already have {artist} / {title} at {existing}")
            results.append(BlogResult(item, STATUS_HAVE, album=album, local_path=existing))
            state.queue(item, existing, album)
            continue

        if not album.is_free:
            artist, title = _display(item, album)
            log.warning(
                f"No longer free: {artist} / {title} ({album.price}{album.currency})"
            )
            results.append(
                BlogResult(
                    item,
                    STATUS_PAID,
                    album=album,
                    detail=f"{album.price}{album.currency}",
                )
            )
            continue

        if report_only:
            results.append(BlogResult(item, STATUS_DOWNLOADED, album=album))
            continue

        try:
            local_path, size = download_item(item, album, config, gmail_reader, temp_dir)
        except Exception as e:
            artist, title = _display(item, album)
            log.error(f"Failed to download {artist} / {title}: {e}")
            results.append(BlogResult(item, STATUS_ERROR, album=album, detail=str(e)))
            continue
        artist, title = _display(item, album)
        log.info(f"Downloaded {artist} / {title} to {local_path}")
        results.append(
            BlogResult(
                item,
                STATUS_DOWNLOADED,
                album=album,
                local_path=local_path,
                bytes_downloaded=size,
            )
        )
        state.queue(item, local_path, album)

    if not report_only:
        state.save()
    return results


def _display(item, album=None):
    """Display names, preferring bandcamp's over the blog's.

    The blog credits by hand and transliterates - Nosferator's September release is
    credited in Latin there and in Cyrillic by bandcamp, and it is bandcamp's form that
    names the directory and fills the Plex tags. Quoting the blog's would send a reader
    looking for something that is not on disk under that name.
    """
    if album is not None:
        return album.artist or item.artist, album.title or item.title
    return item.artist, item.title


def _names(result):
    return _display(result.item, result.album)


def format_report(post_url, results, pending_count=0):
    """Human-readable summary. The paid list is the point of the whole exercise."""
    by_status = {}
    for result in results:
        by_status.setdefault(result.status, []).append(result)

    downloaded = by_status.get(STATUS_DOWNLOADED, [])
    have = by_status.get(STATUS_HAVE, [])
    paid = by_status.get(STATUS_PAID, [])
    errors = by_status.get(STATUS_ERROR, [])

    lines = [
        "Fuzzy Cracklins blog sync",
        f"  {post_url}",
        "",
        f"  Items in post:   {len(results)}",
        f"  Downloaded:      {len(downloaded)}",
        f"  Already on disk: {len(have)}",
        f"  No longer free:  {len(paid)}",
        f"  Could not check: {len(errors)}",
        f"  Queued for Plex: {pending_count}",
    ]
    total = sum(r.bytes_downloaded for r in downloaded)
    if total:
        lines.append(f"  Downloaded size: {total / (1024 ** 3):.2f} GB")

    if paid:
        lines += ["", "NO LONGER FREE (buy by hand, or skip):"]
        for r in paid:
            artist, title = _names(r)
            lines.append(f"  {artist} / {title}  [{r.detail}]")
            lines.append(f"      {r.item.url}")
    if errors:
        lines += ["", "COULD NOT CHECK (not necessarily paid - re-run):"]
        for r in errors:
            artist, title = _names(r)
            lines.append(f"  {artist} / {title}")
            lines.append(f"      {r.detail}")
    if downloaded:
        lines += ["", "DOWNLOADED:"]
        for r in downloaded:
            artist, title = _names(r)
            where = f"  -> {r.local_path}" if r.local_path else ""
            lines.append(f"  {artist} / {title}{where}")
    if have:
        lines += ["", "ALREADY ON DISK (queued for the collection only):"]
        for r in have:
            artist, title = _names(r)
            lines.append(f"  {artist} / {title}")
            lines.append(f"      {r.local_path}")
    return "\n".join(lines)
