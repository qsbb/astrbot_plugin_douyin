"""在干净容器中验证自动安装、实际画面和缓存复用；不访问抖音账号。"""

import argparse
import asyncio
import json
from pathlib import Path

from astrbot_plugin_douyin.core.settings import Settings
from astrbot_plugin_douyin.douyin.runtime import ManagedBrowserRuntime
from astrbot_plugin_douyin.douyin.session import BrowserSession
from astrbot_plugin_douyin.series_diagnostics import DiagnosticBuffer


async def prepare_once(root: Path):
    runtime = ManagedBrowserRuntime(root, "", DiagnosticBuffer())
    runtime.start()
    last = ""
    while runtime.snapshot()["state"] not in {"ready", "failed", "closed"}:
        state = runtime.snapshot()["state"]
        if state != last:
            print(json.dumps(runtime.snapshot(), ensure_ascii=True), flush=True)
            last = state
        await asyncio.sleep(0.5)
    if runtime.snapshot()["state"] != "ready":
        print(json.dumps(runtime.snapshot(), ensure_ascii=True), flush=True)
        await runtime.close()
        raise RuntimeError("Browser automatic preparation failed")
    return runtime


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--data-dir", type=Path, required=True)
    parser.add_argument("--expect-install", action="store_true")
    parser.add_argument("--expect-system-deps", action="store_true")
    args = parser.parse_args()
    runtime = await prepare_once(args.data_dir)
    phases = {
        event["details"].get("state")
        for event in runtime.diagnostics.events()["events"]
    }
    if args.expect_install:
        assert "installing_browser" in phases
    if args.expect_system_deps:
        assert "installing_dependencies" in phases
    browser = BrowserSession(
        args.data_dir, Settings(), runtime.diagnostics, runtime=runtime
    )
    original_goto = browser._goto
    routed = False

    async def route_request(route):
        await route.fulfill(
            content_type="text/html",
            body="<!doctype html><title>Installed browser</title><h1>ready</h1>",
        )

    async def offline_goto(url, *, check_ready=True):
        nonlocal routed
        if not routed:
            await browser._page.route("**/*", route_request)
            routed = True
        await original_goto(url, check_ready=check_ready)

    browser._goto = offline_goto
    try:
        await browser.remote_navigate("home")
        frame = await browser.remote_frame()
        assert frame["image"].startswith("data:image/jpeg;base64,")
        assert frame["width"] == 1280 and frame["height"] == 900
        assert await browser._page.title() == "Installed browser"
    finally:
        await browser.close()
        await runtime.close()
    reused = await prepare_once(args.data_dir)
    try:
        phases = {
            event["details"].get("state")
            for event in reused.diagnostics.events()["events"]
        }
        assert "installing_browser" not in phases
        assert "installing_dependencies" not in phases
        print(
            json.dumps(
                {"browser_ready": True, "frame_received": True, "cached_restart": True}
            ),
            flush=True,
        )
    finally:
        await reused.close()


if __name__ == "__main__":
    asyncio.run(main())
