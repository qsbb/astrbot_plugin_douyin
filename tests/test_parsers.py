import pytest
from astrbot_plugin_douyin.core.models import PluginError
from astrbot_plugin_douyin.douyin.parsers import (
    comments,
    contact,
    extract_videos,
    video_id,
    walk_records,
)


@pytest.mark.parametrize(
    "url",
    [
        "https://evil.example/video/123456",
        "http://www.douyin.com/video/123456",
        "https://www.douyin.com@evil.example/video/123456",
        "https://www.douyin.com:8443/video/123456",
        "123456;rm -rf /",
        "https://v.douyin.com/abc",
    ],
)
def test_video_reference_cannot_navigate_arbitrary_sites(url):
    with pytest.raises(PluginError):
        video_id(url)


def test_no_short_id_or_nickname_identity_fallback():
    assert contact({"short_id": "12345", "nickname": "Alice"}) is None


def test_recommendations_are_candidates_and_comments_cannot_overwrite_them():
    data = {
        "aweme_list": [
            {"aweme_id": "123456", "desc": "描述", "video": {"duration": 2000}},
            {"aweme_id": "123456", "cid": "777", "text": "评论"},
        ]
    }
    result = extract_videos(data)
    assert len(result) == 1 and result[0]["observation_kind"] == "candidate"
    assert result[0]["title"] == "描述"


def test_malformed_comments_are_skipped():
    assert (
        comments({"comments": [None, {}, {"cid": "123", "text": "正常"}]})["comments"][
            0
        ]["comment_ref"]
        == "123"
    )


def test_nested_payload_work_is_bounded():
    payload = {"a": [{"a": i} for i in range(100000)]}
    assert len(list(walk_records(payload, limit=20))) <= 20
