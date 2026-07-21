"""
Retrieval of bandcamp free-download links from Gmail.

Albums with require_email set do not return a download URL directly; bandcamp emails the
link instead. This module authorises against the Gmail API once, then reads those mails
unattended on subsequent runs.

The Google libraries are an optional dependency, installed with:

    uv sync --extra gmail          (or: pip install "bandcampsync[gmail]")

Only the readonly scope is requested. Nothing is ever sent, modified or deleted.
"""

import base64
import re
import time
from pathlib import Path

from .logger import get_logger

log = get_logger("gmail")

SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]
API_BASE = "https://gmail.googleapis.com/gmail/v1/users/me"

# bandcamp sends one mail per requested album, all with the same subject, so the link must
# be correlated on the id parameter rather than assuming the newest mail is the right one.
SEARCH_QUERY = 'from:noreply@bandcamp.com subject:"Your download from"'
DOWNLOAD_URL_REGEX = re.compile(r"https://bandcamp\.com/download\?[^\s\"'<>]+")
ITEM_ID_REGEX = re.compile(r"[?&]id=(\d+)")


class GmailError(ValueError):
    pass


def _require_google_libs():
    try:
        from google.auth.transport.requests import AuthorizedSession, Request
        from google.oauth2.credentials import Credentials
        from google_auth_oauthlib.flow import InstalledAppFlow
    except ImportError as e:
        raise GmailError(
            "The Gmail integration requires extra dependencies. Install them with: "
            'uv sync --extra gmail  (or: pip install "bandcampsync[gmail]")'
        ) from e
    return AuthorizedSession, Request, Credentials, InstalledAppFlow


def load_credentials(client_secret_path, token_path):
    """Return authorised credentials, running the browser consent flow if needed.

    The consent flow only runs when there is no stored token and none can be refreshed,
    so scheduled runs never block on user interaction.
    """
    AuthorizedSession, Request, Credentials, InstalledAppFlow = _require_google_libs()
    client_secret_path = Path(client_secret_path)
    token_path = Path(token_path)

    creds = None
    if token_path.is_file():
        try:
            creds = Credentials.from_authorized_user_file(str(token_path), SCOPES)
        except Exception as e:
            log.warning(f"Could not load stored token {token_path}: {e}")

    if creds and creds.valid:
        return creds

    if creds and creds.expired and creds.refresh_token:
        log.info("Refreshing expired Gmail token")
        try:
            creds.refresh(Request())
        except Exception as e:
            log.warning(f"Token refresh failed, re-authorising: {e}")
            creds = None
        else:
            _save_token(token_path, creds)
            return creds

    if not client_secret_path.is_file():
        raise GmailError(
            f"Gmail client secret not found: {client_secret_path}. Download the OAuth "
            f"desktop client JSON from the Google Cloud console."
        )
    log.info("No usable Gmail token, starting the browser authorisation flow")
    flow = InstalledAppFlow.from_client_secrets_file(str(client_secret_path), SCOPES)
    creds = flow.run_local_server(port=0)
    _save_token(token_path, creds)
    return creds


def _save_token(token_path, creds):
    token_path.parent.mkdir(parents=True, exist_ok=True)
    token_path.write_text(creds.to_json(), encoding="utf-8")
    try:  # best effort on Windows, which ignores POSIX modes
        token_path.chmod(0o600)
    except OSError:
        pass
    log.info(f"Stored Gmail token at {token_path}")


def _decode_part(data):
    if not data:
        return ""
    padded = data + "=" * (-len(data) % 4)
    try:
        return base64.urlsafe_b64decode(padded).decode("utf-8", errors="replace")
    except Exception:
        return ""


def _collect_text(payload):
    """Recursively gather text from a Gmail message payload."""
    out = []
    body = payload.get("body") or {}
    if body.get("data"):
        out.append(_decode_part(body["data"]))
    for part in payload.get("parts") or []:
        out.append(_collect_text(part))
    return "\n".join(p for p in out if p)


class GmailReader:
    """Reads bandcamp download links out of a Gmail mailbox."""

    def __init__(self, credentials):
        AuthorizedSession, _Request, _Credentials, _Flow = _require_google_libs()
        self.session = AuthorizedSession(credentials)

    def _get(self, url, **params):
        response = self.session.get(url, params=params, timeout=60)
        if response.status_code != 200:
            raise GmailError(
                f"Gmail API request failed ({response.status_code}): {response.text[:300]}"
            )
        return response.json()

    def find_download_links(self, newer_than="1d", max_messages=50):
        """Return {item_id: download_url} from recent bandcamp download mails."""
        query = f"{SEARCH_QUERY} newer_than:{newer_than}"
        listing = self._get(f"{API_BASE}/messages", q=query, maxResults=max_messages)
        links = {}
        for stub in listing.get("messages") or []:
            message = self._get(f"{API_BASE}/messages/{stub['id']}", format="full")
            text = _collect_text(message.get("payload") or {})
            for url in DOWNLOAD_URL_REGEX.findall(text):
                url = url.replace("&amp;", "&")
                match = ITEM_ID_REGEX.search(url)
                if not match:
                    continue
                item_id = int(match.group(1))
                # Keep the first seen; Gmail returns newest first.
                links.setdefault(item_id, url)
        log.info(f"Found {len(links)} bandcamp download link(s) in Gmail")
        return links

    def wait_for_link(self, item_id, timeout=300, poll_interval=15, newer_than="1d"):
        """Poll until the download link for one album arrives, or time out.

        Mails normally arrive within seconds, but polling makes a batch run robust.
        """
        deadline = time.monotonic() + timeout
        attempt = 0
        while True:
            attempt += 1
            links = self.find_download_links(newer_than=newer_than)
            if item_id in links:
                log.info(
                    f"Got download link for item {item_id} after {attempt} check(s)"
                )
                return links[item_id]
            if time.monotonic() >= deadline:
                raise GmailError(
                    f"No download email arrived for item {item_id} within {timeout}s. "
                    f"Check the address configured under defaults.email matches the "
                    f"authorised mailbox."
                )
            time.sleep(poll_interval)
