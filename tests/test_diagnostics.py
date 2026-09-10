import json

import pytest
from astrbot_plugin_douyin.series_diagnostics import DiagnosticBuffer, redact


def test_diagnostic_cursor_does_not_skip_pages():
    buffer = DiagnosticBuffer()
    for number in range(12):
        buffer.emit("INFO", "EVENT", "Event", {"n": number})
    first = buffer.events(limit=5)
    second = buffer.events(first["next_seq"], 5)
    third = buffer.events(second["next_seq"], 5)
    assert [
        r["seq"] for page in (first, second, third) for r in page["events"]
    ] == list(range(1, 13))


def test_ring_overflow_and_clear_stream():
    buffer = DiagnosticBuffer()
    for _ in range(1004):
        buffer.emit("DEBUG", "EVENT", "Event")
    page = buffer.events(limit=1000)
    assert len(page["events"]) == 1000 and page["dropped_before"] == 4
    buffer.clear()
    assert buffer.events()["stream_id"] != page["stream_id"]
    assert buffer.events()["events"] == []


def test_returned_records_cannot_mutate_buffer():
    buffer = DiagnosticBuffer()
    buffer.emit("INFO", "TEST", "Test", {"nested": {"value": 1}})
    buffer.events()["events"][0]["details"]["nested"]["value"] = 99
    assert buffer.events()["events"][0]["details"]["nested"]["value"] == 1


@pytest.mark.parametrize(
    "value,secret",
    [
        ({"api_key": "sk-abc"}, "sk-abc"),
        ({"nested": {"Cookie": "sid=abc123"}}, "abc123"),
        ({"error": "Authorization: Bearer private-key-123"}, "private-key-123"),
        ({"url": "https://x/a?token=top-secret&video=12345"}, "top-secret"),
        ({"error": "password='my secret'; uid=123"}, "my secret"),
        ({"error": "Cookie: sessionid=hidden1; other=hidden2\nstatus=403"}, "hidden2"),
    ],
)
def test_secrets_redacted_in_serialized_payload(value, secret):
    assert secret not in json.dumps(redact(value), ensure_ascii=False)


def test_nonsecret_evidence_is_kept():
    value = {
        "path": "D:/media/example.mp4",
        "url": "https://www.douyin.com/video/123456",
        "uid": "1234567890123456789",
        "text": "原始内容",
        "status": 403,
    }
    assert redact(value) == value
