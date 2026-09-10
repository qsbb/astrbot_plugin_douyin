"""真实前端 + Page API + 服务 + Chrome 浏览器的离线集成；不登录外部账号。"""

import asyncio
import json
import mimetypes
import os
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
from urllib.parse import urlsplit

import pytest
from astrbot_plugin_douyin import page_api
from astrbot_plugin_douyin.core.models import Caller
from astrbot_plugin_douyin.core.service import DouyinService
from astrbot_plugin_douyin.core.settings import Settings
from astrbot_plugin_douyin.douyin.session import BrowserSession
from astrbot_plugin_douyin.series_diagnostics import DiagnosticBuffer
from playwright.async_api import async_playwright, expect

ROOT = Path(__file__).resolve().parents[1]
REMOTE_HTML = """<!doctype html><html lang="zh-CN"><meta charset="utf-8">
<style>body{background:#171923;color:#fff;font:20px sans-serif;padding:50px}button,input{font:inherit;padding:14px;margin:12px;display:block}h1{color:#c8bfff}</style>
<h1>抖音 · 本地交互验证</h1><p>演示页面，用于验证 Page 与 Bot 共用浏览器。</p>
<button id="login" onclick="document.querySelector('#dialog').hidden=false">登录</button>
<div id="dialog" hidden><p>输入测试验证码，然后按 Enter 完成本地登录</p><input id="code" autocomplete="off" onkeydown="if(event.key==='Enter' && this.value==='123456'){window.logged=true;document.querySelector('#account').textContent='已登录 · user:12345'}"></div>
<p id="account">未登录</p><button id="like" onclick="window.likes++;this.textContent='已点赞 '+window.likes">点赞</button>
<script>window.logged=false;window.likes=0;window.wheels=[];document.addEventListener('wheel',e=>window.wheels.push(e.deltaY));</script></html>"""


class Config(dict):
    async def save_config_async(self, changes):
        self.update(changes)
        return True


async def save_preview(page, name):
    directory = os.environ.get("DOUYIN_PAGE_SCREENSHOT_DIR")
    if directory:
        target = Path(directory)
        await asyncio.to_thread(target.mkdir, parents=True, exist_ok=True)
        await page.screenshot(path=str(target / name), full_page=True)


@pytest.fixture
async def ui(tmp_path, monkeypatch):
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(
            channel=os.environ.get("DOUYIN_TEST_BROWSER_CHANNEL"), headless=True
        )
        remote_context = await browser.new_context(
            viewport={"width": 900, "height": 730}
        )
        remote = await remote_context.new_page()

        async def remote_route(route):
            if "/aweme/v1/web/query/user/" in route.request.url:
                logged = await remote.evaluate("Boolean(window.logged)")
                await route.fulfill(
                    json={"status_code": 0, "user": {"uid": "12345" if logged else "0"}}
                )
            else:
                await route.fulfill(content_type="text/html", body=REMOTE_HTML)

        await remote_context.route("**/*", remote_route)
        await remote.goto("https://www.douyin.com/")
        settings = Settings()
        diagnostics = DiagnosticBuffer()
        session = BrowserSession(tmp_path / "data", settings, diagnostics)
        session._context, session._page = remote_context, remote
        service = DouyinService(
            tmp_path / "data",
            settings,
            session,
            SimpleNamespace(close=AsyncMock()),
            diagnostics,
        )
        config = Config()
        api = page_api.PageApi(
            SimpleNamespace(register_web_api=Mock()), config, service
        )
        api.register()
        api_lock = asyncio.Lock()
        calls = []

        async def backend(source, endpoint, data=None):
            async with api_lock:
                calls.append({"endpoint": endpoint, "data": data})
                monkeypatch.setattr(
                    page_api,
                    "request",
                    SimpleNamespace(
                        username="admin",
                        body=AsyncMock(return_value=json.dumps(data or {}).encode()),
                        json=AsyncMock(return_value=data or {}),
                    ),
                )
                response = await api.handle(endpoint.removeprefix("page/"))
                if response["status_code"] != 200:
                    raise RuntimeError(response["body"]["message"])
                return response["body"]

        locale = json.loads(
            (ROOT / ".astrbot-plugin/i18n/zh-CN.json").read_text(encoding="utf-8")
        )
        ui_context = await browser.new_context(viewport={"width": 1450, "height": 1150})
        await ui_context.expose_binding("backend", backend)
        await ui_context.add_init_script(
            """(() => {
          let ctx={locale:'zh-CN',isDark:false,i18n:LOCALE}; const listeners=[];
          window.setHostContext = value => {ctx={...ctx,...value};document.documentElement.dataset.theme=ctx.isDark?'dark':'light';listeners.forEach(fn=>fn(ctx));};
          window.AstrBotPluginPage={ready:async()=>ctx,getContext:()=>ctx,getLocale:()=>ctx.locale,getI18n:()=>ctx.i18n,
            t:(key,fallback)=>key.split('.').reduce((v,k)=>v?.[k],ctx.i18n)??fallback,
            onContext:fn=>{listeners.push(fn);return()=>{};},
            apiGet:endpoint=>window.backend(endpoint),apiPost:(endpoint,data)=>window.backend(endpoint,data)};
        })();""".replace("LOCALE", json.dumps(locale, ensure_ascii=False))
        )

        async def serve(route):
            path = urlsplit(route.request.url).path.removeprefix("/") or "index.html"
            if path not in {"index.html", "app.js", "style.css"}:
                await route.abort()
                return
            await route.fulfill(
                body=(ROOT / "pages/manager" / path).read_bytes(),
                content_type=mimetypes.guess_type(path)[0] or "text/plain",
            )

        await ui_context.route("**/*", serve)
        page = await ui_context.new_page()
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        await page.goto("https://plugin-page.test/")
        await expect(page.locator("#open-login")).to_be_enabled()
        yield SimpleNamespace(
            page=page,
            remote=remote,
            service=service,
            api=api,
            config=config,
            calls=calls,
            errors=errors,
        )
        await ui_context.close()
        api.close()
        await service.close()
        await browser.close()


async def remote_click(ui, selector):
    remote_bounds = await ui.remote.locator(selector).bounding_box()
    image = ui.page.locator("#browser-frame")
    await expect(image).to_be_visible()
    bounds = await image.bounding_box()
    await image.click(
        position={
            "x": (remote_bounds["x"] + remote_bounds["width"] / 2)
            * bounds["width"]
            / 900,
            "y": (remote_bounds["y"] + remote_bounds["height"] / 2)
            * bounds["height"]
            / 730,
        }
    )


async def test_page_login_bind_and_manual_operation_use_bot_browser(ui):
    page = ui.page
    await page.locator("#auto-refresh").uncheck()
    await page.locator("#open-login").click()
    await expect(page.locator("#browser-frame")).to_be_visible()
    await expect(ui.remote.locator("#dialog")).to_be_visible()
    await remote_click(ui, "#code")
    await page.locator("#remote-text").fill("123456")
    await page.locator("#send-text").click()
    await expect(ui.remote.locator("#code")).to_have_value("123456")
    await page.locator("#send-key").click()
    await expect(ui.remote.locator("#account")).to_have_text("已登录 · user:12345")
    await page.locator("#refresh-status").click()
    await expect(page.locator("#account-current")).to_contain_text("user:12345")
    await page.locator("#bind-account").click()
    await expect(page.locator("#account-bound")).to_contain_text("user:12345")
    assert (
        ui.config["expected_account_ref"]
        == ui.service.settings.expected_account_ref
        == "user:12345"
    )
    await remote_click(ui, "#like")
    await expect(ui.remote.locator("#like")).to_have_text("已点赞 1")
    await expect(page.locator("#refresh-frame")).to_be_enabled()
    await page.locator("#browser-frame").hover()
    await page.mouse.wheel(0, 120)
    await ui.remote.wait_for_function("window.wheels.length > 0")
    assert ui.service.browser._page is ui.remote
    assert ui.service._control_active()
    await page.locator("#refresh-status").scroll_into_view_if_needed()
    await save_preview(page, "astrbot-douyin-page-演示.png")
    await page.locator("#release").click()
    await expect(page.locator("#release")).to_be_disabled()
    result = await ui.service.execute(
        "status", Caller("test:FriendMessage:owner", "owner", True)
    )
    assert result["data"]["account_ref"] == "user:12345"
    assert not ui.service._control_active()
    assert not ui.errors


async def test_quick_write_can_retry_a_preflight_rejection_but_not_resend_success(ui):
    page = ui.page
    ui.service.browser.set_like = AsyncMock(
        return_value={"status": "verified", "code": "LIKE_VERIFIED"}
    )
    await ui.remote.evaluate("window.logged=true")
    await page.locator("#refresh-status").click()
    await page.locator("#bind-account").click()
    await page.locator("#enabled").check()
    await page.locator("#save-settings").click()
    await expect(page.locator("#settings-dirty")).to_be_hidden()
    await page.locator("#operation").select_option("set_like")
    await page.locator("#param-video_ref").fill("123456")
    await page.locator("#run-operation").click()
    await expect(page.locator("#result-output")).to_contain_text("ACTION_NOT_GRANTED")
    await page.locator('input[name="action"][value="set_like"]').check()
    await page.locator("#save-settings").click()
    await expect(page.locator("#settings-dirty")).to_be_hidden()
    await page.locator("#run-operation").click()
    await expect(page.locator("#result-output")).to_contain_text("LIKE_VERIFIED")
    await page.locator("#run-operation").click()
    await expect(page.locator("#run-operation")).to_be_enabled()
    writes = [
        call["data"]
        for call in ui.calls
        if call["endpoint"] == "page/action"
        and call["data"].get("operation") == "set_like"
    ]
    assert len(writes) == 2 and writes[0]["request_id"] != writes[1]["request_id"]
    assert any(
        call["endpoint"] == "page/action" and call["data"].get("operation") == "receipt"
        for call in ui.calls
    )
    ui.service.browser.set_like.assert_awaited_once()
    english = json.loads(
        (ROOT / ".astrbot-plugin/i18n/en-US.json").read_text(encoding="utf-8")
    )
    await page.evaluate("i18n=>window.setHostContext({locale:'en-US',i18n})", english)
    await expect(page.locator("#result-output")).to_contain_text("LIKE_VERIFIED")
    assert not ui.errors


async def test_page_settings_i18n_theme_and_mobile_layout(ui):
    page = ui.page
    await page.locator("#enabled").check()
    await page.locator('input[name="action"][value="set_like"]').check()
    await page.locator("#allowed-targets").fill("user:12345\nuser:45678")
    await page.locator("#save-settings").click()
    await expect(page.locator("#settings-dirty")).to_be_hidden()
    assert ui.service.settings.enabled and ui.service.settings.allowed_actions == (
        "set_like",
    )
    assert ui.service.settings.allowed_target_refs == ("user:12345", "user:45678")
    english = json.loads(
        (ROOT / ".astrbot-plugin/i18n/en-US.json").read_text(encoding="utf-8")
    )
    await page.evaluate(
        "i18n=>window.setHostContext({locale:'en-US',isDark:true,i18n})", english
    )
    await expect(page.locator("html")).to_have_attribute("lang", "en-US")
    await expect(page.locator("html")).to_have_attribute("data-theme", "dark")
    await expect(page.locator("h1")).to_have_text(
        english["pages"]["manager"]["heading"]
    )
    await expect(page.locator("#notice")).to_have_text(
        english["pages"]["manager"]["settingsSaved"]
    )
    await expect(page.locator("#receipts")).to_have_text(
        english["pages"]["manager"]["noReceipts"]
    )
    await page.locator("#refresh-status").scroll_into_view_if_needed()
    await save_preview(page, "astrbot-douyin-page-dark.png")
    await page.set_viewport_size({"width": 390, "height": 844})
    assert await page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    await save_preview(page, "astrbot-douyin-page-mobile.png")
    assert not ui.errors


def test_page_files_and_i18n_metadata_exist():
    locale_keys = []
    for lang in ("zh-CN", "en-US"):
        data = json.loads(
            (ROOT / f".astrbot-plugin/i18n/{lang}.json").read_text(encoding="utf-8")
        )
        assert data["pages"]["manager"]["title"]
        assert data["pages"]["manager"]["description"]
        locale_keys.append(set(data["pages"]["manager"]))
    assert locale_keys[0] == locale_keys[1]
    source = (ROOT / "pages/manager/index.html").read_text(encoding="utf-8")
    for control in (
        "browser-frame",
        "open-login",
        "acquire",
        "release",
        "remote-text",
        "bind-account",
        "settings-form",
        "quick-form",
        "receipts",
    ):
        assert f'id="{control}"' in source
