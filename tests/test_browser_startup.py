"""从空会话开始验证浏览器冷启动，不预先给 BrowserSession 填入页面。"""

import json
import os
from types import SimpleNamespace
from unittest.mock import AsyncMock
from urllib.parse import urlsplit

import playwright.async_api as playwright_api
import pytest
from astrbot_plugin_douyin.core.models import DashboardCaller, PluginError
from astrbot_plugin_douyin.core.service import DouyinService
from astrbot_plugin_douyin.core.settings import Settings
from astrbot_plugin_douyin.douyin.session import BrowserSession
from astrbot_plugin_douyin.series_diagnostics import DiagnosticBuffer
from playwright.async_api import TimeoutError as PlaywrightTimeoutError
from playwright.async_api import async_playwright

HTML = """<!doctype html><meta charset="utf-8"><h1>登录测试页</h1><button>登录</button><input placeholder="验证码">"""


@pytest.fixture
async def cold_browser(tmp_path, monkeypatch):
    async with async_playwright() as actual:
        state = {"account_response": "json", "initial_error": None, "launches": []}

        async def route_request(route):
            if urlsplit(route.request.url).path == "/aweme/v1/web/query/user/":
                if state["account_response"] == "abort":
                    await route.abort()
                elif state["account_response"] == "html":
                    await route.fulfill(
                        content_type="text/html", body="<html>Login needed</html>"
                    )
                else:
                    await route.fulfill(
                        json={"status_code": 0, "user": {"uid": "12345"}}
                    )
            else:
                await route.fulfill(content_type="text/html", body=HTML)

        async def launch(path, **options):
            state["launches"].append(options)
            context = await actual.chromium.launch_persistent_context(path, **options)
            await context.route("**/*", route_request)
            if state["initial_error"]:
                page = context.pages[0]
                state["real_goto"] = page.goto
                monkeypatch.setattr(
                    page, "goto", AsyncMock(side_effect=state["initial_error"])
                )
            return context

        runtime = SimpleNamespace(
            chromium=SimpleNamespace(launch_persistent_context=launch), stop=AsyncMock()
        )
        monkeypatch.setattr(
            playwright_api,
            "async_playwright",
            lambda: SimpleNamespace(start=AsyncMock(return_value=runtime)),
        )
        browser = BrowserSession(
            tmp_path / "data",
            Settings(browser_channel=os.environ.get("DOUYIN_TEST_BROWSER_CHANNEL", "")),
            DiagnosticBuffer(),
        )
        assert browser._page is None and browser._context is None
        yield browser, state, runtime
        await browser.close()


async def test_disabled_plugin_page_can_start_real_persistent_browser_and_show_frame(
    cold_browser, tmp_path
):
    browser, state, _ = cold_browser
    service = DouyinService(
        tmp_path / "data",
        browser.settings,
        browser,
        SimpleNamespace(close=AsyncMock()),
        browser.diagnostics,
    )
    caller = DashboardCaller.from_username("admin")
    assert not service.settings.enabled
    await service.page_execute(caller, "control", {"action": "acquire"})
    login = await service.page_execute(caller, "control", {"action": "login"})
    frame = await service.page_execute(caller, "frame")
    assert login["status"] == frame["status"] == "ok"
    assert frame["data"]["image"].startswith("data:image/jpeg;base64,")
    assert await browser._page.locator("h1").inner_text() == "登录测试页"
    assert state["launches"][0]["headless"] is True
    assert len(state["launches"]) == 1
    assert browser._context.pages == [browser._page]


@pytest.mark.parametrize("account_response", ["html", "abort"])
async def test_anonymous_account_endpoint_failure_does_not_remove_browser_frame(
    cold_browser, account_response
):
    browser, state, _ = cold_browser
    state["account_response"] = account_response
    await browser.remote_navigate("login")
    status = await browser.status()
    assert status["browser_started"] and status["authenticated"] is False
    assert status["status_available"] is False
    assert status["code"] == "ACCOUNT_STATUS_UNAVAILABLE"
    assert (await browser.remote_frame())["image"].startswith("data:image/jpeg;base64,")
    assert len(state["launches"]) == 1


@pytest.mark.parametrize(
    "error",
    [
        PlaywrightTimeoutError("Page.goto: Timeout exceeded"),
        RuntimeError("net::ERR_NAME_NOT_RESOLVED at https://www.douyin.com/"),
    ],
)
async def test_initial_navigation_failure_keeps_browser_and_allows_recovery(
    cold_browser, monkeypatch, error
):
    browser, state, runtime = cold_browser
    state["initial_error"] = error
    with pytest.raises(PluginError) as failure:
        await browser.remote_navigate("login")
    assert failure.value.code == "BROWSER_NAVIGATION_FAILED"
    assert browser._page is not None and not browser._page.is_closed()
    runtime.stop.assert_not_awaited()
    frame = await browser.remote_frame()
    assert frame["image"].startswith("data:image/jpeg;base64,")
    assert frame["url"] == "about:blank"
    with pytest.raises(PluginError) as input_failure:
        await browser.remote_input("key", frame["frame_id"], key="Enter")
    assert input_failure.value.code == "REMOTE_SITE_REJECTED"
    monkeypatch.setattr(browser._page, "goto", state["real_goto"])
    await browser.remote_navigate("home")
    assert (await browser.remote_frame())["url"] == "https://www.douyin.com/"
    assert browser._navigation_error is None
    assert len(state["launches"]) == 1


@pytest.mark.parametrize(
    "message,code",
    [
        ("Executable doesn't exist at /cache/chromium/chrome", "BROWSER_NOT_INSTALLED"),
        (
            "Chromium distribution 'chrome' is not found at /opt/google/chrome/chrome",
            "BROWSER_NOT_INSTALLED",
        ),
        (
            "Host system is missing dependencies to run browsers",
            "BROWSER_DEPENDENCIES_MISSING",
        ),
        (
            "error while loading shared libraries: libnss3.so",
            "BROWSER_DEPENDENCIES_MISSING",
        ),
        ("Target page, context or browser has been closed", "BROWSER_LAUNCH_FAILED"),
    ],
)
async def test_launch_failure_has_actionable_error_and_cleans_profile_lock(
    tmp_path, monkeypatch, message, code
):
    runtime = SimpleNamespace(
        chromium=SimpleNamespace(
            launch_persistent_context=AsyncMock(side_effect=RuntimeError(message))
        ),
        stop=AsyncMock(),
    )
    monkeypatch.setattr(
        playwright_api,
        "async_playwright",
        lambda: SimpleNamespace(start=AsyncMock(return_value=runtime)),
    )
    browser = BrowserSession(tmp_path, Settings(), DiagnosticBuffer())
    with pytest.raises(PluginError) as failure:
        await browser.remote_navigate("login")
    assert failure.value.code == code
    assert failure.value.details["stage"] == "launch"
    assert browser._page is browser._context is browser._profile_lock is None
    assert browser.diagnostics.events()["events"][-1]["code"] == code
    runtime.stop.assert_awaited_once()


async def test_unexpected_status_query_error_is_isolated_from_browser_control(
    cold_browser, monkeypatch
):
    browser, _, _ = cold_browser
    await browser.remote_navigate("login")
    monkeypatch.setattr(
        browser,
        "_account_identity",
        AsyncMock(side_effect=RuntimeError("secret-account-token")),
    )
    status = await browser.status()
    assert status["code"] == "ACCOUNT_STATUS_UNAVAILABLE"
    assert "secret-account-token" not in str(status) + str(browser.diagnostics.events())
    assert (await browser.remote_frame())["image"].startswith("data:image/jpeg;base64,")


async def test_launch_diagnostics_and_page_error_redact_credentials(
    tmp_path, monkeypatch
):
    runtime = SimpleNamespace(
        chromium=SimpleNamespace(
            launch_persistent_context=AsyncMock(
                side_effect=RuntimeError(
                    "Executable doesn't exist token=private-launch-value"
                )
            )
        ),
        stop=AsyncMock(),
    )
    monkeypatch.setattr(
        playwright_api,
        "async_playwright",
        lambda: SimpleNamespace(start=AsyncMock(return_value=runtime)),
    )
    browser = BrowserSession(tmp_path, Settings(), DiagnosticBuffer())
    service = DouyinService(
        tmp_path,
        browser.settings,
        browser,
        SimpleNamespace(close=AsyncMock()),
        browser.diagnostics,
    )
    caller = DashboardCaller.from_username("admin")
    await service.page_execute(caller, "control", {"action": "acquire"})
    result = await service.page_execute(caller, "control", {"action": "login"})
    assert result["code"] == "BROWSER_NOT_INSTALLED"
    assert result["data"]["details"]["stage"] == "launch"
    assert "private-launch-value" not in json.dumps(result) + json.dumps(
        browser.diagnostics.events()
    )
