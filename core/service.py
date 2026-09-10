"""一次有边界的抖音任务；人格、长期记忆和主动调度由宿主承担。"""

import asyncio
import hashlib
import json
import math
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import uuid4

from ..douyin.parsers import video_id
from ..series_diagnostics import redact
from .models import Caller, DashboardCaller, PluginError
from .page import PageControlMixin
from .settings import Settings
from .store import StateStore

WRITES = {"set_like", "post_comment", "share_video", "send_message"}
PARAMETERS = {
    "status": set(),
    "receipt": set(),
    "browse": {"limit", "dwell_seconds"},
    "search": {"query", "limit"},
    "watch": {"video_ref", "depth", "question"},
    "read_comments": {"video_ref", "limit", "cursor"},
    "resolve_contact": {"query", "limit"},
    "read_inbox": {"conversation_ref", "limit", "cursor"},
    "set_like": {"video_ref", "liked"},
    "post_comment": {"video_ref", "text", "reply_to", "mentions"},
    "share_video": {"video_ref", "target_ref"},
    "send_message": {"conversation_ref", "text"},
    "task": {"task_id", "cancel"},
}


class DouyinService(PageControlMixin):
    def __init__(
        self,
        data_dir: Path,
        settings: Settings,
        browser,
        analyzer,
        diagnostics,
        *,
        browser_runtime=None,
    ):
        self.settings = settings
        self.browser = browser
        self.analyzer = analyzer
        self.diagnostics = diagnostics
        self.browser_runtime = browser_runtime
        self.store = StateStore(data_dir)
        self._operation_lock = asyncio.Lock()
        self._jobs: dict[str, dict] = {}
        self._closed = False
        self._executions: set[asyncio.Task] = set()
        self._inflight: asyncio.Task | None = None
        self._init_page_control()

    def snapshot(self) -> dict:
        return {
            "enabled": self.settings.enabled,
            "paused": self.store.state["paused"],
            "closed": self._closed,
            "active_tasks": sum(j["status"] == "running" for j in self._jobs.values()),
            "account_bound": bool(self.settings.expected_account_ref),
            "authorized_actions": list(self.settings.allowed_actions),
            "unresolved_actions": sum(
                row["receipt"].get("status") in ("pending", "unknown_result")
                for row in self.store.state["actions"].values()
            ),
        }

    def _authorize(self, caller: Caller, operation: str, params: dict) -> None:
        if not isinstance(caller, Caller) or not caller.umo or not caller.actor_id:
            raise PluginError("ROLE_REQUIRED", "缺少可信宿主会话与调用者身份。")
        if self._closed:
            raise PluginError("SERVICE_CLOSED", "插件已停止。")
        listed = (
            isinstance(caller, DashboardCaller)
            and caller.is_admin
            or (
                caller.umo in self.settings.allowed_origins
                and caller.actor_id in self.settings.allowed_actor_ids
            )
        )
        if operation == "status" and caller.is_admin:
            return
        if not listed:
            raise PluginError("ROLE_FORBIDDEN", "当前会话或调用者未获得抖音操作授权。")
        if operation in {"task", "receipt"}:
            return
        if self._control_active():
            raise PluginError(
                "ACCOUNT_CONTROLLED", "浏览器正在页面中被人工接管，请先归还。"
            )
        if not self.settings.enabled:
            raise PluginError(
                "PLUGIN_DISABLED", "请管理员先在 AstrBot 配置中启用插件。"
            )
        if self.store.state["paused"]:
            raise PluginError("ACCOUNT_PAUSED", "抖音账号操作已暂停。")
        if operation in WRITES:
            if not caller.is_admin:
                raise PluginError(
                    "ROLE_FORBIDDEN", "站内写操作需要管理员角色及动作授权。"
                )
            if operation not in self.settings.allowed_actions:
                raise PluginError(
                    "ACTION_NOT_GRANTED", "该动作尚未获得管理员的持续授权。"
                )
            if not self.settings.expected_account_ref:
                raise PluginError("ACCOUNT_NOT_BOUND", "请管理员先核对并绑定抖音账号。")
            targets = []
            if operation == "share_video":
                targets.append(params["target_ref"])
            if operation == "send_message":
                targets.append(params["conversation_ref"])
            if operation == "post_comment":
                for mention in params.get("mentions", []):
                    target = mention.get("target_ref")
                    if not target:
                        raise PluginError(
                            "TARGET_REQUIRED", "提及必须包含已解析的 target_ref。"
                        )
                    targets.append(target)
            if any(
                target not in self.settings.allowed_target_refs for target in targets
            ):
                raise PluginError(
                    "TARGET_NOT_GRANTED", "接收对象或被提及用户不在授权名单中。"
                )

    def _validate(self, operation: str, params: dict) -> None:
        if operation not in PARAMETERS:
            raise PluginError("UNKNOWN_OPERATION", "未知抖音操作。")
        if set(params) - PARAMETERS[operation]:
            raise PluginError("INVALID_ARGUMENT", "包含当前操作不支持的参数。")
        required = {
            "search": ("query",),
            "watch": ("video_ref",),
            "read_comments": ("video_ref",),
            "resolve_contact": ("query",),
            "set_like": ("video_ref", "liked"),
            "post_comment": ("video_ref", "text"),
            "share_video": ("video_ref", "target_ref"),
            "send_message": ("conversation_ref", "text"),
            "task": ("task_id",),
        }
        if any(key not in params for key in required.get(operation, ())):
            raise PluginError("INVALID_ARGUMENT", "缺少必要参数。")
        for key, value in params.items():
            if key in ("liked", "cancel"):
                valid = type(value) is bool
            elif key in ("limit", "dwell_seconds"):
                cap = 50
                if operation == "browse":
                    cap = self.settings.max_browse_items if key == "limit" else 15
                valid = type(value) is int and 1 <= value <= cap
            elif key == "mentions":
                valid = (
                    isinstance(value, list)
                    and len(value) <= 5
                    and all(
                        isinstance(m, dict)
                        and set(m)
                        <= {"target_ref", "uid", "sec_uid", "nickname", "text"}
                        and all(
                            isinstance(v, str) and len(v) <= 512 for v in m.values()
                        )
                        and m.get("target_ref")
                        for m in value
                    )
                )
            else:
                maximum = 2000 if key in ("text", "question") else 512
                valid = isinstance(value, str) and len(value) <= maximum
                if key in required.get(operation, ()):
                    valid = valid and bool(value.strip())
            if not valid:
                raise PluginError("INVALID_ARGUMENT", f"参数 {key} 无效或超出上限。")
        if operation == "watch" and params.get("depth", "preview") not in (
            "metadata",
            "preview",
            "full",
        ):
            raise PluginError(
                "INVALID_ARGUMENT", "depth 必须为 metadata、preview 或 full。"
            )
        if "video_ref" in params:
            params["video_ref"] = video_id(params["video_ref"])

    async def execute(self, operation: str, caller: Caller, **params) -> dict:
        """以可信调用者身份执行一次语义操作，并保留可追踪的结果。

        Args:
            operation: PARAMETERS 中声明的操作名。
            caller: 来自宿主事件或 Dashboard 认证上下文的身份。
            **params: 该操作的参数；写操作和回执查询必须提供 request_id。

        Returns:
            douyin.result.v1 结果。已提交但不能确认的写动作返回
            unknown_result，并保留相同 request_id 的幂等记录。

        Raises:
            asyncio.CancelledError: 读取或排队阶段被取消；已进入写入阶段的
                中断由执行器记录为未知结果，避免自动重复发送。
        """
        execution = asyncio.current_task()
        self._executions.add(execution)
        request_id = params.pop("request_id", None)
        supplied_id = request_id is not None
        request_id = request_id if supplied_id else uuid4().hex
        try:
            if not isinstance(request_id, str) or not re.fullmatch(
                r"[A-Za-z0-9_.:-]{8,128}", request_id
            ):
                raise PluginError(
                    "INVALID_REQUEST_ID",
                    "request_id 必须是 8–128 位字母、数字或 ._:-。",
                )
            if operation in WRITES | {"receipt"} and not supplied_id:
                raise PluginError(
                    "REQUEST_ID_REQUIRED", "站内写操作必须提供可复用的 request_id。"
                )
            self._validate(operation, params)
            self._authorize(caller, operation, params)
            if operation == "task":
                data = await self._task(caller, **params)
            elif operation == "receipt":
                scope = f"{caller.umo}\0{caller.actor_id}\0{self.settings.expected_account_ref}"
                key = hashlib.sha256(f"{scope}\0{request_id}".encode()).hexdigest()
                record = self.store.state["actions"].get(key)
                if record is None:
                    raise PluginError("RECEIPT_NOT_FOUND", "当前会话没有该操作回执。")
                data = dict(record["receipt"])
                if data.get("status") == "pending":
                    data.update(status="unknown_result", code="ACTION_NOT_FINALIZED")
            else:
                # 串行租用单账号页面；排队也受超时限制，队列超时不会产生写操作。
                async with asyncio.timeout(self.settings.operation_timeout_seconds):
                    async with self._operation_lock:
                        self._inflight = execution
                        self._authorize(caller, operation, params)
                        if operation in WRITES:
                            data = await self._write(
                                operation, caller, request_id, params
                            )
                        elif operation == "status":
                            data = {**self.snapshot(), **await self.browser.status()}
                        elif operation == "watch":
                            data = await self._watch(caller, params)
                        else:
                            if operation == "browse":
                                params.setdefault(
                                    "limit", self.settings.max_browse_items
                                )
                                params.setdefault(
                                    "dwell_seconds", self.settings.browse_dwell_seconds
                                )
                            method = getattr(self.browser, operation)
                            data = await method(**params)
            self.diagnostics.emit(
                "INFO",
                "OPERATION_RESULT",
                "Douyin operation completed",
                {
                    "operation": operation,
                    "request_id": request_id,
                    "status": data.get("status", "ok"),
                },
            )
            return self._result(request_id, data)
        except PluginError as exc:
            self.diagnostics.emit(
                "WARNING",
                exc.code,
                "Douyin operation rejected",
                {
                    "operation": operation,
                    "request_id": request_id,
                    "details": exc.details,
                },
            )
            return self._result(
                request_id,
                {
                    "status": "failed",
                    "code": exc.code,
                    "message": exc.message,
                    "details": exc.details,
                },
            )
        except TimeoutError:
            return self._result(
                request_id,
                {
                    "status": "failed",
                    "code": "OPERATION_TIMEOUT",
                    "message": "操作超时，请检查状态后再决定下一步。",
                },
            )
        except Exception as exc:
            self.diagnostics.emit(
                "ERROR",
                "OPERATION_FAILED",
                "Douyin operation failed",
                {"operation": operation, "error": str(exc)},
            )
            return self._result(
                request_id,
                {
                    "status": "failed",
                    "code": "OPERATION_FAILED",
                    "message": "操作失败，可在系列诊断中查看具体阶段。",
                },
            )
        finally:
            self._executions.discard(execution)
            if self._inflight is execution:
                self._inflight = None

    def _result(self, request_id: str, data: dict) -> dict:
        # 内部带签名媒体 URL 仅供下载器；工具层不传播它，也不将网页文本当指令。
        def public(value: Any) -> Any:
            if isinstance(value, dict):
                return {
                    key: public(item)
                    for key, item in value.items()
                    if key
                    not in {
                        "media",
                        "video_url",
                        "audio_url",
                        "cookies",
                        "storage_state",
                    }
                }
            if isinstance(value, (tuple, list)):
                return [public(item) for item in value]
            if isinstance(value, float) and not math.isfinite(value):
                return None
            return value

        clean = redact(public(data))
        return {
            "schema_version": "douyin.result.v1",
            "request_id": request_id
            if isinstance(request_id, str)
            and re.fullmatch(r"[A-Za-z0-9_.:-]{8,128}", request_id)
            else uuid4().hex,
            "status": clean.get("status", "ok"),
            "code": clean.get("code", "OK"),
            "source_trust": "untrusted_platform_content",
            "data": clean,
        }

    async def _write(
        self, operation: str, caller: Caller, request_id: str, params: dict
    ) -> dict:
        scope = f"{caller.umo}\0{caller.actor_id}\0{self.settings.expected_account_ref}"
        key = hashlib.sha256(f"{scope}\0{request_id}".encode()).hexdigest()
        fingerprint = hashlib.sha256(
            json.dumps(
                {"operation": operation, "params": params},
                ensure_ascii=False,
                sort_keys=True,
            ).encode()
        ).hexdigest()
        previous = self.store.state["actions"].get(key)
        if previous:
            if previous["fingerprint"] != fingerprint:
                raise PluginError(
                    "REQUEST_ID_CONFLICT", "同一 request_id 不能用于不同操作参数。"
                )
            saved = previous["receipt"]
            if saved.get("status") == "pending":
                saved = {
                    **saved,
                    "status": "unknown_result",
                    "code": "INTERRUPTED_AFTER_RESERVATION",
                }
            return {**saved, "replayed": True}
        status = await self.browser.status()
        if not status.get("authenticated"):
            raise PluginError(
                status.get("code", "LOGIN_REQUIRED"),
                "当前抖音会话不可用，请管理员检查登录或人工验证状态。",
            )
        account = status.get("account_ref", "")
        if account != self.settings.expected_account_ref:
            raise PluginError("ACCOUNT_MISMATCH", "当前浏览器账号与已授权账号不一致。")
        now = datetime.now(UTC)
        recent = [
            r
            for r in self.store.state["actions"].values()
            if datetime.fromisoformat(r["created_at"]) > now - timedelta(hours=1)
        ]
        if len(recent) >= self.settings.action_limit_per_hour:
            raise PluginError("RATE_LIMITED", "已达到每小时站内操作额度。")
        if (
            recent
            and (
                now - max(datetime.fromisoformat(r["created_at"]) for r in recent)
            ).total_seconds()
            < self.settings.action_cooldown_seconds
        ):
            raise PluginError("ACTION_COOLDOWN", "站内操作仍在冷却时间内。")
        if operation != "set_like" and any(
            r["fingerprint"] == fingerprint
            and (now - datetime.fromisoformat(r["created_at"])).total_seconds() < 300
            for r in recent
        ):
            raise PluginError(
                "DUPLICATE_ACTION", "五分钟内已有相同动作，请查询原 request_id 的结果。"
            )
        receipt = {
            "schema_version": "douyin.receipt.v1",
            "action_id": key,
            "operation": operation,
            "request_id": request_id,
            "account_ref": account,
            "status": "pending",
            "code": "RESERVED",
            "created_at": now.isoformat(),
        }
        receipt.update(
            {
                key: value
                for key, value in params.items()
                if key
                in {"video_ref", "target_ref", "conversation_ref", "reply_to", "liked"}
            }
        )
        if params.get("text"):
            receipt["content_sha256"] = hashlib.sha256(
                params["text"].encode()
            ).hexdigest()
        record = {
            "fingerprint": fingerprint,
            "created_at": now.isoformat(),
            "receipt": receipt,
        }
        await self.store.update(lambda state: state["actions"].__setitem__(key, record))
        try:
            # 写日志后再次检查暂停开关。浏览器适配器还需在点击前核对账号。
            self._authorize(caller, operation, params)
            async with asyncio.timeout(self.settings.operation_timeout_seconds):
                outcome = await getattr(self.browser, operation)(**params)
            if outcome.get("status") not in {
                "verified",
                "submitted",
                "unknown_result",
                "failed",
            }:
                outcome = {
                    "status": "unknown_result",
                    "code": "INVALID_ACTION_RECEIPT",
                    "message": "适配器未提供可确认的操作结果。",
                }
            receipt.update(outcome)
        except PluginError as exc:
            # 适配器必须在跨过提交点后返回 unknown_result，不抛预检异常。
            receipt.update(
                status="failed", code=exc.code, message=exc.message, details=exc.details
            )
        except (asyncio.CancelledError, TimeoutError):
            receipt.update(
                status="unknown_result",
                code="ACTION_INTERRUPTED",
                message="操作中断，可能已经提交。请核对平台结果，切勿自动重发。",
            )
        except Exception as exc:
            receipt.update(
                status="unknown_result",
                code="ACTION_RESULT_UNKNOWN",
                message="提交过程中出错，平台结果未知。",
                details={"error": str(exc)},
            )
        receipt = redact(receipt)
        try:
            await self.store.update(
                lambda state: state["actions"][key].__setitem__("receipt", receipt)
            )
        except Exception:
            # 已落盘的 pending 能阻止重启重发；本次也绝不声称可靠完成。
            receipt = {
                **receipt,
                "status": "unknown_result",
                "code": "RECEIPT_SAVE_FAILED",
                "message": "操作回执未能可靠保存，需要人工核对。",
            }
        return receipt

    async def _watch(self, caller: Caller, params: dict) -> dict:
        depth = params.get("depth", "preview")
        if (
            depth != "metadata"
            and sum(j["status"] == "running" for j in self._jobs.values()) >= 2
        ):
            raise PluginError("TASK_LIMIT", "已有两个视频感知任务，请先等待或取消。")
        video = await self.browser.watch(params["video_ref"])
        if depth == "metadata":
            return video
        # 视频帧和音轨分析独立执行；浏览器页面立即归还给其他任务。
        task_id = uuid4().hex
        job = {
            "task_id": task_id,
            "owner": (caller.umo, caller.actor_id),
            "status": "running",
            "result": None,
        }
        self._jobs[task_id] = job

        async def run():
            try:
                async with asyncio.timeout(300):
                    result = await self.analyzer.analyze(
                        video, depth, params.get("question", ""), caller.umo
                    )
                job.update(status="completed", result=self._result(task_id, result))
            except asyncio.CancelledError:
                job.update(status="cancelled", result={"code": "TASK_CANCELLED"})
            except Exception as exc:
                code = exc.code if isinstance(exc, PluginError) else "ANALYSIS_FAILED"
                self.diagnostics.emit(
                    "WARNING",
                    code,
                    "Video analysis failed",
                    {"task_id": task_id, "error": str(exc)},
                )
                job.update(
                    status="failed",
                    result={
                        "code": code,
                        "message": "视频感知未能完成，仍可读取作品描述。",
                    },
                )

        job["task"] = asyncio.create_task(run(), name=f"douyin-watch-{task_id}")
        # 已结束的任务最多保留最近 32 个；不持久化内容或人格记忆。
        finished = [
            key for key, item in self._jobs.items() if item["status"] != "running"
        ]
        for key in finished[:-32]:
            self._jobs.pop(key)
        return {
            "status": "running",
            "code": "ANALYSIS_STARTED",
            "task_id": task_id,
            "video": video,
            "message": "已读取视频，内容感知正在进行，请用 douyin_task 查询。",
        }

    async def _task(self, caller: Caller, task_id: str, cancel: bool = False) -> dict:
        job = self._jobs.get(task_id)
        if not job or job["owner"] != (caller.umo, caller.actor_id):
            raise PluginError("TASK_NOT_FOUND", "任务不存在、已过期或属于其他会话。")
        if cancel and job["status"] == "running":
            job["task"].cancel()
            await asyncio.gather(job["task"], return_exceptions=True)
            if job["status"] == "running":
                job.update(status="cancelled", result={"code": "TASK_CANCELLED"})
        return {"task_id": task_id, "status": job["status"], "result": job["result"]}

    async def login(self, caller: Caller) -> dict:
        if not caller.is_admin or not caller.umo or not caller.actor_id:
            return self._result(
                uuid4().hex,
                {
                    "status": "failed",
                    "code": "ROLE_FORBIDDEN",
                    "message": "仅管理员可以打开登录浏览器。",
                },
            )

        if self._closed:
            return self._result(
                uuid4().hex, {"status": "failed", "code": "SERVICE_CLOSED"}
            )
        execution = asyncio.current_task()
        self._executions.add(execution)
        try:
            async with asyncio.timeout(self.settings.operation_timeout_seconds):
                async with self._operation_lock:
                    if self._control_active():
                        raise PluginError(
                            "ACCOUNT_CONTROLLED",
                            "浏览器已被 Page 接管，请在页面中登录。",
                        )
                    return self._result(uuid4().hex, await self.browser.start_login())
        except PluginError as exc:
            return self._result(
                uuid4().hex,
                {"status": "failed", "code": exc.code, "message": exc.message},
            )
        except Exception as exc:
            self.diagnostics.emit(
                "WARNING", "LOGIN_FAILED", "Login browser failed", {"error": str(exc)}
            )
            return self._result(
                uuid4().hex,
                {
                    "status": "failed",
                    "code": "LOGIN_FAILED",
                    "message": "无法打开浏览器，请检查运行环境和系列诊断。",
                },
            )
        finally:
            self._executions.discard(execution)

    def receipts(self, caller: Caller) -> dict:
        if not caller.is_admin or not caller.umo or not caller.actor_id:
            return self._result(
                uuid4().hex, {"status": "failed", "code": "ROLE_FORBIDDEN"}
            )
        rows = list(self.store.state["actions"].values())[-20:]
        return self._result(
            uuid4().hex, {"receipts": [row["receipt"] for row in reversed(rows)]}
        )

    async def set_paused(self, caller: Caller, paused: bool) -> dict:
        if not caller.is_admin or not caller.umo or not caller.actor_id:
            return self._result(
                uuid4().hex, {"status": "failed", "code": "ROLE_FORBIDDEN"}
            )
        if type(paused) is not bool:
            return self._result(
                uuid4().hex, {"status": "failed", "code": "INVALID_ARGUMENT"}
            )
        await self.store.update(lambda state: state.__setitem__("paused", paused))
        if paused:
            active = self._inflight
            if (
                active is not None
                and active is not asyncio.current_task()
                and not active.done()
            ):
                active.cancel()
                await asyncio.wait({active}, timeout=5)
            for job in self._jobs.values():
                if job["status"] == "running":
                    job["task"].cancel()
            await asyncio.gather(
                *(j["task"] for j in self._jobs.values()), return_exceptions=True
            )
            for job in self._jobs.values():
                if job["status"] == "running":
                    job.update(status="cancelled", result={"code": "TASK_CANCELLED"})
        self.diagnostics.emit(
            "INFO", "PAUSE_CHANGED", "Account pause changed", {"paused": paused}
        )
        return self._result(
            uuid4().hex,
            {
                "status": "ok",
                "paused": paused,
                "message": "已暂停后续操作；已提交动作仍需核对。"
                if paused
                else "已恢复后续操作。",
            },
        )

    async def close(self) -> None:
        self._closed = True
        self._page_owner = None
        current = asyncio.current_task()
        active = [
            task for task in self._executions if task is not current and not task.done()
        ]
        for task in active:
            task.cancel()
        if active:
            await asyncio.wait(active, timeout=5)
        for job in self._jobs.values():
            job["task"].cancel()
        await asyncio.gather(
            *(j["task"] for j in self._jobs.values()), return_exceptions=True
        )
        # 先停止网络和浏览器，不能因为其中一个清理失败而泄漏另一个。
        cleanup = [self.browser.close(), self.analyzer.close()]
        if self.browser_runtime is not None:
            cleanup.append(self.browser_runtime.close())
        await asyncio.gather(*cleanup, return_exceptions=True)
