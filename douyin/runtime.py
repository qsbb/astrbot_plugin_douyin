"""浏览器依赖由插件在后台准备；宿主和其他插件的环境保持独立。"""

import asyncio
import json
import os
import re
import signal
import subprocess
import sys
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path

from ..core.models import PluginError
from ..series_diagnostics import redact
from .runtime_worker import EVENT_PREFIX

PREPARING_STATES = {
    "idle",
    "checking",
    "installing_browser",
    "installing_dependencies",
    "verifying",
}


class ManagedBrowserRuntime:
    def __init__(self, data_dir: Path, channel: str, diagnostics):
        self.root = (data_dir / "browser-runtime").resolve()
        self.channel = channel
        self.diagnostics = diagnostics
        self._state = "external" if channel in {"chrome", "msedge"} else "idle"
        self._message = (
            "使用已配置的系统浏览器。"
            if self._state == "external"
            else "等待准备浏览器。"
        )
        self._detail = ""
        self._error_code = ""
        self._executable = None
        self._version = ""
        self._force_install = False
        self._task = None
        self._process = None
        self._closed = False
        self.timeout_seconds = 900

    def snapshot(self) -> dict:
        return {
            "state": self._state,
            "message": self._message,
            "detail": self._detail,
            "error_code": self._error_code,
            "managed": self.channel not in {"chrome", "msedge"},
        }

    def start(self, retry: bool = False) -> None:
        """安排后台准备，不在插件加载或 Page 请求中等待下载。

        Args:
            retry: 是否明确重试上次失败；已有任务和已就绪状态始终复用。

        Returns:
            无返回值；通过 snapshot 查询进度。
        """
        if (
            self._closed
            or self._state in {"ready", "external"}
            or (self._task is not None and not self._task.done())
        ):
            return
        if self._state == "failed" and not retry:
            return
        self._force_install = retry and self._error_code in {
            "BROWSER_DOWNLOAD_FAILED",
            "BROWSER_RUNTIME_VERIFY_FAILED",
        }
        self._state, self._message = "checking", "正在检查浏览器运行环境。"
        self._detail = self._error_code = ""
        self._executable = None
        self._version = ""
        self._task = asyncio.create_task(self._prepare(), name="douyin-browser-prepare")

    async def launch_options(self) -> dict:
        """返回已准备的启动参数，未就绪时让调用者展示准备进度。

        Returns:
            系统浏览器 channel 或匹配当前 Playwright 的 executable_path。

        Raises:
            PluginError: 浏览器正在准备、准备失败或管理器已停止。
        """
        if self._closed:
            raise PluginError("SERVICE_CLOSED", "浏览器运行环境已停止。")
        if self._state == "external":
            return {"channel": self.channel}
        if self._state == "ready":
            try:
                installed_version = await asyncio.to_thread(version, "playwright")
            except PackageNotFoundError:
                installed_version = ""
            if self._version != installed_version or not await asyncio.to_thread(
                self._executable.is_file
            ):
                self._state = "idle"
        self.start()
        if self._state == "ready":
            return {"executable_path": str(self._executable)}
        if self._state == "failed":
            raise PluginError(
                self._error_code or "BROWSER_PREPARATION_FAILED",
                self._message,
                {"runtime": self.snapshot()},
            )
        raise PluginError(
            "BROWSER_PREPARING", self._message, {"runtime": self.snapshot()}
        )

    @staticmethod
    def _safe_detail(value: str) -> str:
        value = re.sub(r"\x1b\[[0-?]*[ -/]*[@-~]", "", value)
        value = re.sub(r"(?i)(https?://)[^\s/]+@", r"\1<hidden>@", value)
        return str(redact(value))[-2000:]

    def _event(self, line: str) -> None:
        if not line.startswith(EVENT_PREFIX):
            if self._state == "installing_browser":
                percentages = re.findall(r"\b(\d{1,3})%", line)
                if percentages and 0 <= int(percentages[-1]) <= 100:
                    self._detail = f"{percentages[-1]}%"
            return
        data = json.loads(line[len(EVENT_PREFIX) :])
        state = data.get("state")
        if state not in PREPARING_STATES | {"ready", "failed"}:
            return
        # 子进程报告 ready 后，还要等其成功退出再向调用者开放启动。
        if state == "ready":
            self._executable = Path(data["executable_path"])
            self._version = str(data.get("playwright_version", ""))
            return
        previous = self._state
        self._state = state
        self._message = self._safe_detail(str(data.get("message", "")))[:500]
        self._detail = self._safe_detail(str(data.get("detail", "")))
        self._error_code = str(data.get("error_code", ""))[:80]
        if previous != state:
            self.diagnostics.emit(
                "WARNING" if state == "failed" else "INFO",
                "BROWSER_RUNTIME_STATE",
                "Browser preparation state changed",
                self.snapshot(),
            )

    async def _terminate_process(self, process):
        if process.returncode is not None:
            return
        if os.name == "nt":
            killer = await asyncio.create_subprocess_exec(
                "taskkill",
                "/PID",
                str(process.pid),
                "/T",
                "/F",
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
                creationflags=subprocess.CREATE_NO_WINDOW,
            )
            await asyncio.wait_for(killer.wait(), 10)
        else:
            try:
                os.killpg(process.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
        try:
            await asyncio.wait_for(process.wait(), 5)
        except TimeoutError:
            if os.name == "nt":
                process.kill()
            else:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            await asyncio.wait_for(process.wait(), 5)

    async def _prepare(self):
        process = None
        try:
            temporary = self.root / "tmp"
            await asyncio.to_thread(temporary.mkdir, parents=True, exist_ok=True)
            env = {
                **os.environ,
                "PLAYWRIGHT_BROWSERS_PATH": str(self.root / "browsers"),
                "TEMP": str(temporary),
                "TMP": str(temporary),
                "TMPDIR": str(temporary),
                "PYTHONDONTWRITEBYTECODE": "1",
                "PLAYWRIGHT_DOWNLOAD_CONNECTION_TIMEOUT": "120000",
            }
            options = (
                {
                    "creationflags": subprocess.CREATE_NO_WINDOW
                    | subprocess.CREATE_NEW_PROCESS_GROUP
                }
                if os.name == "nt"
                else {"start_new_session": True}
            )
            spawn = asyncio.create_task(
                asyncio.create_subprocess_exec(
                    sys.executable,
                    str(Path(__file__).with_name("runtime_worker.py")),
                    str(self.root),
                    *(["--force"] if self._force_install else []),
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.STDOUT,
                    stdin=asyncio.subprocess.DEVNULL,
                    env=env,
                    **options,
                )
            )
            try:
                process = await asyncio.shield(spawn)
            except asyncio.CancelledError:
                process = await spawn
                raise
            self._process = process
            pending = ""
            tail = ""
            overflow = False
            async with asyncio.timeout(self.timeout_seconds):
                while chunk := await process.stdout.read(4096):
                    pending += chunk.decode("utf-8", errors="replace").replace(
                        "\r", "\n"
                    )
                    while "\n" in pending:
                        line, pending = pending.split("\n", 1)
                        if not overflow:
                            safe = self._safe_detail(line)
                            tail = (tail + "\n" + safe)[-4000:]
                            self._event(line)
                        overflow = False
                    if len(pending) > 65536:
                        pending = ""
                        overflow = True
                if pending and not overflow:
                    self._event(pending)
                returncode = await process.wait()
            if self._closed:
                return
            if returncode != 0 or self._executable is None:
                if self._state != "failed":
                    self._state, self._error_code = (
                        "failed",
                        "BROWSER_PREPARATION_FAILED",
                    )
                    self._message = "浏览器准备未完成，请点击重试。"
                if not self._detail:
                    self._detail = self._safe_detail(tail)
                self.diagnostics.emit(
                    "WARNING",
                    self._error_code,
                    "Browser preparation did not complete",
                    {"detail": self._detail},
                )
                return
            executable = self._executable.resolve()
            if not executable.is_relative_to(
                (self.root / "browsers").resolve()
            ) or not await asyncio.to_thread(executable.is_file):
                raise RuntimeError("Prepared browser path is invalid")
            self._executable = executable
            self._state, self._message = "ready", "浏览器已就绪。"
            self._detail = self._error_code = ""
            self.diagnostics.emit(
                "INFO", "BROWSER_RUNTIME_READY", "Managed browser is ready"
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._state, self._error_code = (
                "failed",
                "BROWSER_PREPARATION_TIMEOUT"
                if isinstance(exc, TimeoutError)
                else "BROWSER_PREPARATION_FAILED",
            )
            self._message = (
                "浏览器准备超时，请检查网络后重试。"
                if isinstance(exc, TimeoutError)
                else "浏览器准备失败，请重试。"
            )
            self._detail = self._safe_detail(str(exc))
            self.diagnostics.emit(
                "WARNING",
                self._error_code,
                "Browser preparation failed",
                {"detail": self._detail},
            )
        finally:
            if process is not None:
                await self._terminate_process(process)
            self._process = None

    async def close(self) -> None:
        self._closed = True
        if self._task is not None and not self._task.done():
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
        self._state, self._message = "closed", "浏览器准备已停止。"
