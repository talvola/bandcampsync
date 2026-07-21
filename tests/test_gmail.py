"""Tests for the Gmail download-link retrieval, using a fake session."""

import base64

from bandcampsync.gmail import GmailReader, _collect_text, _decode_part


def _b64(text):
    return base64.urlsafe_b64encode(text.encode()).decode().rstrip("=")


def _message(item_id):
    body = (
        f"Hi there,\nTo start your free download please click:\n"
        f"https://bandcamp.com/download?from=email&amp;id={item_id}"
        f"&amp;payment_id=1&amp;sig=abc&amp;type=album\n"
    )
    return {"payload": {"body": {"data": _b64(body)}}}


class _FakeReader(GmailReader):
    """Bypasses the Google libraries; records how many message bodies were fetched."""

    def __init__(self, item_ids):
        self._ids = list(item_ids)
        self.fetched = 0

    def _get(self, url, **params):
        if url.endswith("/messages"):
            return {"messages": [{"id": str(i)} for i in self._ids]}
        self.fetched += 1
        return _message(int(url.rsplit("/", 1)[-1]))


def test_extracts_link_and_normalises_entities():
    reader = _FakeReader([111])
    links = reader.find_download_links()
    assert 111 in links
    assert "&amp;" not in links[111]
    assert "id=111" in links[111]


def test_short_circuits_on_wanted_item():
    """Gmail lists newest first and each body costs a request, so looking for a specific
    album must stop at the first match rather than reading every recent mail."""
    reader = _FakeReader([999, 888, 777, 666, 555])
    links = reader.find_download_links(want_item_id=999)
    assert 999 in links
    assert reader.fetched == 1


def test_reads_all_when_no_target_given():
    reader = _FakeReader([1, 2, 3])
    links = reader.find_download_links()
    assert set(links) == {1, 2, 3}
    assert reader.fetched == 3


def test_keeps_newest_when_item_repeats():
    reader = _FakeReader([42, 42])
    assert 42 in reader.find_download_links(want_item_id=42)
    assert reader.fetched == 1


def test_collect_text_walks_nested_parts():
    payload = {
        "body": {},
        "parts": [
            {"body": {"data": _b64("plain part")}},
            {"parts": [{"body": {"data": _b64("nested part")}}], "body": {}},
        ],
    }
    text = _collect_text(payload)
    assert "plain part" in text
    assert "nested part" in text


def test_decode_part_handles_missing_padding_and_junk():
    assert _decode_part("") == ""
    assert _decode_part(None) == ""
    assert "hello" in _decode_part(_b64("hello"))
