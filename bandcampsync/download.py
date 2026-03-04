import math
import re
import shutil
from urllib.parse import unquote
from zipfile import ZipFile
from bs4 import BeautifulSoup
from curl_cffi import requests
from .logger import get_logger


log = get_logger("download")


def mask_sig(url):
    if "&sig=" not in url:
        return url
    url_parts = url.split("&")
    for i, url_part in enumerate(url_parts):
        if url_part[:4] == "sig=":
            url_parts[i] = "sig=[masked]"
        elif url_part[:6] == "token=":
            url_parts[i] = "token=[masked]"
    return "&".join(url_parts)


class DownloadBadStatusCode(ValueError):
    pass


class DownloadInvalidContentType(ValueError):
    pass



class DownloadExpired(ValueError):
    pass


def _is_expired_download_page(html):
    if not html:
        return False
    soup = BeautifulSoup(html, "html.parser")
    reauth_error = soup.select_one("div.email-reauth-error")
    if not reauth_error:
        return False
    style = (reauth_error.get("style") or "").replace(" ", "").lower()
    return "display:none" not in style


def _fetch_html_body(url):
    if not url:
        return ""
    try:
        resp = requests.get(url, stream=False, impersonate="chrome")
    except Exception:
        return ""
    try:
        return resp.text or ""
    finally:
        resp.close()


def _read_html_body(response):
    html_body = ""
    try:
        html_body = response.text or ""
    except Exception:
        pass
    if not html_body:
        try:
            html_body = response.content.decode("utf-8", errors="replace")
        except Exception:
            pass
    if not html_body:
        html_body = _fetch_html_body(response.url)
    return html_body


def _parse_content_disposition_filename(header_value):
    """Parse filename from a Content-Disposition header value.

    Handles quoted, unquoted, and RFC 5987 UTF-8 encoded forms.
    Returns the filename string or None if not found.
    """
    if not header_value:
        return None
    # Try RFC 5987 filename* first (e.g. filename*=UTF-8''Artist%20-%20Album.zip)
    match = re.search(r"filename\*\s*=\s*UTF-8''(.+?)(?:;|$)", header_value, re.IGNORECASE)
    if match:
        return unquote(match.group(1).strip())
    # Try quoted filename (e.g. filename="Artist - Album.zip")
    match = re.search(r'filename\s*=\s*"(.+?)"', header_value)
    if match:
        return match.group(1).strip()
    # Try unquoted filename (e.g. filename=Artist - Album.zip)
    match = re.search(r"filename\s*=\s*([^;]+)", header_value)
    if match:
        return match.group(1).strip()
    return None


def download_file(
    url,
    target,
    mode="wb",
    chunk_size=8192,
    logevery=10,
    disallow_content_type="text/html",
):
    """
    Attempts to stream a download to an open target file handle in chunks. If the
    request returns a disallowed content type then return a failed state with the
    response content.
    """
    text = True if "t" in mode else False
    data_streamed = 0
    last_log = 0
    content_filename = None
    r = requests.get(url, stream=True, impersonate="chrome")
    try:
        # r.raise_for_status()
        if r.status_code != 200:
            raise DownloadBadStatusCode(f"Got non-200 status code: {r.status_code}")
        try:
            content_type = r.headers.get("Content-Type", "")
        except (ValueError, KeyError):
            content_type = ""
        content_type_parts = content_type.split(";")
        major_content_type = content_type_parts[0].strip()
        if major_content_type == disallow_content_type:
            html_body = _read_html_body(r)
            if _is_expired_download_page(html_body):
                raise DownloadExpired("Download expired and requires email confirmation on Bandcamp")
            raise DownloadInvalidContentType(
                f"Invalid content type: {major_content_type}"
            )
        try:
            cd_header = r.headers.get("Content-Disposition", "")
        except (ValueError, KeyError):
            cd_header = ""
        content_filename = _parse_content_disposition_filename(cd_header)
        try:
            content_length = int(r.headers.get("Content-Length", "0"))
        except (ValueError, KeyError):
            content_length = 0
        for chunk in r.iter_content(chunk_size=chunk_size):
            data_streamed += len(chunk)
            if text:
                chunk = chunk.decode()
            target.write(chunk)
            if content_length > 0 and logevery > 0:
                percent_complete = math.floor((data_streamed / content_length) * 100)
                if percent_complete % logevery == 0 and percent_complete > last_log:
                    log.info(f"Downloading {mask_sig(url)}: {percent_complete}%")
                    last_log = percent_complete
    finally:
        r.close()
    return content_filename


def is_zip_file(file_path):
    try:
        with ZipFile(file_path) as z:
            z.infolist()
        return True
    except Exception:
        return False


def unzip_file(decompress_from, decompress_to):
    with ZipFile(decompress_from) as z:
        z.extractall(decompress_to)
    return True


def move_file(src, dst):
    return shutil.move(src, dst)


def copy_file(src, dst):
    return shutil.copyfile(src, dst)
