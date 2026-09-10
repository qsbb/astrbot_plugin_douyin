"""工具身份只来自宿主事件；平台资料以结构化结果回到原主 Agent。"""

import json

from .core.models import Caller


def caller_from_event(event) -> Caller:
    return Caller(
        umo=str(event.unified_msg_origin or ""),
        actor_id=str(event.get_sender_id() or ""),
        is_admin=event.is_admin() is True,
    )


def render(result: dict) -> str:
    return json.dumps(result, ensure_ascii=False, allow_nan=False)
