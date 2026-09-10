"""仅通过 AstrBot 公共 Page API 接入；身份由宿主 JWT 请求上下文提供。"""

from astrbot.api.web import error_response, json_response, request

from .core.models import PLUGIN_ID, DashboardCaller, PluginError
from .core.service import PARAMETERS


class PageApi:
    def __init__(self, context, config, service):
        self.context = context
        self.config = config
        self.service = service
        self.closed = False
        self.registered = False

    def register(self):
        register = getattr(self.context, "register_web_api", None)
        if not callable(register):
            self.service.diagnostics.emit(
                "WARNING", "PAGE_API_UNAVAILABLE", "Host Page API unavailable"
            )
            return False
        for name in (
            "status",
            "frame",
            "receipts",
            "control",
            "input",
            "action",
            "pause",
            "bind",
            "settings",
            "prepare",
        ):

            async def handler(endpoint=name):
                return await self.handle(endpoint)

            register(
                f"/{PLUGIN_ID}/page/{name}",
                handler,
                ["GET" if name in {"status", "frame", "receipts"} else "POST"],
                f"Douyin Page {name}",
            )
        self.registered = True
        return True

    @property
    def config_writable(self):
        return callable(getattr(self.config, "save_config_async", None))

    async def _save_config(self, changes, settings):
        before = dict(self.config)
        try:
            committed = await self.config.save_config_async(changes)
            if committed is False:
                raise PluginError(
                    "CONFIG_SAVE_SUPERSEDED", "配置同时被其他页面更新，请刷新后重试。"
                )
        except BaseException:
            # 仅撤回本请求仍持有的值，不覆盖宿主中另一次修改。
            for key, value in changes.items():
                if self.config.get(key) == value:
                    if key in before:
                        self.config[key] = before[key]
                    else:
                        self.config.pop(key, None)
            raise

    async def handle(self, endpoint):
        if self.closed:
            return error_response("插件已停止，请重载后重新打开页面。", status_code=503)
        try:
            caller = DashboardCaller.from_username(request.username)
        except PluginError as exc:
            return error_response(exc.message, status_code=403)
        payload = {}
        if endpoint not in {"status", "frame", "receipts"}:
            if len(await request.body()) > 16384:
                return error_response("页面请求过大。", status_code=413)
            payload = await request.json(default=None)
            if not isinstance(payload, dict):
                return error_response("请求必须是 JSON 对象。", status_code=400)
        if self.closed:
            return error_response("插件已停止。", status_code=503)
        if endpoint == "action":
            if (
                set(payload) - {"operation", "params", "request_id"}
                or not isinstance(payload.get("operation"), str)
                or payload["operation"] not in PARAMETERS
                or not isinstance(payload.get("params", {}), dict)
                or "request_id" in payload.get("params", {})
            ):
                return error_response("操作或参数格式无效。", status_code=400)
            params = dict(payload.get("params", {}))
            if "request_id" in payload:
                params["request_id"] = payload["request_id"]
            result = await self.service.execute(payload["operation"], caller, **params)
        elif endpoint == "pause":
            if set(payload) != {"paused"} or type(payload["paused"]) is not bool:
                return error_response("paused 必须为布尔值。", status_code=400)
            result = await self.service.set_paused(caller, payload["paused"])
        elif endpoint == "receipts":
            result = self.service.receipts(caller)
        else:
            result = await self.service.page_execute(
                caller,
                endpoint,
                payload,
                save_config=self._save_config if self.config_writable else None,
            )
        # 避免 SDK 解包业务 status=ok 导致 request_id 或 submitted/unknown 状态丢失。
        return json_response({"result": result}, headers={"Cache-Control": "no-store"})

    def close(self):
        # 宿主目前没有公开注销接口；卸载后的残留路由必须停止服务。
        self.closed = True
