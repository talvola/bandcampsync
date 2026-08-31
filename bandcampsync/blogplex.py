"""Add blog-sourced albums to a Plex collection.

Deliberately separate from the download: an album has no Plex ratingKey until Plex has
scanned it, and this tool does not drive Plex's scanner. So downloads queue into
BlogState.pending, and this drains whatever Plex has caught up on since. Anything not yet
scanned simply stays queued for the next run.

The Fuzzy Cracklins collection cannot be maintained by folder_collection_sync.py the way
the other seven collections are: its members are scattered across the media tree (349 of
409 at the root, the rest inside label directories), so no single folder contains it.
Membership is therefore resolved per album.

Credentials come from plex-mcp-server's .env (PLEX_URL / PLEX_TOKEN), the same file that
tool and the MCP server use, so there is only one place to rotate a token.
"""

import os
import posixpath
from pathlib import Path

import requests

from .logger import get_logger

log = get_logger("blogplex")

DEFAULT_ENV = Path(r"C:\projects\plex-mcp-server\.env")
DEFAULT_SECTION = 1  # Music. Same default folder_collection_sync.py uses.
FUZZY_CRACKLINS_COLLECTION = 472199

# The same files under two names: N: is the SMB mount of \\192.168.1.64\Music, which the
# server itself serves from /share/Music. Needed to compare a path we downloaded to
# against the path Plex reports for the same file.
LOCAL_MEDIA_ROOT = "N:/Bandcamp (FLAC)"
PLEX_MEDIA_ROOT = "/share/Music/Bandcamp (FLAC)"

# Plex rejects a whole PUT with HTTP 400 if any ratingKey in it is dead, adding nothing
# at all, so batches stay small enough that one re-scanned album cannot cost much.
BATCH = 50


class PlexError(ValueError):
    pass


def load_plex_env(env_path=None):
    """Read PLEX_URL/PLEX_TOKEN. Environment wins, then the .env file."""
    url, token = os.environ.get("PLEX_URL"), os.environ.get("PLEX_TOKEN")
    if url and token:
        return url.rstrip("/"), token
    env_path = Path(env_path or DEFAULT_ENV)
    values = {}
    if env_path.is_file():
        for line in env_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            values[key.strip()] = value.strip().strip("\"'")
    url = url or values.get("PLEX_URL", "")
    token = token or values.get("PLEX_TOKEN", "")
    if not url or not token:
        raise PlexError(
            f"PLEX_URL and PLEX_TOKEN must be set in the environment or in {env_path}"
        )
    return url.rstrip("/"), token


class PlexCollection:
    def __init__(self, collection_id, env_path=None, section=DEFAULT_SECTION, timeout=60):
        self.base, self._token = load_plex_env(env_path)
        self.collection_id = int(collection_id)
        self.section = section
        self.timeout = timeout
        self.session = requests.Session()
        self.session.headers.update(
            {"X-Plex-Token": self._token, "Accept": "application/json"}
        )

    def _get(self, path, **params):
        response = self.session.get(
            f"{self.base}{path}", params=params or None, timeout=self.timeout
        )
        response.raise_for_status()
        return response.json()["MediaContainer"]

    def machine_id(self):
        response = self.session.get(f"{self.base}/identity", timeout=30)
        try:
            return response.json()["MediaContainer"]["machineIdentifier"]
        except Exception:
            import re

            match = re.search(r'machineIdentifier="([^"]+)"', response.text)
            if not match:
                raise PlexError(f"Could not parse /identity: {response.text[:200]}")
            return match.group(1)

    def members(self):
        """ratingKey -> title for everything currently in the collection."""
        keys, start = {}, 0
        while True:
            container = self._get(
                f"/library/collections/{self.collection_id}/children",
                **{"X-Plex-Container-Start": start, "X-Plex-Container-Size": 300},
            )
            meta = container.get("Metadata", [])
            for entry in meta:
                keys[entry["ratingKey"]] = entry.get("title")
            start += len(meta)
            if not meta or start >= container.get("totalSize", len(keys)):
                break
        return keys

    @staticmethod
    def local_to_plex_path(local_path):
        """Rewrite a local media path to the form Plex reports, or '' if it is elsewhere."""
        if not local_path:
            return ""
        normalised = str(local_path).replace("\\", "/").rstrip("/")
        root = LOCAL_MEDIA_ROOT.replace("\\", "/")
        if normalised.casefold().startswith(root.casefold()):
            return PLEX_MEDIA_ROOT + normalised[len(root) :]
        return ""

    def album_dir(self, rating_key):
        """The directory Plex's own files for this album live in, or ''."""
        try:
            container = self._get(f"/library/metadata/{rating_key}/children")
        except requests.RequestException:
            return ""
        for track in container.get("Metadata", []):
            for media in track.get("Media", []):
                for part in media.get("Part", []):
                    f = (part.get("file") or "").replace("\\", "/")
                    if f:
                        return posixpath.dirname(f)
        return ""

    def find_album(self, artist, title, local_path=None):
        """Resolve one album to a ratingKey, or None if it cannot be identified.

        Plex indexes what the *tags* say, which is the point of asking it at all - it sees
        an album whatever its directory is called and wherever it sits in the tree. But it
        also means the tags routinely disagree with bandcamp: Weedian's June 2026
        compilation is credited to bandcamp as "WEEDIAN / The Best Releases of June 2026"
        and tagged in Plex as "Various Artists / Weedian: The Best Releases of June 2026".
        So an exact artist+title agreement is the best case, not the normal one.

        Matching therefore goes strongest-evidence first:
          1. the candidate's files are in the directory we downloaded to - decisive
          2. artist and title both agree exactly
          3. a single candidate whose title contains ours, or vice versa

        Returns None when the search finds nothing, which is NOT always "not scanned yet":
        a standalone track has no album row at all, so it can never resolve here (and could
        not join an album-subtype collection even if it did).
        """
        from .blogsync import _norm

        try:
            container = self._get(
                f"/library/sections/{self.section}/all", type=9, title=title
            )
        except requests.RequestException as e:
            log.warning(f"Plex search failed for {artist} / {title}: {e}")
            return None
        candidates = container.get("Metadata", [])
        if not candidates:
            return None

        # 1. Path agreement. Costs one request per candidate, but only ever runs for the
        # handful a title search returned, and it is the only check that cannot be fooled
        # by a retagged compilation.
        wanted_dir = self.local_to_plex_path(local_path)
        if wanted_dir:
            for entry in candidates:
                if self.album_dir(entry["ratingKey"]).casefold() == wanted_dir.casefold():
                    return entry["ratingKey"]

        want_artist, want_title = _norm(artist), _norm(title)
        for entry in candidates:
            if (
                _norm(entry.get("parentTitle", "")) == want_artist
                and _norm(entry.get("title", "")) == want_title
            ):
                return entry["ratingKey"]

        # 3. One candidate and the titles are a containment away from each other. Requiring
        # uniqueness is what keeps this safe: a label prefix is fine, a second album with a
        # similar name is not, and that falls through to None rather than guessing.
        if len(candidates) == 1 and want_title:
            got = _norm(candidates[0].get("title", ""))
            if got and (want_title in got or got in want_title):
                return candidates[0]["ratingKey"]
        return None

    def add(self, rating_keys):
        """Add ratingKeys to the collection. Returns the number added."""
        if not rating_keys:
            return 0
        machine = self.machine_id()
        added = 0
        for start in range(0, len(rating_keys), BATCH):
            batch = [str(k) for k in rating_keys[start : start + BATCH]]
            uri = (
                f"server://{machine}/com.plexapp.plugins.library"
                f"/library/metadata/{','.join(batch)}"
            )
            response = self.session.put(
                f"{self.base}/library/collections/{self.collection_id}/items",
                params={"uri": uri},
                timeout=self.timeout,
            )
            response.raise_for_status()
            added += len(batch)
        return added


def drain_pending(state, collection, apply=False):
    """Add everything queued that Plex has now scanned.

    Returns (to_add, unresolved, unaddable).

    Albums Plex has not seen yet are left in the queue untouched - that is the whole
    mechanism for not having to wait on a scan. Standalone tracks are different: they
    have no album row in Plex and could not join an album-subtype collection even if
    they did, so they never become addable and are retired to state.skipped rather than
    retried forever.
    """
    if not state.pending:
        return [], [], []
    existing = collection.members()
    resolved, unresolved, unaddable = [], [], []
    for item_id, entry in sorted(state.pending.items()):
        if entry.get("item_type") in ("t", "track"):
            unaddable.append(
                (item_id, entry, "standalone track - an album collection cannot hold it")
            )
            continue
        rating_key = collection.find_album(
            entry.get("artist", ""), entry.get("title", ""), entry.get("path")
        )
        if rating_key is None:
            unresolved.append((item_id, entry))
            continue
        resolved.append((item_id, entry, rating_key))

    to_add = [(i, e, rk) for i, e, rk in resolved if rk not in existing]
    already = [(i, e, rk) for i, e, rk in resolved if rk in existing]

    if apply:
        collection.add([rk for _, _, rk in to_add])
        for item_id, entry, rating_key in to_add + already:
            state.added[item_id] = {**entry, "rating_key": rating_key}
            state.pending.pop(item_id, None)
        for item_id, entry, reason in unaddable:
            state.skipped[item_id] = {**entry, "reason": reason}
            state.pending.pop(item_id, None)
        state.save()
    return to_add, unresolved, unaddable
