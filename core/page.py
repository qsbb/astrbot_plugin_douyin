"""Page 人工接管与 Bot 共用一把浏览器锁；截图、输入与凭据不落盘。"""

import asyncio
import time
from dataclasses import asdict
from uuid import uuid4

from .models import DashboardCaller, PluginError
from .settings import Settings

PAGE_CONFIG_KEYS = frozenset(
    {
        "enabled",
        "allowed_actions",
        "allowed_target_refs",
        "allowed_origins",
        "allowed_actor_ids",
    }
)
LEASE_SECONDS = 300


class PageControlMixin:
    def _init_page_control(self):
        self._page_owner = None
        self._page_deadline = 0.0

    def _control_active(self):
        if self._page_owner and time.monotonic() >= self._page_deadline:
            self._page_owner = None
            self.diagnostics.emit(
                "INFO", "PAGE_CONTROL_EXPIRED", "Manual control expired"
            )
        return self._page_owner is not None

    def _page_require(self, caller):
        if not isinstance(caller, DashboardCaller) or not caller.is_admin:
            raise PluginError("ROLE_FORBIDDEN", "页面操作需要宿主 Dashboard 身份。")
        if self._closed:
            raise PluginError("SERVICE_CLOSED", "插件已停止。")

    def _page_owned(self, caller):
        if not self._control_active() or self._page_owner != caller.actor_id:
            raise PluginError("CONTROL_REQUIRED", "请先接管 Bot 浏览器。")
        self._page_deadline = time.monotonic() + LEASE_SECONDS

    def _control_snapshot(self, caller):
        active = self._control_active()
        return {
            "active": active,
            "owned": active and self._page_owner == caller.actor_id,
            "expires_in": max(0, int(self._page_deadline - time.monotonic()))
            if active
            else 0,
        }

    async def page_execute(self, caller, operation, params=None, *, save_config=None):
        """串行执行 Dashboard 操作，复用 Bot 的浏览器与账号。

        Args:
            caller: 由宿主认证上下文创建的 DashboardCaller。
            operation: 页面内部操作名，不能经 LLM 工具分派。
            params: 对应操作的参数对象；绑定账号不接受客户端账号标识。
            save_config: 可选异步保存回调，接收已校验的变更和 Settings。

        Returns:
            douyin.result.v1 结果；业务错误转换为 failed，人工输入保留
            submitted 或 unknown_result。截图只返回给当前认证页面。

        Raises:
            asyncio.CancelledError: 请求被取消；已启动的配置事务会先完成收尾。
        """
        execution = asyncio.current_task()
        self._executions.add(execution)
        try:
            self._page_require(caller)
            params = {} if params is None else params
            if not isinstance(params, dict):
                raise PluginError("INVALID_ARGUMENT", "请求参数必须是对象。")
            async with asyncio.timeout(45):
                async with self._operation_lock:
                    self._page_require(caller)
                    data = await self._page_locked(
                        caller, operation, params, save_config
                    )
            return self._result(uuid4().hex, data)
        except PluginError as exc:
            return self._result(
                uuid4().hex,
                {
                    "status": "failed",
                    "code": exc.code,
                    "message": exc.message,
                    "details": exc.details,
                },
            )
        except TimeoutError:
            return self._result(
                uuid4().hex,
                {
                    "status": "failed",
                    "code": "PAGE_TIMEOUT",
                    "message": "浏览器忙或操作超时，请刷新状态。",
                },
            )
        except Exception as exc:
            # 不记录 Playwright 错误详情，里面可能包含用户输入的验证码或密码。
            self.diagnostics.emit(
                "WARNING",
                "PAGE_OPERATION_FAILED",
                "Page operation failed",
                {"operation": operation, "error_type": type(exc).__name__},
            )
            return self._result(
                uuid4().hex,
                {
                    "status": "failed",
                    "code": "PAGE_OPERATION_FAILED",
                    "message": "页面操作未完成，请刷新当前画面核对。",
                },
            )
        finally:
            self._executions.discard(execution)

    async def _page_locked(self, caller, operation, params, save_config):
        if operation == "status" and not params:
            return {
                **self.snapshot(),
                "browser": await self.browser.status(),
                "control": self._control_snapshot(caller),
                "expected_account_ref": self.settings.expected_account_ref,
                "config": {key: asdict(self.settings)[key] for key in PAGE_CONFIG_KEYS},
                "config_writable": save_config is not None,
                "max_browse_items": self.settings.max_browse_items,
            }
        if operation == "control" and set(params) == {"action"}:
            action = params["action"]
            if action == "acquire":
                if self._control_active() and self._page_owner != caller.actor_id:
                    raise PluginError(
                        "CONTROL_BUSY", "浏览器正由其他 Dashboard 操作者接管。"
                    )
                self._page_owner = caller.actor_id
                self._page_deadline = time.monotonic() + LEASE_SECONDS
                self.diagnostics.emit(
                    "INFO", "PAGE_CONTROL_ACQUIRED", "Manual browser control acquired"
                )
            elif action == "release":
                self._page_owned(caller)
                self._page_owner = None
                self.diagnostics.emit(
                    "INFO", "PAGE_CONTROL_RELEASED", "Manual browser control released"
                )
            elif action in {"login", "home", "inbox", "reload"}:
                self._page_owned(caller)
                return await self.browser.remote_navigate(action)
            else:
                raise PluginError("INVALID_ARGUMENT", "未知页面控制动作。")
            return {"control": self._control_snapshot(caller)}
        if operation == "frame" and not params:
            self._page_owned(caller)
            return await self.browser.remote_frame()
        if operation == "input":
            self._page_owned(caller)
            return await self.browser.remote_input(**params)
        if operation in {"settings", "bind"}:
            if self._control_active() and self._page_owner != caller.actor_id:
                raise PluginError("CONTROL_BUSY", "请等待其他操作者归还浏览器。")
            if save_config is None:
                raise PluginError(
                    "CONFIG_READ_ONLY", "当前宿主未提供异步配置保存接口。"
                )
            if operation == "bind":
                if params:
                    raise PluginError("INVALID_ARGUMENT", "绑定不接受外部账号标识。")
                account = await self.browser.status()
                if account.get("authenticated") is not True or not account.get(
                    "account_ref"
                ):
                    raise PluginError("LOGIN_REQUIRED", "请先登录并刷新账号状态。")
                changes = {"expected_account_ref": account["account_ref"]}
            else:
                if set(params) != {"config"} or not isinstance(params["config"], dict):
                    raise PluginError("INVALID_ARGUMENT", "config 必须是配置对象。")
                changes = params["config"]
                if set(changes) - PAGE_CONFIG_KEYS:
                    raise PluginError(
                        "INVALID_ARGUMENT", "配置中包含页面不支持的字段。"
                    )
            settings = Settings.from_mapping({**asdict(self.settings), **changes})

            # 保存与生效使用同一串行入口；失败时不替换运行中的配置。
            async def commit():
                await save_config(changes, settings)
                self.settings = settings
                self.browser.settings = settings
                self.analyzer.settings = settings
                if getattr(self.analyzer, "host", None) is not None:
                    self.analyzer.host.settings = settings

            transaction = asyncio.create_task(commit())
            try:
                await asyncio.shield(transaction)
            except asyncio.CancelledError:
                # 请求断开也要收尾同一次保存，避免磁盘已写入但进程仍用旧授权。
                await transaction
                raise
            self.diagnostics.emit(
                "INFO",
                "PAGE_CONFIG_SAVED",
                "Page configuration saved",
                {"keys": sorted(changes)},
            )
            return {
                "saved": True,
                "expected_account_ref": settings.expected_account_ref,
                "restart_required": False,
            }
        raise PluginError("INVALID_ARGUMENT", "未知页面请求或参数。")
