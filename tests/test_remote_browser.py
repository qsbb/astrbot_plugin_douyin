"""真实 Chrome 的 Page 远程控制测试，所有网络请求在本地拦截。"""

import asyncio
import base64
import os
from unittest.mock import AsyncMock
from urllib.parse import urlsplit

import pytest
from astrbot_plugin_douyin.core.models import PluginError
from astrbot_plugin_douyin.core.settings import Settings
from astrbot_plugin_douyin.douyin.session import BrowserSession
from astrbot_plugin_douyin.series_diagnostics import DiagnosticBuffer
from playwright.async_api import Error as PlaywrightError
from playwright.async_api import async_playwright

HTML = """<!doctype html><html><head><meta charset="utf-8"><style>
body { margin:20px; font:16px sans-serif; } button,textarea,input {display:block;margin:8px;}
#slider {width:240px;height:36px;background:#abc;touch-action:none;}
</style></head><body>
<button id="login" onclick="document.querySelector('#dialog').hidden=false">登录</button>
<div id="dialog" hidden><label>短信登录<input id="phone" autocomplete="off"></label></div>
<textarea id="editor"></textarea><input type="password" value="private-password">
<button data-e2e="im-entry" onclick="document.body.dataset.inbox='opened'">私信</button>
<button id="counter" onclick="window.clicked++">点击</button>
<div id="slider"></div><a id="outside" href="https://outside.example/">外部站点</a>
<button id="popup" onclick="window.open('https://sso.douyin.com/login')">登录弹窗</button>
<script>window.clicked=0;window.keys=[];window.moves=[];window.wheel=[];
document.addEventListener('keydown',e=>window.keys.push(e.key));
document.addEventListener('pointermove',e=>{if(e.buttons)window.moves.push([e.clientX,e.clientY]);});
document.addEventListener('wheel',e=>window.wheel.push([e.deltaX,e.deltaY]));
</script></body></html>"""


@pytest.fixture
async def remote_browser(tmp_path):
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(
            channel=os.environ.get("DOUYIN_TEST_BROWSER_CHANNEL"), headless=True
        )
        context = await browser.new_context(viewport={"width": 800, "height": 700})
        requested = []

        async def route_request(route):
            requested.append(route.request.url)
            await route.fulfill(status=200, content_type="text/html", body=HTML)

        await context.route("**/*", route_request)
        page = await context.new_page()
        await page.goto("https://www.douyin.com/?login_secret=do-not-return")
        session = BrowserSession(tmp_path, Settings(), DiagnosticBuffer())
        session._page = page
        session._context = context
        yield session, requested
        await session.close()
        await browser.close()


async def click_node(session, selector):
    bounds = await session._page.locator(selector).bounding_box()
    frame = await session.remote_frame()
    return await session.remote_input(
        "click",
        frame["frame_id"],
        x=bounds["x"] + bounds["width"] / 2,
        y=bounds["y"] + bounds["height"] / 2,
    )


async def test_remote_frame_is_bounded_memory_jpeg_without_url_credentials(
    remote_browser, tmp_path
):
    session, _ = remote_browser
    frame = await session.remote_frame()
    assert frame["width"] == 800 and frame["height"] == 700
    assert frame["url"] == "https://www.douyin.com/"
    assert frame["expires_in_seconds"] == 30
    assert base64.b64decode(frame["image"].split(",", 1)[1]).startswith(b"\xff\xd8")
    assert list(tmp_path.iterdir()) == []
    assert "cookie" not in frame and "storage_state" not in frame


async def test_manual_click_consumes_frame_and_reports_only_dispatched(remote_browser):
    session, _ = remote_browser
    bounds = await session._page.locator("#counter").bounding_box()
    frame = await session.remote_frame()
    result = await session.remote_input(
        "click", frame["frame_id"], x=bounds["x"] + 3, y=bounds["y"] + 3
    )
    assert result["status"] == "submitted"
    assert await session._page.evaluate("window.clicked") == 1
    with pytest.raises(PluginError, match="过期"):
        await session.remote_input("click", frame["frame_id"], x=30, y=30)
    assert await session._page.evaluate("window.clicked") == 1


async def test_text_and_keyboard_support_sms_login_without_logging_input(
    remote_browser,
):
    session, _ = remote_browser
    await session.remote_navigate("login")
    assert await session._page.locator("#dialog").is_visible()
    await click_node(session, "#phone")
    frame = await session.remote_frame()
    await session.remote_input("text", frame["frame_id"], text="13800138000")
    assert await session._page.locator("#phone").input_value() == "13800138000"
    frame = await session.remote_frame()
    await session.remote_input("key", frame["frame_id"], key="Control+A")
    frame = await session.remote_frame()
    await session.remote_input("text", frame["frame_id"], text="123456")
    assert await session._page.locator("#phone").input_value() == "123456"
    assert "13800138000" not in str(session.diagnostics.events())
    assert "123456" not in str(session.diagnostics.events())


async def test_manual_scroll_and_drag_are_bounded_user_points(remote_browser):
    session, _ = remote_browser
    frame = await session.remote_frame()
    await session.remote_input("scroll", frame["frame_id"], delta_y=120)
    await session._page.wait_for_function("window.wheel.length === 1")
    assert await session._page.evaluate("window.wheel[0]") == [0, 120]
    frame = await session.remote_frame()
    await session.remote_input(
        "drag",
        frame["frame_id"],
        points=[{"x": 30, "y": 300}, {"x": 70, "y": 300}, {"x": 140, "y": 300}],
    )
    assert await session._page.evaluate("window.moves") == [[70, 300], [140, 300]]


@pytest.mark.parametrize(
    "kind,params",
    [
        ("click", {"x": -1, "y": 10}),
        ("click", {"x": 800, "y": 10}),
        ("click", {"x": True, "y": 10}),
        ("click", {"x": float("nan"), "y": 10}),
        ("text", {"text": "x" * 2001}),
        ("key", {"key": "Control+L"}),
        ("key", {"key": "F12"}),
        ("scroll", {"delta_y": 2001}),
        ("drag", {"points": [{"x": 1, "y": 1}]}),
        ("click", {"x": 1, "y": 1, "button": "right"}),
        ("evaluate", {"script": "document.cookie"}),
    ],
)
async def test_invalid_input_never_consumes_frame_or_dispatches(
    remote_browser, kind, params
):
    session, _ = remote_browser
    frame = await session.remote_frame()
    with pytest.raises(PluginError) as exc:
        await session.remote_input(kind, frame["frame_id"], **params)
    assert exc.value.code == "REMOTE_INPUT_INVALID"
    assert session._remote_frame["frame_id"] == frame["frame_id"]
    assert await session._page.evaluate("window.clicked") == 0


async def test_expired_frame_and_navigation_invalidate_coordinates(remote_browser):
    session, _ = remote_browser
    frame = await session.remote_frame()
    session._remote_frame["expires"] = asyncio.get_running_loop().time() - 1
    with pytest.raises(PluginError) as exc:
        await session.remote_input("key", frame["frame_id"], key="Enter")
    assert exc.value.code == "REMOTE_FRAME_STALE"
    frame = await session.remote_frame()
    await session._page.goto("https://www.douyin.com/video/12345")
    with pytest.raises(PluginError) as exc:
        await session.remote_input("key", frame["frame_id"], key="Enter")
    assert exc.value.code == "REMOTE_FRAME_STALE"


@pytest.mark.parametrize(
    "failure", [RuntimeError("secret-input-value"), asyncio.CancelledError()]
)
async def test_interrupted_dispatch_is_unknown_without_input_or_error_details(
    remote_browser, monkeypatch, failure
):
    session, _ = remote_browser
    frame = await session.remote_frame()
    monkeypatch.setattr(
        session._page.keyboard, "insert_text", AsyncMock(side_effect=failure)
    )
    result = await session.remote_input(
        "text", frame["frame_id"], text="sensitive-code"
    )
    assert result == {
        "status": "unknown_result",
        "code": "MANUAL_INPUT_UNKNOWN",
        "kind": "text",
    }
    assert session._remote_frame is None
    assert "sensitive-code" not in str(result) and "secret-input-value" not in str(
        result
    )


@pytest.mark.parametrize(
    "destination",
    [
        "https://www.douyin.com/",
        "javascript:alert(1)",
        "file:///C:/secret",
        "../profile",
    ],
)
async def test_only_fixed_navigation_destinations(remote_browser, destination):
    session, requested = remote_browser
    count = len(requested)
    with pytest.raises(PluginError) as exc:
        await session.remote_navigate(destination)
    assert exc.value.code == "REMOTE_DESTINATION_INVALID"
    assert len(requested) == count


async def test_fixed_inbox_reload_home_work_in_same_browser(remote_browser):
    session, _ = remote_browser
    context = session._context
    await session.remote_navigate("inbox")
    assert await session._page.evaluate("document.body.dataset.inbox") == "opened"
    await session.remote_navigate("reload")
    assert await session._page.evaluate("document.body.dataset.inbox") is None
    await session.remote_navigate("home")
    assert session._context is context


async def test_external_navigation_aborted_before_request_then_home_recovers(
    remote_browser,
):
    session, requested = remote_browser
    await session.remote_frame()
    with pytest.raises(PlaywrightError):
        await session._page.goto("https://outside.example/stolen")
    assert all(urlsplit(url).hostname != "outside.example" for url in requested)
    with pytest.raises(PluginError) as exc:
        await session.remote_frame()
    assert exc.value.code == "REMOTE_SITE_REJECTED"
    await session.remote_navigate("home")
    assert (await session.remote_frame())["url"] == "https://www.douyin.com/"


async def test_official_login_popup_becomes_controlled_page(remote_browser):
    session, _ = remote_browser
    original = session._page
    async with original.expect_popup():
        await click_node(session, "#popup")
    if session._remote_tasks:
        await asyncio.gather(*session._remote_tasks)
    assert session._page is not original
    assert (await session.remote_frame())["url"] == "https://sso.douyin.com/login"


async def test_repeated_login_popups_keep_one_worker_and_one_popup(remote_browser):
    session, _ = remote_browser
    await session.remote_frame()
    main = session._page
    await main.evaluate(
        "for(let i=0;i<6;i++)window.open('https://sso.douyin.com/login?attempt='+i)"
    )
    assert len(session._remote_tasks) <= 1
    while session._remote_tasks:
        await asyncio.gather(*session._remote_tasks)
    assert len(main.context.pages) == 2
    assert len(session._remote_pages) == 2
    popup = session._page
    assert popup is not main and not main.is_closed()
    await popup.close()
    assert session._page is main
    assert len(session._remote_pages) == 1
    assert (await session.remote_frame())["url"] == "https://www.douyin.com/"


async def test_bot_read_returns_to_main_after_manual_login(remote_browser):
    session, _ = remote_browser
    await session.remote_frame()
    main = session._page
    async with main.expect_popup():
        await click_node(session, "#popup")
    while session._remote_tasks:
        await asyncio.gather(*session._remote_tasks)
    popup = session._page
    session._account_identity = AsyncMock(return_value="user:12345")
    await session._read_ready()
    assert session._page is main and popup.is_closed()
    assert len(main.context.pages) == 1


async def test_closing_main_promotes_remaining_popup_without_closing_it(remote_browser):
    session, _ = remote_browser
    await session.remote_frame()
    main = session._page
    async with main.expect_popup():
        await click_node(session, "#popup")
    while session._remote_tasks:
        await asyncio.gather(*session._remote_tasks)
    popup = session._page
    await main.close()
    assert session._remote_main_page is popup
    assert session._remote_popup_page is None
    await session.remote_navigate("home")
    assert not popup.is_closed()
    assert (await session.remote_frame())["url"] == "https://www.douyin.com/"


@pytest.mark.parametrize(
    "url",
    [
        "http://www.douyin.com/",
        "https://www.douyin.com.evil.example/",
        "https://user:password@www.douyin.com/",
        "https://www.douyin.com:8443/",
        "https://unknown.douyin.com/",
        "file:///etc/passwd",
        "javascript:void(0)",
    ],
)
def test_remote_url_allowlist_rejects_ambiguous_and_nonofficial_destinations(url):
    assert BrowserSession._remote_url_allowed(url) is False
