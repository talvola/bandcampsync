"""
Scanning and reporting for free / pay-what-you-want label downloads.

Scan strategy, per label:

  * The first time a label is scanned there is no date cutoff, so the whole back catalogue
    is examined. This catches free albums missed during manual downloading.
  * Afterwards the cutoff is the newest release date already examined, recorded in state.
    An explicit "since" in the config overrides it, --full-scan bypasses it, and the
    newest local file time is used only as a fallback for a label adopted with existing
    files but no recorded scan.
  * Albums found wanted but not yet downloaded are kept pending and reconsidered on every
    run regardless of the cutoff, so nothing is lost to a --limit run or a failure.

Album classifications are cached by item_id so a label's back catalogue is only fetched
once. Prices can change, so entries that were not free are re-checked periodically.
"""

import json
import re
import time
from datetime import datetime, timezone
from pathlib import Path

from .labelconfig import MAX_PRICE, matches, prefilter
from .labels import (
    BandcampAPI,
    FreeAlbum,
    LabelError,
    RateLimited,
    album_from_details,
    list_discography,
)
from .logger import get_logger
from .media import LocalMedia

log = get_logger("freesync")

ITEM_INDEX_FILENAME = "bandcamp_item_id.txt"
# Non-free albums are re-checked after this many days in case a price has changed.
RECHECK_DAYS = 90

STATUS_DOWNLOADED = "downloaded"
STATUS_WANTED = "wanted"
STATUS_NOT_FREE = "not-free"
STATUS_FILTERED = "filtered"
STATUS_ERROR = "error"
# Title matches more than one local directory, so neither downloading nor skipping is
# safe. Resolved by writing bandcamp_item_id.txt files.
STATUS_AMBIGUOUS = "ambiguous"
ALL_STATUSES = (
    STATUS_WANTED,
    STATUS_DOWNLOADED,
    STATUS_AMBIGUOUS,
    STATUS_NOT_FREE,
    STATUS_FILTERED,
    STATUS_ERROR,
)


class FreeState:
    """Persistent scan state and album classification cache."""

    def __init__(self, state_path):
        self.path = Path(state_path)
        self.labels = {}
        self.items = {}
        # Albums classified as wanted but not yet downloaded. These are always
        # reconsidered regardless of the date cutoff, otherwise an album that was found
        # but not fetched (a --limit run, a failed download, or simply an older release
        # spotted during the first full scan) would fall behind the cutoff on the next
        # run and never be seen again.
        self.pending = {}
        # Items that can never be downloaded in the configured format - a physical-only
        # release with no digital files, for instance. Without this they stay pending and
        # are retried on every run forever.
        self.skipped = {}
        self.load()

    def load(self):
        if not self.path.is_file():
            log.info(f"No existing state file, starting fresh: {self.path}")
            return
        try:
            with open(self.path, "rt", encoding="utf-8") as f:
                data = json.load(f)
        except Exception as e:
            log.warning(f"Could not read state file {self.path}: {e}, starting fresh")
            return
        self.labels = data.get("labels") or {}
        # Keys are coerced defensively: a malformed entry must not make the whole state
        # file unreadable, which would silently reset every label to a first full scan.
        self.items = self._int_keyed(data.get("items"), "items")
        self.pending = self._int_keyed(data.get("pending"), "pending")
        self.skipped = self._int_keyed(data.get("skipped"), "skipped")
        log.info(
            f"Loaded state: {len(self.labels)} labels, {len(self.items)} cached albums, "
            f"{len(self.pending)} pending, {len(self.skipped)} skipped from {self.path}"
        )

    @staticmethod
    def _int_keyed(mapping, what):
        out = {}
        dropped = 0
        for key, value in (mapping or {}).items():
            try:
                out[int(key)] = value
            except (TypeError, ValueError):
                dropped += 1
        if dropped:
            log.warning(
                f"Dropped {dropped} malformed {what} key(s) from the state file"
            )
        return out

    def save(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        with open(tmp, "wt", encoding="utf-8") as f:
            json.dump(
                {
                    "labels": self.labels,
                    "items": {str(k): v for k, v in self.items.items()},
                    "pending": {str(k): v for k, v in self.pending.items()},
                    "skipped": {str(k): v for k, v in self.skipped.items()},
                },
                f,
                indent=1,
                sort_keys=True,
            )
        tmp.replace(self.path)
        log.info(f"Wrote state to {self.path}")

    def label(self, name):
        return self.labels.setdefault(name, {})

    def has_scanned(self, name):
        return bool(self.labels.get(name, {}).get("first_scan_done"))

    def cached(self, item_id):
        """Return a cached classification, or None if absent or due for re-check."""
        entry = self.items.get(item_id)
        if not entry:
            return None
        if entry.get("is_free"):
            return entry
        checked = entry.get("checked_ts", 0)
        if time.time() - checked > RECHECK_DAYS * 86400:
            return None
        return entry

    def cache(self, album, is_free):
        if album.item_id is None:
            # A details response without an id is unusable: it cannot be correlated to a
            # download, and storing it under a None key corrupts the state file.
            log.warning(f"Refusing to cache an album with no item id: {album.title!r}")
            return
        self.items[album.item_id] = {
            "title": album.title,
            "artist": album.artist,
            "url": album.url,
            "price": album.price,
            "is_free": is_free,
            "require_email": album.require_email,
            "num_tracks": album.num_tracks,
            "checked_ts": int(time.time()),
        }


class LabelIndex:
    """Index of what is already downloaded for one label.

    Scoped to the label's own directory rather than the whole media tree: it is much
    faster, and it avoids matching an album against a same-titled release by a different
    artist elsewhere in the collection.
    """

    def __init__(self, media_dir, label_name):
        self.dir = Path(media_dir) / label_name
        self.by_id = {}
        # Titles are NOT unique: The Audio Atelier has four distinct releases all called
        # "Best of 2025 (Free Download)". Keyed by a plain dict, later directories would
        # overwrite earlier ones and every one of those releases would match whichever
        # survived - reporting albums as downloaded that are not on disk at all.
        self.by_title = {}
        self._index()

    @property
    def ids(self):
        return set(self.by_id)

    def _index(self):
        if not self.dir.is_dir():
            log.info(f"No local directory yet for label: {self.dir}")
            return
        for child in sorted(p for p in self.dir.iterdir() if p.is_dir()):
            id_file = child / ITEM_INDEX_FILENAME
            if id_file.is_file():
                try:
                    self.by_id[int(id_file.read_text().strip())] = child.name
                except (ValueError, OSError) as e:
                    log.warning(f"Could not read {id_file}: {e}")
            title = child.name.split(" - ", 1)[1] if " - " in child.name else child.name
            self.by_title.setdefault(LocalMedia._normalize_for_match(title), []).append(
                child.name
            )
        ambiguous = sum(1 for v in self.by_title.values() if len(v) > 1)
        log.info(
            f"Indexed {self.dir}: {len(self.by_id)} with ids, "
            f"{sum(len(v) for v in self.by_title.values())} directories"
            + (f", {ambiguous} title collision(s)" if ambiguous else "")
        )

    def find(self, album):
        """Locate an album locally.

        Returns (directory_name, ambiguous). An id match is authoritative. A title match
        is only trusted when exactly one directory carries that title; otherwise the
        result is ambiguous and the caller must not assume either way, because guessing
        wrong either re-downloads something already held or silently skips something
        missing. Writing bandcamp_item_id.txt files resolves it permanently.
        """
        if album.item_id in self.by_id:
            return self.by_id[album.item_id], False
        candidates = self.by_title.get(LocalMedia._normalize_for_match(album.title), [])
        # Directories already claimed by a different item id are not candidates.
        claimed = {name for iid, name in self.by_id.items() if iid != album.item_id}
        unclaimed = [c for c in candidates if c not in claimed]
        if len(unclaimed) == 1:
            return unclaimed[0], False
        if len(unclaimed) > 1:
            return None, True
        return None, False

    def newest_mtime(self):
        """Modification time of the newest file in the label directory, or None.

        Used as the default scan cutoff: it approximates when this label was last
        downloaded, so releases older than it are already accounted for.
        """
        if not self.dir.is_dir():
            return None
        newest = 0.0
        for path in self.dir.rglob("*"):
            try:
                mtime = path.stat().st_mtime
            except OSError:
                continue
            newest = max(newest, mtime)
        if not newest:
            return None
        return datetime.fromtimestamp(newest, tz=timezone.utc)


# Words a label may bolt onto a title without it being a different release. Catalogue
# numbers ("RDC 26") are matched separately by CATALOGUE_REGEX.
NOISE_TOKENS = {
    "free",
    "download",
    "downloads",
    "name",
    "your",
    "price",
    "nyp",
    "bonus",
    "digital",
    "edition",
    "remaster",
    "remastered",
}
# Whole phrases that survive normalisation as a single run of letters, because
# _normalize_for_match drops punctuation without inserting a space: "name-your-price"
# becomes "nameyourprice".
NOISE_PHRASES = {
    "free",
    "freedownload",
    "nameyourprice",
    "nyp",
    "bonus",
    "digital",
    "remaster",
    "remastered",
}
CATALOGUE_REGEX = re.compile(r"^([a-z]{2,6})\s*\d{1,4}$")
# Words that make a trailing number a volume marker rather than a catalogue number.
VOLUME_WORDS = {"vol", "volume", "pt", "part", "no", "chapter", "book", "series"}
# A trailing roman numeral or bare number usually marks a genuinely different volume
# (Trip to Poland vs Trip to Poland II), so those must never be called duplicates.
SEQUEL_REGEX = re.compile(r"^(i{1,3}|iv|v|vi{1,3}|ix|x{1,3}|\d{1,3})$")


def _looks_like_same_release(shorter, longer):
    """True when `longer` is `shorter` plus only decorative extra words.

    Deliberately conservative: a trailing volume marker means a genuinely different
    release, and treating those as duplicates would merge distinct albums and lose
    music. "Trip to Poland" and "Trip to Poland II" must never match.
    """
    if not shorter or not longer.startswith(shorter):
        return False
    remainder = longer[len(shorter) :].strip()
    if not remainder:
        return False

    # Catalogue numbers first: the digits in "rdc 26" would otherwise look like a sequel
    # number. Guard against "vol 2" and friends, which really are different releases.
    catalogue = CATALOGUE_REGEX.match(remainder)
    if catalogue and catalogue.group(1) not in VOLUME_WORDS:
        return True

    tokens = [t for t in remainder.split() if t]
    if not tokens:
        return False
    if any(SEQUEL_REGEX.match(t) for t in tokens):
        return False
    if remainder.replace(" ", "") in NOISE_PHRASES:
        return True
    return all(t in NOISE_TOKENS for t in tokens)


def find_duplicate_dirs(media_dir, label_name):
    """Return [(kept, candidate)] directory-name pairs that look like the same release.

    Labels rename releases after the fact - adding a catalogue number, dropping a
    "(free!)" suffix - which defeats exact title matching and leaves two copies on disk.
    This only reports; deciding which to remove is a judgement call.
    """
    label_dir = Path(media_dir) / label_name
    if not label_dir.is_dir():
        return []
    titles = {}
    for child in sorted(p for p in label_dir.iterdir() if p.is_dir()):
        title = child.name.split(" - ", 1)[1] if " - " in child.name else child.name
        titles[child.name] = LocalMedia._normalize_for_match(title)

    pairs = []
    names = list(titles)
    for i, a in enumerate(names):
        for b in names[i + 1 :]:
            na, nb = titles[a], titles[b]
            if na == nb:
                pairs.append((a, b))
            elif _looks_like_same_release(na, nb):
                pairs.append((a, b))
            elif _looks_like_same_release(nb, na):
                pairs.append((b, a))
    return pairs


def report_duplicates(config, only_labels=None, output=None):
    """Print likely duplicate directories for every configured label. Local only."""
    total = 0
    for spec in config.labels:
        if only_labels and spec.name not in only_labels:
            continue
        pairs = find_duplicate_dirs(config.media_dir, spec.name)
        if not pairs:
            continue
        _safe_print("", output)
        _safe_print(f"=== {spec.name} ===", output)
        for a, b in pairs:
            total += 1
            _safe_print("  possible duplicate:", output)
            _safe_print(f"    {a}", output)
            _safe_print(f"    {b}", output)
    _safe_print("", output)
    _safe_print(f"{total} possible duplicate pair(s) found", output)
    return total


def resolve_cutoff(spec, state, index, full_scan):
    """Decide the release-date cutoff for a label scan. Returns (cutoff, why).

    The cutoff is the newest *release date already examined*, recorded in state - not the
    newest local file time. Those differ, and using file time is actively wrong: it
    updates whenever anything is downloaded, so fetching a new release would push the
    cutoff past older releases that had not been fetched yet, hiding them permanently.
    File time is only a fallback for a label adopted with existing local files but no
    recorded scan.
    """
    if full_scan:
        return None, "full scan requested"
    if not state.has_scanned(spec.name):
        return None, "first scan of this label, examining the full back catalogue"
    if spec.since:
        return spec.since, f"configured since: {spec.since:%Y-%m-%d}"
    seen = state.label(spec.name).get("newest_release_seen")
    if seen:
        cutoff = datetime.fromtimestamp(seen, tz=timezone.utc)
        return cutoff, f"newest release already examined: {cutoff:%Y-%m-%d}"
    mtime = index.newest_mtime()
    if mtime:
        return mtime, f"newest local file: {mtime:%Y-%m-%d}"
    return None, "no local files to date from, examining everything"


def scan_label(api, spec, state, media_dir, full_scan=False):
    """Scan one label.

    Returns (results, index, meta) where results is a list of
    (album_or_stub, status, detail) tuples and meta describes what the scan considered.
    """
    log.info(f'Scanning label "{spec.name}" ({spec.url})')
    index = LabelIndex(media_dir, spec.name)

    band_id = spec.band_id or state.label(spec.name).get("band_id")
    if not band_id:
        band_id = api.resolve_band_id(spec.url)
        log.info(f'Resolved band_id for "{spec.name}": {band_id}')
    state.label(spec.name)["band_id"] = band_id

    cutoff, why = resolve_cutoff(spec, state, index, full_scan)
    log.info(f'Cutoff for "{spec.name}": {why}')

    discography = list_discography(api, band_id)
    log.info(f'"{spec.name}" discography: {len(discography)} items')

    considered = []
    skipped_cutoff = skipped_prefilter = 0
    for entry in discography:
        if entry.item_id is None:
            continue
        # Pending albums are always reconsidered, however old, until downloaded.
        is_pending = entry.item_id in state.pending
        if (
            cutoff
            and entry.release_date
            and entry.release_date < cutoff
            and not is_pending
        ):
            skipped_cutoff += 1
            continue
        # Reject on the cheap metadata before spending a per-album request.
        keep, _reason = prefilter(entry, spec.match)
        if not keep:
            skipped_prefilter += 1
            continue
        considered.append(entry)
    log.info(
        f'"{spec.name}": {len(considered)} of {len(discography)} items to fetch '
        f"({skipped_cutoff} before cutoff, {skipped_prefilter} rejected by prefilter)"
    )

    results = []
    errors = 0
    for entry in considered:
        item_id, item_type, title = entry.item_id, entry.item_type, entry.title
        cached = state.cached(item_id)
        if cached is not None and not cached.get("is_free"):
            # Same reasoning as the freshly-classified not-free branch below: a pending
            # entry must not survive a not-free verdict, or it bypasses the cutoff and
            # is reconsidered forever.
            state.pending.pop(item_id, None)
            results.append(
                (_Stub(item_id, title, cached), STATUS_NOT_FREE, "cached: not free")
            )
            continue
        try:
            details = api.tralbum_details(band_id, item_id, item_type)
            album = album_from_details(details, label_name=spec.name)
        except RateLimited:
            # Abort rather than marking every remaining album as failed. State is not
            # marked scanned, so the next run resumes the full sweep.
            log.error(
                f'Rate limited while scanning "{spec.name}" after '
                f"{len(results)}/{len(considered)} albums. Increase request_delay "
                f"and re-run; already-classified albums are cached."
            )
            raise
        except LabelError as e:
            log.warning(f"Failed to fetch details for {item_id} ({title}): {e}")
            results.append((_Stub(item_id, title, {}), STATUS_ERROR, str(e)))
            errors += 1
            continue

        state.cache(album, album.is_free)

        if not album.is_free or album.price > MAX_PRICE:
            # Clear any pending entry: an album can stop being free (labels re-price
            # them) or turn out never to have been downloadable. Leaving it pending
            # would make it bypass the cutoff and be retried on every future run.
            state.pending.pop(album.item_id, None)
            results.append((album, STATUS_NOT_FREE, f"{album.price} {album.currency}"))
            continue
        if album.item_id in state.skipped:
            state.pending.pop(album.item_id, None)
            results.append(
                (album, STATUS_ERROR, f"skipped: {state.skipped[album.item_id]}")
            )
            continue
        local, ambiguous = index.find(album)
        if local:
            state.pending.pop(album.item_id, None)
            results.append((album, STATUS_DOWNLOADED, local))
            continue
        if ambiguous:
            state.pending.pop(album.item_id, None)
            results.append(
                (
                    album,
                    STATUS_AMBIGUOUS,
                    "title matches several local directories; write "
                    "bandcamp_item_id.txt files to disambiguate",
                )
            )
            continue
        wanted, reason = matches(album, spec.match)
        if wanted:
            state.pending[album.item_id] = spec.name
        else:
            state.pending.pop(album.item_id, None)
        results.append((album, STATUS_WANTED if wanted else STATUS_FILTERED, reason))

    state.label(spec.name)["last_scan_ts"] = int(time.time())
    # Only record the label as fully scanned when nothing failed. Otherwise the next run
    # would apply a cutoff and silently skip whatever was missed.
    if errors:
        log.warning(
            f'"{spec.name}" had {errors} failed lookups; not marking as fully scanned '
            f"so the next run re-examines the full catalogue"
        )
    else:
        state.label(spec.name)["first_scan_done"] = True
        # Record how far we got, so the next run only examines newer releases. Only on a
        # clean scan: recording it after failures would skip whatever was missed.
        newest = max(
            (e.release_date for e in discography if e.release_date), default=None
        )
        if newest:
            state.label(spec.name)["newest_release_seen"] = int(newest.timestamp())
    meta = {
        "cutoff_reason": why,
        "total": len(discography),
        "fetched": len(considered),
        "skipped_cutoff": skipped_cutoff,
        "skipped_prefilter": skipped_prefilter,
    }
    return results, index, meta


class _Stub:
    """Minimal stand-in for a FreeAlbum when only cached or partial data is available."""

    def __init__(self, item_id, title, cached):
        self.item_id = item_id
        self.title = title
        self.artist = cached.get("artist", "")
        self.price = cached.get("price", 0.0)
        self.num_tracks = cached.get("num_tracks", 0)
        self.require_email = cached.get("require_email", False)
        self.currency = ""
        self.url = ""
        self.release_date = None


def _safe_print(text, output=None):
    """Print guarding against Windows console encoding errors on unicode metadata."""
    stream = output
    try:
        if stream:
            stream.write(text + "\n")
        else:
            print(text)
    except UnicodeEncodeError:
        cleaned = text.encode("ascii", errors="replace").decode("ascii")
        if stream:
            stream.write(cleaned + "\n")
        else:
            print(cleaned)


def print_report(all_results, output=None, meta=None):
    """Print a per-label summary and the list of wanted albums."""
    meta = meta or {}
    totals = dict.fromkeys(ALL_STATUSES, 0)
    for label_name, results in all_results.items():
        counts = dict.fromkeys(ALL_STATUSES, 0)
        for _album, status, _detail in results:
            counts[status] = counts.get(status, 0) + 1
            totals[status] = totals.get(status, 0) + 1
        _safe_print("", output)
        _safe_print(f"=== {label_name} ===", output)
        info = meta.get(label_name)
        if info:
            # Without this an incremental run prints all zeros, which reads as a failure
            # rather than "nothing new since last time".
            _safe_print(f"  cutoff: {info['cutoff_reason']}", output)
            _safe_print(
                f"  {info['total']} releases, {info['fetched']} examined "
                f"({info['skipped_cutoff']} older than cutoff, "
                f"{info['skipped_prefilter']} filtered on artist/title)",
                output,
            )
        summary = "  " + "  ".join(f"{k}={v}" for k, v in counts.items() if v)
        _safe_print(summary if summary.strip() else "  nothing new", output)
        for album, status, detail in results:
            if status == STATUS_AMBIGUOUS:
                _safe_print(f"  AMBIGUOUS {album.artist} - {album.title}", output)
                _safe_print(f"          {album.url}", output)
                _safe_print(f"          {detail}", output)
                continue
            if status != STATUS_WANTED:
                continue
            email = " [needs email]" if getattr(album, "require_email", False) else ""
            date = ""
            if getattr(album, "release_date", None):
                date = f" {album.release_date:%Y-%m-%d}"
            _safe_print(
                f"  WANTED{date} {album.artist} - {album.title} "
                f"({album.num_tracks} tracks){email}",
                output,
            )
            _safe_print(f"          {album.url}", output)
            _safe_print(f"          matched: {detail}", output)

    _safe_print("", output)
    _safe_print("=== totals ===", output)
    for status in ALL_STATUSES:
        _safe_print(f"  {status:<12} {totals.get(status, 0)}", output)
    return totals


def scan_all(config, state, full_scan=False, only_labels=None):
    """Scan every enabled label. Returns ({label_name: results}, {label_name: meta})."""
    api = BandcampAPI(delay=config.request_delay)
    all_results = {}
    all_meta = {}
    for spec in config.enabled_labels:
        if only_labels and spec.name not in only_labels:
            continue
        try:
            results, _index, meta = scan_label(
                api, spec, state, config.media_dir, full_scan
            )
            all_meta[spec.name] = meta
        except RateLimited as e:
            # Persist whatever was classified before giving up, so a re-run resumes
            # cheaply rather than re-fetching everything.
            all_results[spec.name] = [(_Stub(0, "", {}), STATUS_ERROR, str(e))]
            state.save()
            log.error("Aborting scan: rate limited by bandcamp.com")
            break
        except LabelError as e:
            log.error(f'Failed to scan label "{spec.name}": {e}')
            all_results[spec.name] = [(_Stub(0, "", {}), STATUS_ERROR, str(e))]
            state.save()
            continue
        except Exception as e:
            # One label's unexpected failure must not discard every other label's work.
            log.exception(f'Unexpected error scanning label "{spec.name}": {e}')
            all_results[spec.name] = [(_Stub(0, "", {}), STATUS_ERROR, repr(e))]
            state.save()
            continue
        all_results[spec.name] = results
        # Save after every label, not just at the end: a full sweep is many minutes of
        # requests and a crash partway through should not throw away the whole run.
        state.save()
    state.save()
    return all_results, all_meta


def generate_free_report(config, state_path, full_scan=False, only_labels=None):
    """Scan all enabled labels and print a report. Makes no changes to the media tree."""
    if not config.media_dir:
        raise ValueError("No media_dir configured under defaults in the config file")
    state = FreeState(state_path)
    all_results, all_meta = scan_all(config, state, full_scan, only_labels)
    print_report(all_results, meta=all_meta)
    return all_results


def backfill_ids(config, state, only_labels=None, apply=False, output=None):
    """Write bandcamp_item_id.txt into local directories that lack one.

    Dedup falls back to matching normalised titles when no id file exists, and that is
    fragile in practice: labels rename releases, add catalogue numbers, drop "(free!)"
    suffixes, and reuse titles across different releases. Stamping the item id makes
    matching exact and permanent.

    Cheap: one band_details request per label, no per-album lookups. Only writes where
    the mapping is unambiguous in both directions - exactly one local directory and
    exactly one remote release share the title.
    """
    api = BandcampAPI(delay=config.request_delay)
    totals = {"written": 0, "have": 0, "ambiguous": 0, "unmatched": 0}

    for spec in config.labels:
        if only_labels and spec.name not in only_labels:
            continue
        index = LabelIndex(config.media_dir, spec.name)
        if not index.dir.is_dir():
            continue
        band_id = spec.band_id or state.label(spec.name).get("band_id")
        if not band_id:
            try:
                band_id = api.resolve_band_id(spec.url)
            except LabelError as e:
                _safe_print(f"  {spec.name}: cannot resolve band id: {e}", output)
                continue

        try:
            disco = list_discography(api, band_id)
        except LabelError as e:
            _safe_print(f"  {spec.name}: discography failed: {e}", output)
            continue

        remote = {}
        for entry in disco:
            remote.setdefault(LocalMedia._normalize_for_match(entry.title), []).append(
                entry
            )

        rows = []
        for key, dirs in sorted(index.by_title.items()):
            for dirname in dirs:
                path = index.dir / dirname
                if (path / ITEM_INDEX_FILENAME).is_file():
                    totals["have"] += 1
                    continue
                candidates = remote.get(key, [])
                if len(candidates) == 1 and len(dirs) == 1:
                    rows.append((dirname, candidates[0].item_id, candidates[0].title))
                elif len(candidates) > 1 or len(dirs) > 1:
                    totals["ambiguous"] += 1
                    rows.append(
                        (
                            dirname,
                            None,
                            f"ambiguous: {len(candidates)} remote / {len(dirs)} local",
                        )
                    )
                else:
                    totals["unmatched"] += 1

        writable = [r for r in rows if r[1] is not None]
        if not rows:
            continue
        _safe_print("", output)
        _safe_print(f"=== {spec.name} ===", output)
        for dirname, item_id, note in rows:
            if item_id is None:
                _safe_print(f"  SKIP  {dirname}  ({note})", output)
            else:
                _safe_print(
                    f"  {'WRITE' if apply else 'would'} {item_id:<12} {dirname}", output
                )
        if apply:
            for dirname, item_id, _note in writable:
                (index.dir / dirname / ITEM_INDEX_FILENAME).write_text(f"{item_id}\n")
                totals["written"] += 1
        else:
            totals["written"] += len(writable)

    _safe_print("", output)
    verb = "wrote" if apply else "would write"
    _safe_print(
        f"{verb} {totals['written']} id file(s); {totals['have']} already had one, "
        f"{totals['ambiguous']} ambiguous, {totals['unmatched']} not found remotely",
        output,
    )
    if not apply:
        _safe_print("(dry run - pass --apply to write)", output)
    return totals


def pending_albums(config, state, api=None):
    """Rebuild FreeAlbum objects for everything queued, without re-scanning.

    A bounded run (--limit / --max-gb) leaves work queued, and re-scanning every label
    just to download it again costs hundreds of API calls for no new information. The
    pending items were already classified and matched, so the cached metadata is enough.
    Anything cached before the cache stored URLs is re-fetched individually.
    """
    specs = {spec.name: spec for spec in config.labels}
    out = []
    for item_id, label_name in sorted(state.pending.items()):
        if item_id in state.skipped:
            continue
        spec = specs.get(label_name)
        if not spec:
            log.warning(
                f"Pending item {item_id} belongs to unconfigured label {label_name!r}"
            )
            continue
        cached = state.items.get(item_id) or {}
        url = cached.get("url")
        if not url:
            if api is None:
                api = BandcampAPI(delay=config.request_delay)
            band_id = spec.band_id or state.label(spec.name).get("band_id")
            log.info(f"Cache predates URL storage, fetching details for {item_id}")
            album = album_from_details(
                api.tralbum_details(band_id, item_id, "a"), label_name=label_name
            )
            if album.item_id is None or not album.title:
                # Delisted or otherwise gone. Record it so the queue drains instead of
                # carrying an item that can never be fetched.
                log.warning(
                    f"Pending item {item_id} returns no usable details; recording it "
                    f"as unavailable"
                )
                state.skipped[item_id] = "no details from bandcamp"
                state.pending.pop(item_id, None)
                continue
            state.cache(album, album.is_free)
        else:
            album = FreeAlbum(
                item_id=item_id,
                title=cached.get("title") or "",
                artist=cached.get("artist") or "",
                url=url,
                price=cached.get("price") or 0.0,
                is_set_price=False,
                require_email=bool(cached.get("require_email")),
                free_download=False,
                num_tracks=int(cached.get("num_tracks") or 0),
                label_name=label_name,
            )
        out.append((label_name, album))
    return out


def do_repair(config, state_path, item_id, client_secret=None, token_path=None):
    """Re-fetch one album and add only the files missing from its local directory.

    For albums a label extended after the original download. Bandcamp does not serve
    individual tracks of a compilation, so the whole archive has to come down even for a
    single missing file, but nothing already present is rewritten.
    """
    from .freedownload import repair_album

    state = FreeState(state_path)
    if not state.items.get(item_id):
        raise ValueError(
            f"Item {item_id} is not in the scan state; run --report first so it is known"
        )

    specs = {spec.name: spec for spec in config.labels}
    api = BandcampAPI(delay=config.request_delay)
    label_name = state.pending.get(item_id)
    if not label_name:
        # Not pending (it counts as downloaded), so find which label lists it.
        for spec in config.labels:
            band_id = spec.band_id or state.label(spec.name).get("band_id")
            if not band_id:
                continue
            if any(e.item_id == item_id for e in list_discography(api, band_id)):
                label_name = spec.name
                break
    if not label_name or label_name not in specs:
        raise ValueError(f"Could not find a configured label owning item {item_id}")

    spec = specs[label_name]
    band_id = spec.band_id or state.label(spec.name).get("band_id")
    album = album_from_details(
        api.tralbum_details(band_id, item_id, "a"), label_name=label_name
    )

    index = LabelIndex(config.media_dir, label_name)
    local_name, ambiguous = index.find(album)
    if ambiguous:
        raise ValueError(
            f"{album.title!r} matches several directories under {label_name}; "
            f"write bandcamp_item_id.txt files to disambiguate before repairing"
        )
    if not local_name:
        raise ValueError(
            f"No local directory found for {album.title!r} under {label_name}; "
            f"use a normal download run rather than --repair"
        )
    local_path = Path(config.media_dir) / label_name / local_name
    existing = len([p for p in local_path.iterdir() if p.is_file()])
    log.info(
        f"Repairing {album.title!r} at {local_path} "
        f"({existing} local files, {album.num_tracks} remote tracks)"
    )

    gmail_reader = None
    if album.require_email:
        from .gmail import GmailReader, load_credentials

        gmail_reader = GmailReader(load_credentials(client_secret, token_path))

    added = repair_album(album, spec, config, local_path, gmail_reader)
    if added:
        log.info(f"Added {len(added)} file(s):")
        for name in added:
            log.info(f"  {name}")
    else:
        log.info("Nothing was missing; no files added")
    return added


def do_free_sync(
    config,
    state_path,
    full_scan=False,
    only_labels=None,
    limit=None,
    max_gb=None,
    temp_dir=None,
    client_secret=None,
    token_path=None,
    pending_only=False,
):
    """Scan, then download everything wanted. Returns a list of (album, path or error)."""
    from .freedownload import AcquireError, acquire_album

    if not config.media_dir:
        raise ValueError("No media_dir configured under defaults in the config file")
    state = FreeState(state_path)
    specs = {spec.name: spec for spec in config.labels}
    if pending_only:
        log.info("Downloading the queued backlog without re-scanning")
        wanted = pending_albums(config, state)
        if only_labels:
            wanted = [(lab, a) for lab, a in wanted if lab in only_labels]
    else:
        all_results, all_meta = scan_all(config, state, full_scan, only_labels)
        print_report(all_results, meta=all_meta)
        wanted = [
            (label_name, album)
            for label_name, results in all_results.items()
            for album, status, _detail in results
            if status == STATUS_WANTED
        ]
    if not wanted:
        log.info("Nothing to download")
        return []
    if limit:
        if len(wanted) > limit:
            log.warning(
                f"{len(wanted)} albums wanted, limiting this run to {limit}; "
                f"re-run to fetch the rest"
            )
        wanted = wanted[:limit]

    # Built on first use rather than up front. The cached require_email flag is not a
    # reliable predictor - bandcamp emails the link for albums cached as not requiring
    # it - and deciding up front meant those downloads failed outright even though
    # credentials were available.
    _gmail = {}

    def get_gmail_reader():
        if "reader" not in _gmail:
            from .gmail import GmailReader, load_credentials

            log.info("Album needs an emailed link, authorising Gmail")
            _gmail["reader"] = GmailReader(load_credentials(client_secret, token_path))
        return _gmail["reader"]

    done, downloaded_bytes = [], 0
    budget = int(max_gb * 1024**3) if max_gb else None
    for label_name, album in wanted:
        if budget and downloaded_bytes >= budget:
            log.warning(
                f"Reached the {max_gb} GB budget for this run, stopping. "
                f"Re-run to continue."
            )
            break
        try:
            path = acquire_album(
                album, specs[label_name], config, get_gmail_reader, temp_dir
            )
        except (AcquireError, ValueError) as e:
            message = str(e)
            permanent = (
                "does not contain requested encoding" in message
                or "No download available" in message
            )
            log.error(f'Failed to download "{album.title}": {e}')
            if permanent:
                # Retrying this every run would never succeed: the release has no
                # digital files in the requested format (physical-only items, for one).
                log.warning(
                    f'Marking "{album.title}" as permanently skipped; it offers no '
                    f"{config.media_format} download"
                )
                state.skipped[album.item_id] = f"no {config.media_format} download"
                state.pending.pop(album.item_id, None)
                state.save()
            done.append((album, None, message))
            continue
        size = sum(p.stat().st_size for p in path.rglob("*") if p.is_file())
        downloaded_bytes += size
        state.pending.pop(album.item_id, None)
        state.save()
        done.append((album, path, None))
        log.info(f'Downloaded "{album.title}" -> {path} ({size / 1024**3:.2f} GB)')

    ok = sum(1 for _a, p, _e in done if p)
    log.info(
        f"Downloaded {ok}/{len(done)} albums, {downloaded_bytes / 1024**3:.2f} GB total"
    )
    return done
