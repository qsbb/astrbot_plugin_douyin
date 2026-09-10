"""宿主身份和可序列化的业务错误。"""

from dataclasses import dataclass, field
from typing import Any

PLUGIN_ID = "astrbot_plugin_douyin"
PLUGIN_NAME = "凝心溯溪-趣"
SERIES_ID = "ningxin_suxi"


@dataclass(frozen=True, slots=True)
class Caller:
    umo: str
    actor_id: str
    is_admin: bool = False


@dataclass(frozen=True, slots=True)
class DashboardCaller(Caller):
    """仅由公开 Web API 的宿主认证上下文创建，不接受模型或前端身份字段。"""

    @classmethod
    def from_username(cls, username: str | None) -> "DashboardCaller":
        if (
            not isinstance(username, str)
            or not username.strip()
            or len(username) > 256
            or username.startswith("api_key:")
        ):
            raise PluginError("ROLE_FORBIDDEN", "请使用 AstrBot Dashboard 管理员登录。")
        return cls("dashboard:PluginPage:douyin", f"dashboard:{username}", True)


@dataclass
class PluginError(Exception):
    code: str
    message: str
    details: dict[str, Any] = field(default_factory=dict)

    def __str__(self) -> str:
        return self.message
