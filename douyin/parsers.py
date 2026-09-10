"""将页面响应转换为稳定标识与来源证据，不依据昵称猜测账号。"""

import math
import re
from datetime import UTC, datetime
from urllib.parse import urlsplit

from ..core.models import PluginError


def video_id(value: str) -> str:
    if re.fullmatch(r"\d{5,30}", value):
        return value
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError as exc:
        raise PluginError("INVALID_VIDEO_REF", "作品链接格式无效。") from exc
    if (
        parsed.scheme == "https"
        and parsed.hostname in {"www.douyin.com", "douyin.com"}
        and not parsed.username
        and port in (None, 443)
    ):
        match = re.fullmatch(r"/video/(\d{5,30})/?", parsed.path)
        if match:
            return match[1]
    raise PluginError(
        "INVALID_VIDEO_REF", "请提供抖音作品 ID 或 www.douyin.com/video/ID 的完整链接。"
    )


def contact(raw: dict) -> dict | None:
    if not isinstance(raw, dict):
        return None
    uid = str(raw.get("uid") or raw.get("user_id") or "")
    sec_uid = str(raw.get("sec_uid") or "")
    if uid and not re.fullmatch(r"\d{2,30}", uid):
        uid = ""
    if sec_uid and not re.fullmatch(r"[A-Za-z0-9_-]{3,160}", sec_uid):
        sec_uid = ""
    if not uid and not sec_uid:
        return None
    return {
        "target_ref": f"user:{uid}" if uid else f"sec_user:{sec_uid}",
        "uid": uid,
        "sec_uid": sec_uid,
        "nickname": str(raw.get("nickname") or "")[:200],
        "profile_url": f"https://www.douyin.com/user/{sec_uid}" if sec_uid else "",
    }


def video(raw: dict) -> dict | None:
    identifier = str(raw.get("aweme_id") or "")
    if not re.fullmatch(r"\d{5,30}", identifier):
        return None
    media = raw.get("video") if isinstance(raw.get("video"), dict) else {}
    play = media.get("play_addr") or {}
    urls = play.get("url_list") if isinstance(play, dict) else []
    url = next(
        (u for u in (urls or []) if isinstance(u, str) and u.startswith("https://")), ""
    )
    created = raw.get("create_time")
    try:
        published = (
            datetime.fromtimestamp(int(created), UTC).isoformat() if created else None
        )
    except (ValueError, TypeError, OverflowError, OSError):
        published = None
    duration = media.get("duration") or raw.get("duration") or 0
    if (
        not isinstance(duration, (int, float))
        or not math.isfinite(duration)
        or duration < 0
        or duration > 86400000
    ):
        duration = 0
    liked = raw.get("user_digged")
    return {
        "video_ref": identifier,
        "canonical_url": f"https://www.douyin.com/video/{identifier}",
        "title": str(raw.get("desc") or "")[:4000],
        "author": contact(raw.get("author") or {}),
        "duration_ms": duration,
        "published_at": published,
        "media": {"video_url": url},
        "liked": bool(liked) if liked in (0, 1) else None,
        "observation_kind": "candidate",
        "evidence": [],
    }


def walk_records(payload, *, limit: int = 500):
    """有界遍历兼容推荐/搜索的列表包裹；不给平台数据执行代码的机会。"""
    stack, count = [payload], 0
    while stack and count < limit:
        node = stack.pop()
        count += 1
        if isinstance(node, dict):
            yield node
            stack.extend(
                value for value in node.values() if isinstance(value, (dict, list))
            )
        elif isinstance(node, list):
            stack.extend(reversed(node[:100]))


def extract_videos(payload) -> list[dict]:
    seen, rows = set(), []
    for raw in walk_records(payload):
        # 评论也有 aweme_id，必须具有作品字段，不能覆盖完整作品信息。
        if "aweme_id" not in raw or not ("video" in raw or "desc" in raw):
            continue
        item = video(raw)
        if item and item["video_ref"] not in seen:
            seen.add(item["video_ref"])
            rows.append(item)
    return rows


def extract_contacts(payload) -> list[dict]:
    seen, rows = set(), []
    for raw in walk_records(payload):
        if "nickname" not in raw:
            continue
        item = contact(raw)
        if item and item["target_ref"] not in seen:
            seen.add(item["target_ref"])
            rows.append(item)
    return rows


def comments(payload: dict) -> dict:
    rows = []
    for raw in (payload.get("comments") or [])[:100]:
        if not isinstance(raw, dict) or not raw.get("cid"):
            continue
        rows.append(
            {
                "comment_ref": str(raw["cid"]),
                "video_ref": str(raw.get("aweme_id") or ""),
                "text": str(raw.get("text") or "")[:4000],
                "author": contact(raw.get("user") or {}),
                "reply_to": str(raw.get("reply_id") or ""),
                "mentions": raw.get("text_extra") or [],
            }
        )
    return {
        "comments": rows,
        "cursor": str(payload.get("cursor") or "0"),
        "has_more": bool(payload.get("has_more")),
    }
