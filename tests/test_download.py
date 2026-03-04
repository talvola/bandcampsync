from pathlib import Path

from bandcampsync.download import _is_expired_download_page, _parse_content_disposition_filename


def _load_fixture(name):
    html_path = Path(__file__).resolve().parent / "data" / name
    return html_path.read_text(encoding="utf-8", errors="ignore")


def test_expired_download_page():
    html = _load_fixture("download-expired.html")
    assert _is_expired_download_page(html) is True


def test_non_expired_download_page():
    html = _load_fixture("download-choose-format.html")
    assert _is_expired_download_page(html) is False


def test_parse_content_disposition_quoted():
    header = 'attachment; filename="Artist - Album.zip"'
    assert _parse_content_disposition_filename(header) == "Artist - Album.zip"


def test_parse_content_disposition_unquoted():
    header = "attachment; filename=Artist - Album.zip"
    assert _parse_content_disposition_filename(header) == "Artist - Album.zip"


def test_parse_content_disposition_utf8():
    header = "attachment; filename*=UTF-8''Artist%20-%20Album.zip"
    assert _parse_content_disposition_filename(header) == "Artist - Album.zip"


def test_parse_content_disposition_utf8_special_chars():
    header = "attachment; filename*=UTF-8''%C3%89milie%20-%20L%27album.zip"
    assert _parse_content_disposition_filename(header) == "\u00c9milie - L'album.zip"


def test_parse_content_disposition_missing():
    assert _parse_content_disposition_filename(None) is None
    assert _parse_content_disposition_filename("") is None
    assert _parse_content_disposition_filename("attachment") is None


def test_parse_content_disposition_utf8_preferred_over_plain():
    header = "attachment; filename=\"fallback.zip\"; filename*=UTF-8''Preferred%20-%20Name.zip"
    assert _parse_content_disposition_filename(header) == "Preferred - Name.zip"
