"""
Acquisition and download of free / pay-what-you-want albums.

Flow, verified against Future Avenue:

  1. POST <label>/email_download with the item id and an address. Plain HTTP: no cookies,
     no reCAPTCHA token. country and postcode are mandatory - omitting them returns
     {"ok": false, "error": "Sorry, this item is no longer available for free."}, which
     means "bad location data" rather than anything about availability.
  2. The response either carries download_url directly, or is {"ok": true} and bandcamp
     emails the link. Both branches must be handled.
  3. The resulting bandcamp.com/download page is parsed by the existing
     Bandcamp.get_download_file_url(), which already picks the requested format out of
     digital_items[].downloads.
"""

import json
import os
import shutil
import time
import tempfile
import zipfile
from pathlib import Path
from urllib.parse import urlsplit

from curl_cffi import requests

from .bandcamp import Bandcamp, BandcampError, BandcampItem
from .download import download_file, is_zip_file, move_file, unzip_file
from .logger import get_logger
from .media import clean_label_dir_name, clean_path_component, parse_zip_filename

log = get_logger("freedownload")

ITEM_INDEX_FILENAME = "bandcamp_item_id.txt"


class AcquireError(ValueError):
    pass


def _endpoint_for(album, label_url):
    """Return the email_download endpoint for an album.

    A label's discography can include releases hosted on the artist's own subdomain
    (Children of Vapor lists albums living on eccosystem.bandcamp.com), and the endpoint
    must be on the same subdomain as the album or the request fails. Derive it from the
    album URL, falling back to the configured label URL.
    """
    source = album.url or label_url
    parts = urlsplit(source)
    if parts.scheme and parts.netloc:
        return f"{parts.scheme}://{parts.netloc}/email_download"
    return f"{label_url.rstrip('/')}/email_download"


def _item_type_name(album):
    """bandcamp wants the word, not its letter: "album" or "track"."""
    return "track" if getattr(album, "item_type", "a") in ("t", "track") else "album"


def request_download(label_url, album, email, country, postcode):
    """Ask bandcamp for a free download. Returns a URL, or None if it was emailed."""
    if not email:
        raise AcquireError("No email address configured (defaults.email)")
    if not country or not postcode:
        # Worth failing loudly: bandcamp's error for missing location data is misleading.
        raise AcquireError(
            "Both defaults.country and defaults.postcode are required; bandcamp rejects "
            "the request with a misleading 'no longer available for free' error without them"
        )
    endpoint = _endpoint_for(album, label_url)
    payload = {
        "encoding_name": "none",
        "item_id": str(album.item_id),
        "item_type": _item_type_name(album),
        "address": email,
        "country": country,
        "postcode": str(postcode),
    }
    log.info(f"Requesting free download of {album.title!r} from {endpoint}")
    try:
        response = requests.post(
            endpoint,
            data=payload,
            headers={
                "X-Requested-With": "XMLHttpRequest",
                "Referer": album.url or label_url,
                "Content-Type": "application/x-www-form-urlencoded",
            },
            impersonate="chrome",
            timeout=60,
        )
    except Exception as e:
        raise AcquireError(f"Failed to POST to {endpoint}: {e}") from e
    if response.status_code != 200:
        raise AcquireError(
            f"Unexpected status code from {endpoint}: {response.status_code}"
        )
    try:
        data = json.loads(response.text)
    except Exception as e:
        raise AcquireError(f"Could not parse response from {endpoint}: {e}") from e
    if not data.get("ok"):
        raise AcquireError(
            f"bandcamp refused the free download of {album.title!r}: "
            f"{data.get('error') or data}"
        )
    url = data.get("download_url")
    if url:
        log.info("bandcamp returned a download URL directly, no email needed")
        return url
    log.info(f"bandcamp emailed the download link for {album.title!r}")
    return None


def resolve_and_download(
    download_page_url, album, label_name, media_dir, media_format="flac", temp_dir=None
):
    """Download and extract an album from its bandcamp download page.

    Returns the local directory the album was extracted to.
    """
    # Anonymous session: free download URLs are signed and need no account.
    bc = Bandcamp("", require_auth=False)
    item = BandcampItem(
        {
            "item_id": album.item_id,
            "band_name": label_name,
            "item_title": album.title,
            "item_type": _item_type_name(album),
        }
    )
    item.download_url = download_page_url

    file_url = bc.get_download_file_url(item, encoding=media_format)
    file_url = stat_download(bc, item, file_url)

    with tempfile.TemporaryDirectory(dir=temp_dir) as td:
        tmp_file = Path(td) / "download.bin"
        with open(tmp_file, "wb") as fh:
            content_filename = download_file(file_url, fh)
        size_mb = tmp_file.stat().st_size / (1024 * 1024)
        log.info(f"Downloaded {content_filename!r} ({size_mb:.1f} MB)")

        local_path = _target_path(media_dir, label_name, album, content_filename)
        local_path.mkdir(parents=True, exist_ok=True)
        if is_zip_file(tmp_file):
            log.info(f"Extracting to {local_path}")
            unzip_file(tmp_file, local_path)
        else:
            # A standalone track is served as the audio file itself, not an archive.
            # Give it a directory of its own so it matches the rest of the collection
            # and can carry the item id file.
            name = content_filename or f"{album.title}.{media_format}"
            log.info(f"Single track, moving {name!r} into {local_path}")
            move_file(str(tmp_file), str(local_path / name))

    id_file = local_path / ITEM_INDEX_FILENAME
    id_file.write_text(f"{album.item_id}\n")
    log.info(f"Wrote {id_file}")
    return local_path


def _target_path(media_dir, label_name, album, content_filename):
    """Place the release under media_dir/<label>/<name>.

    Mirrors LocalMedia.get_path_for_zip_purchase for the label case: the zip filename is
    "Artist - Album.zip" where Artist is the real album artist, and the label name is the
    grouping directory. A standalone track arrives as a bare audio file rather than an
    archive, so its directory is built from the metadata instead.
    """
    def clean(raw):
        # Clean first, then strip: the cleaning can expose leading/trailing separators
        # that were previously hidden behind an illegal character.
        return clean_path_component(raw).strip(" -") or str(album.item_id)

    media_dir = Path(media_dir)
    # Label grouping dir uses the narrower cleaner, matched by LabelIndex. The album
    # directory below still uses clean_path_component, which LocalMedia._normalize_for_match
    # compares leniently.
    label_dir = media_dir / clean_label_dir_name(label_name)
    if getattr(album, "item_type", "a") in ("t", "track"):
        return label_dir / clean(f"{album.artist} - {album.title}")
    if content_filename:
        zip_artist, zip_album = parse_zip_filename(content_filename)
        if zip_artist and zip_album:
            stem = content_filename
            if stem.lower().endswith(".zip"):
                stem = stem[:-4]
            return label_dir / clean(stem)
    return label_dir / clean(f"{album.artist} - {album.title}")


def stat_download(bc, item, file_url, attempts=3, timeout=300, wait=20):
    """Refresh a download URL via the stat endpoint, tolerating slow archive builds.

    Bandcamp assembles the zip server-side while this request is open, so a large
    compilation (a 120-track, 7.5GB Weedian album, say) blows past the default 30 second
    timeout. Retry with a generous timeout, and if it still will not answer, fall back to
    the original URL - which is what the stat call returns anyway when it succeeds with
    an "ok" result, so the download is usually still fine.
    """
    for attempt in range(1, attempts + 1):
        try:
            return bc.check_download_stat(item, file_url, timeout=timeout)
        except BandcampError as e:
            log.warning(
                f"statdownload attempt {attempt}/{attempts} failed "
                f"(bandcamp may still be building the archive): {e}"
            )
            if attempt < attempts:
                time.sleep(wait * attempt)
    log.warning("Proceeding with the un-refreshed download URL")
    return file_url


def _common_prefix(names):
    """Longest common leading string across names, trimmed to a sane boundary."""
    if len(names) < 2:
        return ""
    prefix = os.path.commonprefix(list(names))
    # Only trust a prefix that ends at a separator, so track numbers are not eaten.
    cut = max(prefix.rfind(" - "), prefix.rfind("_"), prefix.rfind("/"))
    return (
        prefix[: cut + 3]
        if prefix.rfind(" - ") == cut and cut != -1
        else (prefix[: cut + 1] if cut != -1 else "")
    )


def _track_keys(names):
    """Map each name to a comparison key with the shared album-title prefix removed.

    Bandcamp embeds the album title in every filename, so when a label renames an album
    ("Trip to Poland" -> "Trip to Poland II") every local file mismatches every archive
    entry even though the tracks are the same. Comparing the part after the shared prefix
    identifies tracks across a rename.
    """
    prefix = _common_prefix(names)
    return {
        (n[len(prefix) :] if prefix and n.startswith(prefix) else n): n for n in names
    }


def extract_missing(zip_path, local_path, max_additions=None):
    """Extract only entries that are not already present locally.

    Used to repair a directory that is complete apart from tracks the label added after
    the original download. Bandcamp does not allow downloading an individual track of a
    compilation (has_digital_download is false on them), so the whole archive must be
    fetched even to recover one file - but there is no reason to rewrite the rest.

    max_additions guards against a silent duplicate explosion: if far more files look
    missing than expected, the naming has drifted in a way this cannot reconcile and it
    is safer to stop than to double the directory.
    """
    local_path = Path(local_path)
    existing_names = [p.name for p in local_path.iterdir() if p.is_file()]
    with zipfile.ZipFile(zip_path) as zf:
        entries = [i for i in zf.infolist() if not i.is_dir() and Path(i.filename).name]
        entry_names = [Path(i.filename).name for i in entries]

        local_keys = set(_track_keys(existing_names))
        entry_keys = _track_keys(entry_names)
        missing = {k: n for k, n in entry_keys.items() if k not in local_keys}

        if max_additions is not None and len(missing) > max_additions:
            raise AcquireError(
                f"Repair would add {len(missing)} files but only {max_additions} were "
                f"expected to be missing. The local filenames probably no longer match "
                f"the archive (the label may have renamed the album). Refusing to "
                f"duplicate the directory; inspect it manually."
            )

        added = []
        by_name = {Path(i.filename).name: i for i in entries}
        for _key, name in sorted(missing.items()):
            info = by_name[name]
            with zf.open(info) as src, open(local_path / name, "wb") as dst:
                shutil.copyfileobj(src, dst)
            added.append(name)

    log.info(
        f"Repair: added {len(added)} file(s), left "
        f"{len(existing_names)} existing file(s) alone"
    )
    return added


def repair_album(album, spec, config, local_path, gmail_reader=None, temp_dir=None):
    """Fetch an album's archive and add only the files missing from local_path."""
    url = request_download(
        spec.subdomain_url, album, config.email, config.country, config.postcode
    )
    if url is None:
        if gmail_reader is None:
            raise AcquireError(
                f"{album.title!r} requires an emailed download link but Gmail is not "
                f"configured. Run with --gmail-auth first."
            )
        url = gmail_reader.wait_for_link(album.item_id)

    bc = Bandcamp("", require_auth=False)
    item = BandcampItem(
        {
            "item_id": album.item_id,
            "band_name": spec.name,
            "item_title": album.title,
            "item_type": "album",
        }
    )
    item.download_url = url
    file_url = bc.get_download_file_url(item, encoding=config.media_format)
    file_url = stat_download(bc, item, file_url)

    with tempfile.TemporaryDirectory(dir=temp_dir) as td:
        tmp_file = Path(td) / "download.bin"
        with open(tmp_file, "wb") as fh:
            content_filename = download_file(file_url, fh)
        size_mb = tmp_file.stat().st_size / (1024 * 1024)
        log.info(f"Downloaded {content_filename!r} ({size_mb:.1f} MB) for repair")
        if not is_zip_file(tmp_file):
            raise AcquireError(f"Repair download for {album.title!r} is not a zip")
        expected = max(
            0,
            album.num_tracks
            - len(
                [p for p in Path(local_path).iterdir() if p.suffix.lower() == ".flac"]
            ),
        )
        # Allow a small margin for artwork and similar non-track files.
        added = extract_missing(tmp_file, local_path, max_additions=expected + 5)

    id_file = Path(local_path) / ITEM_INDEX_FILENAME
    if not id_file.is_file():
        id_file.write_text(f"{album.item_id}\n")
    return added


def acquire_album(album, spec, config, gmail_reader=None, temp_dir=None):
    """Request, retrieve and download one free album. Returns the local path.

    gmail_reader may be a GmailReader or a zero-argument callable returning one, so the
    caller can defer authorising Gmail until an album actually needs an emailed link.
    """
    url = request_download(
        spec.subdomain_url, album, config.email, config.country, config.postcode
    )
    if url is None:
        reader = gmail_reader() if callable(gmail_reader) else gmail_reader
        if reader is None:
            raise AcquireError(
                f"{album.title!r} requires an emailed download link but Gmail is not "
                f"configured. Run with --gmail-auth first."
            )
        url = reader.wait_for_link(album.item_id)
    return resolve_and_download(
        url,
        album,
        spec.name,
        config.media_dir,
        media_format=config.media_format,
        temp_dir=temp_dir,
    )
