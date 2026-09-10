"""严格解析宿主配置；授权只能由管理员配置，不能由模型工具修改。"""

from collections.abc import Mapping
from dataclasses import dataclass, fields
from typing import Any

from .models import PluginError


@dataclass(frozen=True, slots=True)
class Settings:
    enabled: bool = False
    headless: bool = False
    browser_channel: str = ""
    allowed_origins: tuple[str, ...] = ()
    allowed_actor_ids: tuple[str, ...] = ()
    allowed_actions: tuple[str, ...] = ()
    allowed_target_refs: tuple[str, ...] = ()
    expected_account_ref: str = ""
    action_limit_per_hour: int = 12
    action_cooldown_seconds: int = 15
    operation_timeout_seconds: int = 45
    max_browse_items: int = 3
    browse_dwell_seconds: int = 3
    media_max_mb: int = 50
    media_max_seconds: int = 120
    media_cache_mb: int = 200
    vision_provider_id: str = ""
    stt_provider_id: str = ""
    ffmpeg_path: str = "ffmpeg"

    @classmethod
    def from_mapping(cls, config: Mapping[str, Any]) -> "Settings":
        defaults = cls()
        values: dict[str, Any] = {}
        ranges = {
            "action_limit_per_hour": (1, 100),
            "action_cooldown_seconds": (1, 3600),
            "operation_timeout_seconds": (5, 180),
            "max_browse_items": (1, 5),
            "browse_dwell_seconds": (1, 15),
            "media_max_mb": (1, 200),
            "media_max_seconds": (5, 300),
            "media_cache_mb": (10, 2000),
        }
        for item in fields(cls):
            default = getattr(defaults, item.name)
            value = config.get(item.name, default)
            valid = True
            if isinstance(default, bool):
                valid = type(value) is bool
            elif isinstance(default, int):
                low, high = ranges[item.name]
                valid = type(value) is int and low <= value <= high
            elif isinstance(default, tuple):
                valid = isinstance(value, (list, tuple)) and all(
                    isinstance(v, str) and 0 < len(v) <= 512 for v in value
                )
                if valid:
                    value = tuple(dict.fromkeys(value))
            else:
                valid = isinstance(value, str) and len(value) <= 1024
            if not valid:
                raise PluginError("CONFIG_INVALID", f"配置项 {item.name} 无效。")
            values[item.name] = value
        actions = {"set_like", "post_comment", "share_video", "send_message"}
        if not set(values["allowed_actions"]).issubset(actions):
            raise PluginError("CONFIG_INVALID", "allowed_actions 包含未知操作。")
        if values["browser_channel"] not in ("", "chrome", "msedge", "chromium"):
            raise PluginError(
                "CONFIG_INVALID", "browser_channel 必须为已支持的浏览器。"
            )
        return cls(**values)
