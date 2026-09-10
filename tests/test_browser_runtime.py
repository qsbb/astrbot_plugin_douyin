import asyncio
import json
import os
import sys
from importlib.metadata import version
from unittest.mock import AsyncMock, Mock

import pytest
from astrbot_plugin_douyin.core.models import PluginError
from astrbot_plugin_douyin.douyin import runtime_worker as worker
from astrbot_plugin_douyin.douyin.runtime import ManagedBrowserRuntime
from astrbot_plugin_douyin.series_diagnostics import DiagnosticBuffer


class Process:
    def __init__(self):
        self.stdout = asyncio.StreamReader()
        self.returncode = None
        self.pid = 123456789
        self.done = asyncio.Event()

    def finish(self, events, code=0):
        for event in events:
            self.stdout.feed_data(
                (worker.EVENT_PREFIX + json.dumps(event) + "\n").encode()
            )
        self.stdout.feed_eof()
        self.returncode = code
        self.done.set()

    async def wait(self):
        await self.done.wait()
        return self.returncode


def executable(runtime):
    path = runtime.root / "browsers" / "chromium-test" / "chrome"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"test fixture")
    return path


def ready_event(path):
    return {
        "state": "ready",
        "executable_path": str(path),
        "playwright_version": version("playwright"),
    }


async def test_manager_starts_once_and_uses_scoped_environment(tmp_path, monkeypatch):
    runtime = ManagedBrowserRuntime(tmp_path, "", DiagnosticBuffer())
    path = executable(runtime)
    process = Process()
    spawn = AsyncMock(return_value=process)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    monkeypatch.setenv("PLAYWRIGHT_BROWSERS_PATH", "parent-cache")
    runtime.start()
    task = runtime._task
    runtime.start(retry=True)
    assert task is runtime._task
    with pytest.raises(PluginError) as preparing:
        await runtime.launch_options()
    assert preparing.value.code == "BROWSER_PREPARING"
    process.finish([{"state": "verifying", "message": "checking"}, ready_event(path)])
    await task
    assert runtime.snapshot()["state"] == "ready"
    assert await runtime.launch_options() == {"executable_path": str(path)}
    spawn.assert_awaited_once()
    args, options = spawn.call_args
    assert args[0] == sys.executable
    assert args[1].endswith("runtime_worker.py")
    assert options["env"]["PLAYWRIGHT_BROWSERS_PATH"] == str(runtime.root / "browsers")
    assert options["env"]["TEMP"] == options["env"]["TMP"] == str(runtime.root / "tmp")
    assert os.environ["PLAYWRIGHT_BROWSERS_PATH"] == "parent-cache"
    await runtime.close()


async def test_failed_setup_requires_explicit_retry_and_redacts_details(
    tmp_path, monkeypatch
):
    runtime = ManagedBrowserRuntime(tmp_path, "", DiagnosticBuffer())
    failed, success = Process(), Process()
    path = executable(runtime)
    failed.finish(
        [
            {
                "state": "failed",
                "message": "Download failed",
                "error_code": "BROWSER_DOWNLOAD_FAILED",
                "detail": "proxy=https://user:private-password@proxy.invalid token=private-token",
            }
        ],
        1,
    )
    success.finish([ready_event(path)])
    spawn = AsyncMock(side_effect=[failed, success])
    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    runtime.start()
    await runtime._task
    with pytest.raises(PluginError) as failure:
        await runtime.launch_options()
    assert failure.value.code == "BROWSER_DOWNLOAD_FAILED"
    runtime.start()
    assert spawn.await_count == 1
    public = json.dumps(runtime.snapshot()) + json.dumps(runtime.diagnostics.events())
    assert "private-password" not in public and "private-token" not in public
    runtime.start(retry=True)
    await runtime._task
    assert runtime.snapshot()["state"] == "ready"
    assert spawn.await_count == 2
    assert "--force" in spawn.call_args.args
    await runtime.close()


async def test_ready_event_without_successful_process_is_not_ready(
    tmp_path, monkeypatch
):
    runtime = ManagedBrowserRuntime(tmp_path, "", DiagnosticBuffer())
    process = Process()
    process.finish([ready_event(executable(runtime))], 1)
    monkeypatch.setattr(
        asyncio, "create_subprocess_exec", AsyncMock(return_value=process)
    )
    runtime.start()
    await runtime._task
    assert runtime.snapshot()["state"] == "failed"
    await runtime.close()


@pytest.mark.parametrize("cause", ["version_changed", "file_missing"])
async def test_missing_browser_or_changed_playwright_version_rechecks(
    tmp_path, monkeypatch, cause
):
    runtime = ManagedBrowserRuntime(tmp_path, "", DiagnosticBuffer())
    path = executable(runtime)
    first, second = Process(), Process()
    first.finish([ready_event(path)])
    spawn = AsyncMock(side_effect=[first, second])
    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    runtime.start()
    await runtime._task
    if cause == "version_changed":
        runtime._version = "outdated-version"
    else:
        await asyncio.to_thread(path.unlink)
    with pytest.raises(PluginError, match="检查"):
        await runtime.launch_options()
    await asyncio.to_thread(path.write_bytes, b"restored fixture")
    second.finish([ready_event(path)])
    await runtime._task
    assert runtime.snapshot()["state"] == "ready" and spawn.await_count == 2
    await runtime.close()


async def test_external_channel_does_not_install_or_modify_browser(
    tmp_path, monkeypatch
):
    spawn = AsyncMock()
    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    runtime = ManagedBrowserRuntime(tmp_path, "chrome", DiagnosticBuffer())
    runtime.start()
    assert await runtime.launch_options() == {"channel": "chrome"}
    assert runtime.snapshot()["managed"] is False
    spawn.assert_not_awaited()
    assert not runtime.root.exists()
    await runtime.close()


@pytest.mark.parametrize("cancel", [False, True])
async def test_timeout_and_close_terminate_setup_without_late_ready(
    tmp_path, monkeypatch, cancel
):
    runtime = ManagedBrowserRuntime(tmp_path, "", DiagnosticBuffer())
    process = Process()
    spawned = asyncio.Event()

    async def spawn(*args, **kwargs):
        spawned.set()
        return process

    async def stop(target):
        target.finish([], -9)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    runtime._terminate_process = AsyncMock(side_effect=stop)
    runtime.timeout_seconds = 0.01
    runtime.start()
    await spawned.wait()
    if cancel:
        await runtime.close()
        assert runtime.snapshot()["state"] == "closed"
    else:
        await runtime._task
        assert runtime.snapshot()["error_code"] == "BROWSER_PREPARATION_TIMEOUT"
        await runtime.close()
    runtime._terminate_process.assert_awaited_once_with(process)
    assert runtime._process is None


def test_worker_reuses_valid_cache_without_download(tmp_path, monkeypatch):
    path = executable(ManagedBrowserRuntime(tmp_path, "", DiagnosticBuffer()))
    monkeypatch.setattr(worker, "locate_browser", lambda: path)
    verify, run = Mock(), Mock()
    monkeypatch.setattr(worker, "verify_browser", verify)
    monkeypatch.setattr(worker.subprocess, "run", run)
    assert worker.prepare(tmp_path / "browser-runtime") == path
    verify.assert_called_once_with(path)
    run.assert_not_called()


def test_worker_downloads_matching_full_browser_using_same_python(
    tmp_path, monkeypatch
):
    root = tmp_path / "runtime"
    path = root / "browsers" / "matching-version" / "chrome"
    monkeypatch.setattr(worker, "locate_browser", lambda: path)
    monkeypatch.setattr(worker, "verify_browser", Mock())

    def install(args, **kwargs):
        path.parent.mkdir(parents=True)
        path.write_bytes(b"fixture")

    run = Mock(side_effect=install)
    monkeypatch.setattr(worker.subprocess, "run", run)
    assert worker.prepare(root) == path
    assert run.call_args.args[0] == [
        sys.executable,
        "-m",
        "playwright",
        "install",
        "chromium",
        "--no-shell",
    ]
    assert run.call_args.kwargs["stdin"] == worker.subprocess.DEVNULL


def test_worker_explicit_retry_can_replace_incomplete_cached_browser(
    tmp_path, monkeypatch
):
    runtime = ManagedBrowserRuntime(tmp_path, "", DiagnosticBuffer())
    path = executable(runtime)
    monkeypatch.setattr(worker, "locate_browser", lambda: path)
    monkeypatch.setattr(worker, "verify_browser", Mock())
    run = Mock()
    monkeypatch.setattr(worker.subprocess, "run", run)
    assert worker.prepare(runtime.root, force=True) == path
    assert "--force" in run.call_args.args[0]


def test_worker_installs_missing_system_libraries_when_supported_and_privileged(
    tmp_path, monkeypatch
):
    runtime = ManagedBrowserRuntime(tmp_path, "", DiagnosticBuffer())
    path = executable(runtime)
    monkeypatch.setattr(worker, "locate_browser", lambda: path)
    verify = Mock(
        side_effect=[
            RuntimeError("error while loading shared libraries: libnss3.so"),
            None,
        ]
    )
    monkeypatch.setattr(worker, "verify_browser", verify)
    monkeypatch.setattr(worker.sys, "platform", "linux")
    monkeypatch.setattr(worker.os, "geteuid", lambda: 0, raising=False)
    monkeypatch.setattr(worker.shutil, "which", lambda _: "/usr/bin/apt-get")
    run = Mock()
    monkeypatch.setattr(worker.subprocess, "run", run)
    assert worker.prepare(runtime.root) == path
    assert verify.call_count == 2
    assert run.call_args.args[0] == [
        sys.executable,
        "-m",
        "playwright",
        "install-deps",
        "chromium",
    ]
    assert run.call_args.kwargs["env"]["DEBIAN_FRONTEND"] == "noninteractive"


def test_worker_does_not_attempt_interactive_privilege_escalation(
    tmp_path, monkeypatch
):
    runtime = ManagedBrowserRuntime(tmp_path, "", DiagnosticBuffer())
    path = executable(runtime)
    monkeypatch.setattr(worker, "locate_browser", lambda: path)
    monkeypatch.setattr(
        worker,
        "verify_browser",
        Mock(side_effect=RuntimeError("Host system is missing dependencies")),
    )
    monkeypatch.setattr(worker.sys, "platform", "linux")
    monkeypatch.setattr(worker.os, "geteuid", lambda: 1000, raising=False)
    run = Mock()
    monkeypatch.setattr(worker.subprocess, "run", run)
    with pytest.raises(worker.PreparationError) as failure:
        worker.prepare(runtime.root)
    assert failure.value.code == "BROWSER_SYSTEM_DEPENDENCIES_REQUIRED"
    run.assert_not_called()


def test_installation_lock_serializes_and_releases(tmp_path, monkeypatch):
    with worker.installation_lock(tmp_path):
        with monkeypatch.context() as patch:
            patch.setattr(worker.time, "monotonic", Mock(side_effect=[0, 900]))
            with pytest.raises(worker.PreparationError) as failure:
                with worker.installation_lock(tmp_path):
                    raise AssertionError("Concurrent installation acquired the lock")
            assert failure.value.code == "BROWSER_PREPARATION_BUSY"
    with worker.installation_lock(tmp_path):
        pass
