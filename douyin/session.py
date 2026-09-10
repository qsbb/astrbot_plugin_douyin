"""隔离浏览器中的抖音页面操作，响应读取与提交回执在同一会话内核对。"""

import asyncio
import base64
import copy
import json
import math
import os
import re
import sys
from collections import deque
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import parse_qs, quote, unquote, urlsplit, urlunsplit
from uuid import uuid4

from ..core.models import PluginError
from . import locators as ui
from .parsers import comments, extract_contacts, extract_videos, video_id

READ_PATHS = ("/aweme/v1/web/", "/aweme/v2/web/")
ACTION_PATHS = ("/commit/item/digg/", "/comment/publish/")
REMOTE_HOSTS = {
    "douyin.com",
    "www.douyin.com",
    "sso.douyin.com",
    "login.douyin.com",
    "passport.douyin.com",
}
REMOTE_KEYS = {
    "Enter",
    "Tab",
    "Shift+Tab",
    "Backspace",
    "Delete",
    "Escape",
    "Space",
    "ArrowUp",
    "ArrowDown",
    "ArrowLeft",
    "ArrowRight",
    "Home",
    "End",
    "PageUp",
    "PageDown",
    "Control+A",
    "Meta+A",
}


class BrowserSession:
    def __init__(self, data_dir: Path, settings, diagnostics, *, runtime=None):
        self.root = data_dir
        self.settings = settings
        self.diagnostics = diagnostics
        self.runtime = runtime
        self._playwright = None
        self._context = None
        self._page = None
        self._profile_lock = None
        self._capture_tasks: set[asyncio.Task] = set()
        self._responses: deque[dict] = deque(maxlen=100)
        self._videos: dict[str, dict] = {}
        self._contacts: dict[str, dict] = {}
        self._seq = 0
        self._account = ""
        self._feed_seen: deque[str] = deque(maxlen=200)
        self._comment_pages: dict[str, dict] = {}
        self._remote_frame: dict | None = None
        self._remote_context = None
        self._remote_pages: set = set()
        self._remote_tasks: set[asyncio.Task] = set()
        self._remote_blocked = False
        self._remote_main_page = None
        self._remote_popup_page = None
        self._remote_closing = False
        self._navigation_error = None

    async def _ensure(self, *, headless: bool | None = None):
        """冷启动持久化浏览器，并尝试打开抖音首页。

        Args:
            headless: 可选启动模式覆盖值；Page 首次启动使用无头模式。

        Returns:
            无返回值。已有页面时直接复用，不重新创建浏览器。

        Raises:
            PluginError: 数据目录、浏览器安装或系统依赖不满足要求，或者
                首次导航失败。导航失败时保留已启动的页面供 Page 查看与恢复。
            asyncio.CancelledError: 请求被取消；启动阶段会清理部分创建的资源。
        """
        if self._page is not None and not self._page.is_closed():
            return
        if self._context is not None:
            await self.close()
        self._remote_closing = False
        runtime_options = (
            await self.runtime.launch_options() if self.runtime is not None else {}
        )
        try:
            self.root.mkdir(parents=True, exist_ok=True)
            self._profile_lock = (self.root / "browser-profile.lock").open("a+b")
        except OSError as exc:
            raise PluginError(
                "BROWSER_PROFILE_UNWRITABLE",
                "无法写入浏览器数据目录，请检查 AstrBot 数据目录的权限。",
                {"stage": "profile"},
            ) from exc
        try:
            # 操作系统锁在进程退出时释放，不靠删除锁文件猜测其他实例是否在运行。
            self._profile_lock.seek(0)
            if os.name == "nt":
                import msvcrt

                if (self.root / "browser-profile.lock").stat().st_size == 0:
                    self._profile_lock.write(b"0")
                    self._profile_lock.flush()
                self._profile_lock.seek(0)
                msvcrt.locking(self._profile_lock.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(self._profile_lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            self._profile_lock.close()
            self._profile_lock = None
            raise PluginError(
                "PROFILE_IN_USE", "此抖音浏览器配置已由另一实例占用。"
            ) from exc
        try:
            from playwright.async_api import async_playwright

            self._playwright = await async_playwright().start()
            options = {
                "headless": self.settings.headless if headless is None else headless,
                "viewport": {"width": 1280, "height": 900},
                "locale": "zh-CN",
            }
            if sys.platform.startswith("linux") and not (
                os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")
            ):
                options["headless"] = True
            if self.runtime is None and self.settings.browser_channel:
                options["channel"] = self.settings.browser_channel
            options.update(runtime_options)
            self._context = await self._playwright.chromium.launch_persistent_context(
                str(self.root / "browser-profile"), **options
            )
            self._context.set_default_timeout(8000)
            self._context.set_default_navigation_timeout(25000)
            self._page = (
                self._context.pages[0]
                if self._context.pages
                else await self._context.new_page()
            )
            for extra in self._context.pages[1:]:
                await extra.close()
            self._page.on("response", self._schedule_capture)
        except BaseException as exc:
            await self.close()
            if not isinstance(exc, Exception):
                raise
            reason = str(exc).lower()
            if isinstance(exc, ModuleNotFoundError) and str(exc.name).startswith(
                "playwright"
            ):
                code = "PLAYWRIGHT_NOT_INSTALLED"
                message = (
                    "当前 AstrBot Python 环境缺少 Playwright，请重新安装插件依赖。"
                )
            elif any(
                text in reason
                for text in (
                    "executable doesn't exist",
                    "executable does not exist",
                    "distribution 'chrome' is not found",
                    "distribution 'msedge' is not found",
                )
            ):
                code = "BROWSER_NOT_INSTALLED"
                message = "当前 AstrBot 运行环境没有可用的浏览器。请在同一 Python 环境或容器内运行 python -m playwright install chromium，并将 browser_channel 留空；也可安装已配置的 Chrome/Edge。"
            elif any(
                text in reason
                for text in (
                    "host system is missing dependencies",
                    "error while loading shared libraries",
                    "cannot open shared object file",
                )
            ):
                code = "BROWSER_DEPENDENCIES_MISSING"
                message = "浏览器缺少系统运行库。请在 AstrBot 所在 Linux 环境或容器内运行 python -m playwright install --with-deps chromium。"
            else:
                code = "BROWSER_LAUNCH_FAILED"
                message = "浏览器启动失败，请检查浏览器配置、数据目录权限及宿主日志。"
            self.diagnostics.emit(
                "WARNING",
                code,
                "Browser startup failed",
                {
                    "stage": "launch",
                    "browser_channel": self.settings.browser_channel or "chromium",
                    "error_type": type(exc).__name__,
                    "error": str(exc)[:4096],
                },
            )
            raise PluginError(
                code,
                message,
                {
                    "stage": "launch",
                    "browser_channel": self.settings.browser_channel or "chromium",
                    "reason": str(exc).splitlines()[0][:500]
                    if str(exc)
                    else type(exc).__name__,
                },
            ) from exc
        self.diagnostics.emit(
            "INFO", "BROWSER_STARTED", "Isolated Douyin browser started"
        )
        # 导航与进程启动分开：站点失败时仍保留浏览器，Page 可以展示或恢复画面。
        await self._goto("https://www.douyin.com/", check_ready=False)

    def _schedule_capture(self, response):
        parsed = urlsplit(response.url)
        if (
            parsed.hostname != "www.douyin.com"
            or not parsed.path.startswith(READ_PATHS)
            or len(self._capture_tasks) >= 8
        ):
            return
        task = asyncio.create_task(self._capture(response))
        self._capture_tasks.add(task)
        task.add_done_callback(self._capture_tasks.discard)

    async def _capture(self, response):
        try:
            async with asyncio.timeout(10):
                headers = await response.all_headers()
                if int(headers.get("content-length") or 0) > 4 * 1024 * 1024:
                    return
                body = await response.body()
                if len(body) > 4 * 1024 * 1024:
                    return
                data = json.loads(body)
                if not isinstance(data, dict):
                    return
                self._seq += 1
                query = parse_qs(urlsplit(response.url).query)
                post = parse_qs(response.request.post_data or "")
                self._responses.append(
                    {
                        "seq": self._seq,
                        "path": urlsplit(response.url).path,
                        "query": query,
                        "post": post,
                        "data": data,
                        "http_status": response.status,
                    }
                )
                for item in extract_videos(data):
                    self._videos[item["video_ref"]] = item
                for item in extract_contacts(data):
                    self._contacts[item["target_ref"]] = item
                while len(self._videos) > 200:
                    self._videos.pop(next(iter(self._videos)))
                while len(self._contacts) > 200:
                    self._contacts.pop(next(iter(self._contacts)))
        except (Exception, asyncio.CancelledError):
            return

    async def _goto(self, url: str, *, check_ready: bool = True):
        try:
            await self._page.goto(url, wait_until="domcontentloaded")
        except Exception as exc:
            raise self._navigation_failure(exc) from exc
        if urlsplit(self._page.url).hostname not in {"www.douyin.com", "douyin.com"}:
            raise PluginError("NAVIGATION_REJECTED", "页面跳转到了不支持的站点。")
        self._navigation_error = None
        if check_ready:
            await self._check_challenge()

    def _navigation_failure(self, exc: Exception) -> PluginError:
        """记录导航阶段的安全原因，不包含网址参数或页面输入。"""
        match = re.search(r"net::[A-Z_]+", str(exc))
        reason = match.group(0) if match else type(exc).__name__
        self._navigation_error = {"stage": "navigation", "reason": reason}
        self.diagnostics.emit(
            "WARNING",
            "BROWSER_NAVIGATION_FAILED",
            "Browser navigation failed; keeping current page",
            self._navigation_error,
        )
        return PluginError(
            "BROWSER_NAVIGATION_FAILED",
            f"浏览器已启动，但抖音页面打开失败（{reason}）。请检查 AstrBot 主机或容器的网络，再点击首页或刷新网页。",
            dict(self._navigation_error),
        )

    async def _check_challenge(self, page=None):
        page = page or self._page
        if (await page.locator("body").inner_text()).strip() == "Please wait...":
            raise PluginError(
                "PAGE_NOT_READY", "抖音页面仍停留在等待状态，请在可见浏览器检查后重试。"
            )
        challenge = page.locator(
            'iframe[src*="captcha"], #captcha_container, [data-e2e="captcha"]'
        )
        for index in range(await challenge.count()):
            if await challenge.nth(index).is_visible():
                raise PluginError(
                    "HUMAN_VERIFICATION_REQUIRED",
                    "抖音要求人工验证，请在登录浏览器完成后继续。",
                )

    async def _unique(self, selector: str, *, scope=None):
        locator = (scope or self._page).locator(selector).filter(visible=True)
        count = await locator.count()
        if count != 1:
            raise PluginError(
                "UI_UNSUPPORTED",
                "页面控件无法唯一定位，已停止操作。",
                {"selector": selector, "matches": count},
            )
        return locator

    async def _account_identity(self, page=None, *, timeout_ms: int = 8000) -> str:
        """通过站内当前用户接口核实账号，不读取登录凭据。

        Args:
            page: 可选的主页面；登录弹窗控制期间仍从主站查询身份。
            timeout_ms: 账号接口的请求预算，单位毫秒。

        Returns:
            已核实的 user:UID 标识。

        Raises:
            PluginError: 尚未登录、需要人工验证或账号接口暂时不可用。
        """
        page = page or self._page
        await self._check_challenge(page)
        # 只读当前用户接口；不读取 Cookie、localStorage 或昵称来推断身份。
        try:
            result = await page.evaluate(
                """async timeoutMs => {
                try {
                    const response = await fetch('/aweme/v1/web/query/user/?aid=6383&device_platform=webapp', {credentials:'include', signal:AbortSignal.timeout(timeoutMs)});
                    if (!response.ok) return {unavailable:true, reason:'http_error', http_status:response.status};
                    let data;
                    try {data=await response.json();} catch {return {unavailable:true,reason:'invalid_json'};}
                    if (!data || typeof data!=='object') return {unavailable:true,reason:'invalid_response'};
                    const uid = data.status_code === 0 && data.user ? data.user.uid : null;
                    return {uid: typeof uid === 'string' ? uid : null};
                } catch {return {unavailable:true,reason:'request_failed'};}
            }""",
                timeout_ms,
            )
        except Exception as exc:
            raise PluginError(
                "ACCOUNT_STATUS_UNAVAILABLE",
                "暂时无法查询登录状态，仍可在 Page 画面中登录或完成验证。",
                {"stage": "account_status", "reason": type(exc).__name__},
            ) from exc
        if not isinstance(result, dict) or result.get("unavailable"):
            raise PluginError(
                "ACCOUNT_STATUS_UNAVAILABLE",
                "暂时无法查询登录状态，仍可在 Page 画面中登录或完成验证。",
                {
                    "stage": "account_status",
                    **(result if isinstance(result, dict) else {}),
                },
            )
        uid = result.get("uid") if isinstance(result, dict) else None
        if (
            not isinstance(uid, str)
            or not re.fullmatch(r"\d{2,30}", uid)
            or set(uid) == {"0"}
        ):
            self._account = ""
            raise PluginError(
                "LOGIN_REQUIRED", "尚未能核实当前登录账号，请在浏览器完成登录。"
            )
        self._account = "user:" + uid
        return self._account

    async def status(self) -> dict:
        capabilities = {
            name: "implemented_live_unverified"
            for name in (
                "browse",
                "search",
                "watch",
                "read_comments",
                "resolve_contact",
                "set_like",
                "post_comment",
            )
        }
        capabilities.update(
            {
                name: "requires_stable_dom_identity"
                for name in ("read_inbox", "share_video", "send_message")
            }
        )
        if self._page is None or self._page.is_closed():
            return {
                "authenticated": False,
                "account_ref": "",
                "browser_started": False,
                "status_available": False,
                "capabilities": capabilities,
            }
        try:
            identity_page = self._remote_main_page
            if identity_page is None or identity_page.is_closed():
                identity_page = self._page
            # 信息面板使用较短预算，避免登录检查长时间占用交互画面。
            account = await self._account_identity(identity_page, timeout_ms=2500)
            return {
                "authenticated": True,
                "account_ref": account,
                "browser_started": True,
                "status_available": True,
                "capabilities": capabilities,
            }
        except PluginError as exc:
            return {
                "authenticated": False,
                "account_ref": "",
                "browser_started": True,
                "code": exc.code,
                "message": exc.message,
                "status_available": exc.code == "LOGIN_REQUIRED",
                "capabilities": capabilities,
            }
        except Exception as exc:
            self.diagnostics.emit(
                "WARNING",
                "ACCOUNT_STATUS_UNAVAILABLE",
                "Account status query unavailable",
                {"error_type": type(exc).__name__},
            )
            return {
                "authenticated": False,
                "account_ref": "",
                "browser_started": True,
                "status_available": False,
                "code": "ACCOUNT_STATUS_UNAVAILABLE",
                "message": "暂时无法查询登录状态，仍可继续查看浏览器画面。",
                "capabilities": capabilities,
            }

    async def start_login(self) -> dict:
        if self.settings.headless or (
            sys.platform.startswith("linux")
            and not (os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"))
        ):
            raise PluginError(
                "HEADFUL_REQUIRED",
                "当前环境使用无窗口浏览器，请从插件 Page 完成扫码登录。",
            )
        await self._ensure()
        return {
            "status": "ok",
            "code": "LOGIN_BROWSER_OPENED",
            "message": "已打开独立抖音浏览器，请在该机器手动扫码登录，再用 /dy status 核对账号。",
        }

    @staticmethod
    def _remote_url_allowed(url: str) -> bool:
        try:
            parsed = urlsplit(url)
            return (
                parsed.scheme == "https"
                and parsed.hostname in REMOTE_HOSTS
                and parsed.port in (None, 443)
                and parsed.username is None
                and parsed.password is None
            )
        except (TypeError, ValueError):
            return False

    def _remote_frame_url_allowed(self, url: str) -> bool:
        # 只展示本插件固定站内导航失败留下的内部错误页，不扩大输入或导航权限。
        return self._remote_url_allowed(url) or (
            self._navigation_error is not None
            and url in {"about:blank", "chrome-error://chromewebdata/"}
        )

    async def _remote_navigation_guard(self, route):
        request = route.request
        if request.is_navigation_request():
            try:
                top_level = request.frame.parent_frame is None
            except Exception:
                # 弹窗首个导航有时尚未绑定 Frame，按顶层导航处理。
                top_level = True
            if top_level and not self._remote_url_allowed(request.url):
                try:
                    if request.frame.page is self._page:
                        self._remote_blocked = True
                        self._remote_frame = None
                except Exception:
                    pass
                await route.abort("blockedbyclient")
                return
        await route.fallback()

    def _remote_navigated(self, frame):
        if self._page is not None and frame == self._page.main_frame:
            self._remote_frame = None

    def _remote_popup(self, page):
        if self._remote_closing or self._remote_tasks:
            return
        context = self._remote_context
        if context is None:
            return

        async def drain():
            # 只有一个接管 worker；主页面和一个登录弹窗以外的页面逐个关闭。
            while not self._remote_closing:
                candidates = [
                    item
                    for item in context.pages
                    if item not in (self._remote_main_page, self._remote_popup_page)
                    and not item.is_closed()
                ]
                if not candidates:
                    return
                for candidate in candidates:
                    await self._remote_adopt_popup(candidate)

        def finished(task):
            self._remote_tasks.discard(task)
            if not self._remote_closing and any(
                item not in (self._remote_main_page, self._remote_popup_page)
                for item in context.pages
            ):
                self._remote_popup(None)

        task = asyncio.create_task(drain(), name="douyin-login-popup")
        self._remote_tasks.add(task)
        task.add_done_callback(finished)

    async def _remote_adopt_popup(self, page):
        try:
            if (
                self._remote_popup_page is not None
                and not self._remote_popup_page.is_closed()
            ):
                if page is not self._remote_popup_page:
                    await page.close()
                return
            await page.wait_for_load_state("domcontentloaded", timeout=8000)
            if not self._remote_url_allowed(page.url):
                await page.close()
                return
            if self._remote_main_page is None or self._remote_main_page.is_closed():
                self._remote_main_page = self._page
            self._remote_popup_page = page
            self._page = page
            self._remote_frame = None
            page.on("response", self._schedule_capture)
            self._remote_watch_page(page)
        except (Exception, asyncio.CancelledError):
            try:
                await page.close()
            except Exception:
                pass

    def _remote_page_closed(self, page):
        self._remote_pages.discard(page)
        page.remove_listener("framenavigated", self._remote_navigated)
        page.remove_listener("popup", self._remote_popup)
        page.remove_listener("response", self._schedule_capture)
        page.remove_listener("close", self._remote_page_closed)
        self._remote_frame = None
        if page is self._remote_popup_page:
            self._remote_popup_page = None
        if page is self._remote_main_page:
            self._remote_main_page = self._remote_popup_page
            self._remote_popup_page = None
        if page is self._page:
            self._page = self._remote_main_page

    async def _remote_return_to_main(self):
        """结束人工弹窗控制，保留同一上下文中的登录 Cookie。

        Returns:
            无返回值；有登录弹窗时恢复主页面并刷新其登录界面。

        Raises:
            asyncio.CancelledError: 等待弹窗或页面刷新时被取消。
        """
        if self._remote_tasks:
            await asyncio.gather(*self._remote_tasks)
        main = self._remote_main_page
        popup = self._remote_popup_page
        if main is not None and not main.is_closed() and popup is not None:
            self._page = main
            self._remote_frame = None
            self._remote_popup_page = None
            await popup.close()
            try:
                await main.reload(wait_until="domcontentloaded")
            except Exception as exc:
                raise self._navigation_failure(exc) from exc
            self._navigation_error = None
            self._remote_blocked = False

    def _remote_watch_page(self, page):
        if page not in self._remote_pages:
            self._remote_pages.add(page)
            page.on("framenavigated", self._remote_navigated)
            page.on("popup", self._remote_popup)
            page.on("close", self._remote_page_closed)

    async def _remote_prepare(
        self, *, check_site: bool = True, allow_error_page: bool = False
    ):
        # Page 首次启动在无桌面的服务器同样可用；已有浏览器原样复用。
        await self._ensure(headless=True)
        context = self._page.context
        if self._remote_main_page is None or self._remote_main_page.is_closed():
            self._remote_main_page = self._page
        if context is not self._remote_context:
            await context.route("**/*", self._remote_navigation_guard)
            self._remote_context = context
        self._remote_watch_page(self._page)
        url_allowed = (
            self._remote_frame_url_allowed(self._page.url)
            if allow_error_page
            else self._remote_url_allowed(self._page.url)
        )
        if check_site and (self._remote_blocked or not url_allowed):
            self._remote_frame = None
            raise PluginError(
                "REMOTE_SITE_REJECTED", "当前页面不是允许控制的抖音页面。"
            )

    async def remote_frame(self) -> dict:
        """内存截图供已鉴权管理员操作；不绕过等待页或人工验证。"""
        await self._remote_prepare(allow_error_page=True)
        page = self._page
        original_url = page.url
        self._remote_frame = None
        dimensions = await page.evaluate(
            "() => ({width:innerWidth,height:innerHeight})"
        )
        width, height = dimensions["width"], dimensions["height"]
        if not (0 < width <= 2560 and 0 < height <= 1600):
            raise PluginError("REMOTE_VIEWPORT_INVALID", "浏览器画面尺寸超出控制范围。")
        capture = await page.screenshot(
            type="jpeg",
            quality=70,
            scale="css",
            timeout=8000,
            mask=[page.locator('input[type="password"]')],
        )
        if (
            page is not self._page
            or page.url != original_url
            or not self._remote_frame_url_allowed(page.url)
        ):
            raise PluginError(
                "REMOTE_FRAME_CHANGED", "截图时页面已跳转，请重新获取画面。"
            )
        if len(capture) > 2 * 1024 * 1024:
            raise PluginError("REMOTE_FRAME_TOO_LARGE", "当前浏览器画面超过传输上限。")
        frame_id = uuid4().hex
        self._remote_frame = {
            "frame_id": frame_id,
            "page": page,
            "url": original_url,
            "width": width,
            "height": height,
            "expires": asyncio.get_running_loop().time() + 30,
        }
        parsed = urlsplit(original_url)
        return {
            "image": "data:image/jpeg;base64,"
            + base64.b64encode(capture).decode("ascii"),
            "frame_id": frame_id,
            "width": width,
            "height": height,
            "url": urlunsplit((parsed.scheme, parsed.netloc, parsed.path, "", "")),
            "expires_in_seconds": 30,
        }

    async def remote_input(self, kind: str, frame_id: str, **params) -> dict:
        """对管理员看到的短期画面发送一次输入，不推断站内业务结果。"""
        await self._remote_prepare()
        frame = self._remote_frame
        if (
            not frame
            or not isinstance(frame_id, str)
            or frame_id != frame["frame_id"]
            or frame["page"] is not self._page
            or frame["url"] != self._page.url
            or asyncio.get_running_loop().time() >= frame["expires"]
        ):
            raise PluginError(
                "REMOTE_FRAME_STALE", "操作画面已过期，请刷新画面后再试。"
            )
        shapes = {
            "click": {"x", "y"},
            "text": {"text"},
            "key": {"key"},
            "scroll": {"delta_x", "delta_y"},
            "drag": {"points"},
        }
        if kind not in shapes or set(params) - shapes[kind]:
            raise PluginError("REMOTE_INPUT_INVALID", "不支持该浏览器输入。")

        def finite_number(value):
            return type(value) in (int, float) and math.isfinite(value)

        def point_valid(point):
            return (
                isinstance(point, dict)
                and set(point) == {"x", "y"}
                and finite_number(point["x"])
                and finite_number(point["y"])
                and 0 <= point["x"] < frame["width"]
                and 0 <= point["y"] < frame["height"]
            )

        if kind == "click":
            valid = point_valid(params)
        elif kind == "text":
            value = params.get("text")
            valid = (
                isinstance(value, str)
                and 0 < len(value) <= 2000
                and "\x00" not in value
            )
        elif kind == "key":
            valid = isinstance(params.get("key"), str) and params["key"] in REMOTE_KEYS
        elif kind == "scroll":
            params.setdefault("delta_x", 0)
            valid = "delta_y" in params and all(
                finite_number(v) and abs(v) <= 2000 for v in params.values()
            )
        else:
            points = params.get("points")
            valid = (
                isinstance(points, list)
                and 2 <= len(points) <= 100
                and all(point_valid(point) for point in points)
            )
        if not valid:
            raise PluginError(
                "REMOTE_INPUT_INVALID", "输入超出允许的长度、按键或画面范围。"
            )
        # 一旦准备发送就消费画面标识；中断时不能凭相同标识重复点击。
        self._remote_frame = None
        page = self._page
        try:
            if kind == "click":
                await page.mouse.click(params["x"], params["y"])
            elif kind == "text":
                await page.keyboard.insert_text(params["text"])
            elif kind == "key":
                await page.keyboard.press(params["key"])
            elif kind == "scroll":
                await page.mouse.wheel(params["delta_x"], params["delta_y"])
            else:
                points = params["points"]
                await page.mouse.move(points[0]["x"], points[0]["y"])
                await page.mouse.down()
                try:
                    for point in points[1:]:
                        await page.mouse.move(point["x"], point["y"])
                        await asyncio.sleep(0.01)
                finally:
                    await page.mouse.up()
        except (Exception, asyncio.CancelledError):
            return {
                "status": "unknown_result",
                "code": "MANUAL_INPUT_UNKNOWN",
                "kind": kind,
            }
        if self._remote_blocked or not self._remote_url_allowed(self._page.url):
            raise PluginError(
                "REMOTE_SITE_REJECTED",
                "人工输入已发送，但页面尝试离开抖音，已停止控制。",
            )
        return {"status": "submitted", "code": "MANUAL_INPUT_DISPATCHED", "kind": kind}

    async def remote_navigate(self, destination: str) -> dict:
        """只提供固定站内入口；Page 登录不要求宿主桌面环境。"""
        if destination not in {"home", "login", "inbox", "reload"}:
            raise PluginError(
                "REMOTE_DESTINATION_INVALID", "只能打开首页、登录、私信或刷新。"
            )
        await self._remote_prepare(check_site=destination == "reload")
        if destination != "reload":
            await self._remote_return_to_main()
        self._remote_frame = None
        if self._remote_blocked:
            # 被阻断的导航可能仍在切换 Chrome 错误页；在同一账号上下文新建标签恢复。
            previous = self._page
            self._page = await previous.context.new_page()
            self._remote_main_page = self._page
            self._page.on("response", self._schedule_capture)
            self._remote_watch_page(self._page)
            previous.remove_listener("framenavigated", self._remote_navigated)
            previous.remove_listener("popup", self._remote_popup)
            previous.remove_listener("response", self._schedule_capture)
            previous.remove_listener("close", self._remote_page_closed)
            self._remote_pages.discard(previous)
            await previous.close()
        self._remote_blocked = False
        if destination == "reload":
            try:
                await self._page.reload(wait_until="domcontentloaded")
            except Exception as exc:
                raise self._navigation_failure(exc) from exc
            self._navigation_error = None
        elif destination == "inbox":
            await self._goto("https://www.douyin.com/", check_ready=False)
            await (await self._unique(ui.MESSAGES_OPEN)).click()
        else:
            await self._goto("https://www.douyin.com/", check_ready=False)
            if destination == "login":
                # 登录 UI 可能已打开，也可能是等待页；保留画面让用户处理。
                button = self._page.get_by_role(
                    "button", name="登录", exact=True
                ).filter(visible=True)
                if await button.count() == 1:
                    await button.click()
        if not self._remote_url_allowed(self._page.url):
            raise PluginError(
                "REMOTE_SITE_REJECTED", "页面跳转到了不支持的站点，已停止控制。"
            )
        return {
            "status": "ok",
            "code": "REMOTE_PAGE_OPENED",
            "destination": destination,
        }

    async def _read_ready(self):
        await self._ensure()
        await self._remote_return_to_main()
        account = await self._account_identity()
        if (
            self.settings.expected_account_ref
            and account != self.settings.expected_account_ref
        ):
            raise PluginError("ACCOUNT_MISMATCH", "浏览器账号与配置绑定的账号不一致。")

    async def _write_ready(self):
        account = await self._account_identity()
        if (
            not self.settings.expected_account_ref
            or account != self.settings.expected_account_ref
        ):
            raise PluginError("ACCOUNT_MISMATCH", "提交前账号身份核对失败。")

    async def _wait_data(
        self, start: int, predicate, wait_seconds: float = 6
    ) -> dict | None:
        deadline = asyncio.get_running_loop().time() + wait_seconds
        while asyncio.get_running_loop().time() < deadline:
            for response in self._responses:
                if response["seq"] > start and predicate(response):
                    return response
            await asyncio.sleep(0.15)
        return None

    async def _load_video(self, identifier: str) -> dict:
        start = self._seq
        # 清掉旧详情，避免将缓存的点赞状态当成本次核验依据。
        self._videos.pop(identifier, None)
        await self._goto(f"https://www.douyin.com/video/{identifier}")
        captured = await self._wait_data(
            start,
            lambda row: (
                "/aweme/detail/" in row["path"]
                and row["query"].get("aweme_id") == [identifier]
            ),
        )
        items = extract_videos(captured["data"]) if captured else []
        if not items:
            # 部分网页使用服务端 JSON 预载数据；只解码 JSON，不执行脚本文本。
            embedded = self._page.locator('script#RENDER_DATA[type="application/json"]')
            if await embedded.count() == 1:
                encoded = await embedded.text_content()
                if encoded and len(encoded) <= 4 * 1024 * 1024:
                    try:
                        items = extract_videos(json.loads(unquote(encoded)))
                    except (ValueError, TypeError):
                        pass
        item = next((item for item in items if item["video_ref"] == identifier), None)
        if item is None:
            raise PluginError(
                "VIDEO_DATA_UNAVAILABLE",
                "页面未提供可识别的视频详情，不能据此执行互动。",
            )
        return copy.deepcopy(item)

    async def watch(self, video_ref: str) -> dict:
        await self._read_ready()
        item = await self._load_video(video_id(video_ref))
        player = await self._unique(ui.VIDEO)
        # 实际播放器状态来自页面，而不是推荐响应中的曝光列表。
        playback = await player.evaluate("""async el => {
            try { await Promise.race([el.play(), new Promise(resolve=>setTimeout(resolve,1500))]); } catch (_) {}
            return {current_time:el.currentTime, paused:el.paused, ready_state:el.readyState};
        }""")
        item.update(
            observation_kind="opened", observed_at=datetime.now(UTC).isoformat()
        )
        item["evidence"].append({"kind": "visible_player", "playback": playback})
        return item

    async def browse(self, limit: int = 3, dwell_seconds: int = 3) -> dict:
        await self._read_ready()
        start = self._seq
        await self._goto("https://www.douyin.com/")
        captured = await self._wait_data(
            start,
            lambda row: "/feed/" in row["path"] and bool(extract_videos(row["data"])),
        )
        if not captured:
            raise PluginError(
                "FEED_UNAVAILABLE", "没有读取到本次推荐流，请检查登录或平台页面。"
            )
        candidates = [
            item
            for item in extract_videos(captured["data"])
            if item["video_ref"] not in self._feed_seen
        ][:limit]
        observed = []
        for candidate in candidates:
            item = await self.watch(candidate["video_ref"])
            player = await self._unique(ui.VIDEO)
            before = await player.evaluate(
                "el => ({time:el.currentTime, duration:el.duration, paused:el.paused})"
            )
            await asyncio.sleep(dwell_seconds)
            after = await player.evaluate(
                "el => ({time:el.currentTime, duration:el.duration, paused:el.paused})"
            )
            advanced = after["time"] > before["time"] and not after["paused"]
            item["observation_kind"] = "played_sample" if advanced else "opened"
            item["evidence"].append(
                {
                    "kind": "playback_sample",
                    "before": before,
                    "after": after,
                    "requested_seconds": dwell_seconds,
                    "playback_advanced": advanced,
                }
            )
            observed.append(item)
            self._feed_seen.append(item["video_ref"])
        return {
            "status": "ok" if observed else "partial",
            "videos": observed,
            "continuation": None,
            "message": "从本次推荐响应选取少量作品逐条打开；不声称复现手机推荐流或完整观看。",
        }

    async def search(self, query: str, limit: int = 10) -> dict:
        await self._read_ready()
        start = self._seq
        await self._goto(
            f"https://www.douyin.com/search/{quote(query, safe='')}?type=video"
        )
        captured = await self._wait_data(
            start,
            lambda row: "search" in row["path"] and bool(extract_videos(row["data"])),
        )
        if not captured:
            raise PluginError("SEARCH_UNAVAILABLE", "未读取到可识别的视频搜索响应。")
        return {
            "status": "ok",
            "videos": extract_videos(captured["data"])[:limit],
            "observation_kind": "candidates",
            "query": query,
        }

    async def read_comments(
        self, video_ref: str, limit: int = 20, cursor: str = ""
    ) -> dict:
        await self._read_ready()
        identifier = video_id(video_ref)
        if cursor.startswith("local:"):
            saved = self._comment_pages.get(cursor)
            if (
                not saved
                or saved["account_ref"] != self._account
                or saved["video_ref"] != identifier
            ):
                raise PluginError(
                    "CURSOR_UNAVAILABLE", "评论游标已失效或不属于当前账号和作品。"
                )
            return self._comment_page(copy.deepcopy(saved["result"]), limit, identifier)
        start = self._seq
        await self._load_video(identifier)
        opener = self._page.locator(ui.COMMENT_OPEN).filter(visible=True)
        if await opener.count() == 1:
            await opener.click()
        captured = await self._wait_data(
            start,
            lambda row: (
                "/comment/list/" in row["path"]
                and row["query"].get("aweme_id") == [identifier]
            ),
        )
        if not captured:
            raise PluginError("COMMENTS_UNAVAILABLE", "未读取到该作品的评论响应。")
        # 页面滚动触发原生分页，不拼接签名请求；目标游标必须来自本次实际响应。
        if cursor and cursor != "0":
            for _ in range(3):
                if captured["query"].get("cursor", ["0"])[0] == cursor:
                    break
                start = self._seq
                panel = await self._unique('[data-e2e="comment-list"]')
                await panel.evaluate("el => el.scrollTop = el.scrollHeight")
                next_page = await self._wait_data(
                    start,
                    lambda row: (
                        "/comment/list/" in row["path"]
                        and row["query"].get("aweme_id") == [identifier]
                    ),
                )
                if not next_page:
                    break
                captured = next_page
            if captured["query"].get("cursor", ["0"])[0] != cursor:
                raise PluginError(
                    "CURSOR_UNAVAILABLE", "当前页面无法恢复到指定评论游标。"
                )
        result = comments(captured["data"])
        return self._comment_page(result, limit, identifier)

    def _comment_page(self, result: dict, limit: int, identifier: str) -> dict:
        result["video_ref"] = identifier
        if len(result["comments"]) > limit:
            cursor = "local:" + uuid4().hex
            self._comment_pages[cursor] = {
                "account_ref": self._account,
                "video_ref": identifier,
                "result": {**result, "comments": result["comments"][limit:]},
            }
            while len(self._comment_pages) > 32:
                self._comment_pages.pop(next(iter(self._comment_pages)))
            result = {
                **result,
                "comments": result["comments"][:limit],
                "cursor": cursor,
                "has_more": True,
            }
        return result

    async def resolve_contact(self, query: str, limit: int = 10) -> dict:
        await self._read_ready()
        if query in self._contacts:
            return {"contacts": [copy.deepcopy(self._contacts[query])], "exact": True}
        start = self._seq
        await self._goto(
            f"https://www.douyin.com/search/{quote(query, safe='')}?type=user"
        )
        captured = await self._wait_data(
            start,
            lambda row: "search" in row["path"] and bool(extract_contacts(row["data"])),
        )
        if not captured:
            raise PluginError(
                "CONTACTS_UNAVAILABLE", "未读取到带稳定身份的联系人候选。"
            )
        rows = extract_contacts(captured["data"])[:limit]
        return {
            "contacts": rows,
            "exact": False,
            "message": "搜索结果仅为候选，请使用核对后的 target_ref，不按昵称自动选择。",
        }

    async def set_like(self, video_ref: str, liked: bool) -> dict:
        await self._read_ready()
        identifier = video_id(video_ref)
        item = await self._load_video(identifier)
        if item["liked"] is None:
            raise PluginError(
                "LIKE_STATE_UNKNOWN", "无法核实当前点赞状态，不能安全切换。"
            )
        if item["liked"] == liked:
            return {
                "status": "verified",
                "code": "ALREADY_IN_DESIRED_STATE",
                "video_ref": identifier,
                "liked": liked,
                "verification": "fresh_detail_user_digged",
            }
        button = await self._unique(ui.LIKE)
        await self._write_ready()
        start = self._seq
        try:
            await button.click()
            response = await self._wait_data(
                start,
                lambda row: (
                    "/commit/item/digg/" in row["path"]
                    and row["post"].get("aweme_id") == [identifier]
                    and row["post"].get("type") == ["1" if liked else "0"]
                ),
            )
            if not response:
                return {
                    "status": "unknown_result",
                    "code": "LIKE_RECEIPT_MISSING",
                    "video_ref": identifier,
                }
            if response["data"].get("status_code") != 0:
                return {
                    "status": "failed",
                    "code": "PLATFORM_REJECTED",
                    "platform_status": response["data"].get("status_code"),
                }
            refreshed = await self._load_video(identifier)
            verified = refreshed["liked"] == liked
            return {
                "status": "verified" if verified else "submitted",
                "code": "LIKE_VERIFIED" if verified else "LIKE_VISIBILITY_PENDING",
                "video_ref": identifier,
                "liked": liked,
                "verification": "fresh_detail_user_digged"
                if verified
                else "platform_response",
            }
        except (Exception, asyncio.CancelledError):
            return {
                "status": "unknown_result",
                "code": "LIKE_RESULT_UNKNOWN",
                "video_ref": identifier,
            }

    async def post_comment(
        self,
        video_ref: str,
        text: str,
        reply_to: str = "",
        mentions: list | None = None,
    ) -> dict:
        """发布评论并核对该次提交的评论记录与原生提及实体。

        Args:
            video_ref: 作品 ID 或标准抖音链接。
            text: 已由调用者确定的评论正文。
            reply_to: 可选、已核对的评论 ID。
            mentions: 含已解析 target_ref 的用户列表。

        Returns:
            有完整提交回执时返回 submitted；回执缺失、正文或身份不一致
            以及提交后中断均返回 unknown_result，不表示公开可见。

        Raises:
            PluginError: 点击提交前账号、目标或页面控件校验失败。
        """
        await self._read_ready()
        identifier = video_id(video_ref)
        await self._load_video(identifier)
        opener = self._page.locator(ui.COMMENT_OPEN).filter(visible=True)
        if await opener.count() == 1:
            await opener.click()
        if reply_to:
            if not re.fullmatch(r"\d{2,30}", reply_to):
                raise PluginError("INVALID_COMMENT_REF", "回复需要稳定的评论 ID。")
            target = await self._unique(
                f'[data-comment-id="{reply_to}"], [data-cid="{reply_to}"]'
            )
            reply = target.get_by_text("回复", exact=True)
            if await reply.count() != 1:
                raise PluginError(
                    "REPLY_TARGET_UNAVAILABLE", "无法唯一核对被回复的评论。"
                )
            await reply.click()
        editor = await self._unique(ui.COMMENT_EDITOR)
        await editor.fill(text)
        expected_mentions = []
        for mention in mentions or []:
            reference = mention["target_ref"]
            target = self._contacts.get(reference)
            if not target or not target["uid"] or not target["sec_uid"]:
                raise PluginError(
                    "MENTION_IDENTITY_UNAVAILABLE",
                    "请先解析并核实被提及用户的稳定身份。",
                )
            await editor.press("End")
            await editor.press_sequentially(" @" + target["nickname"])
            choice = await self._unique(
                f'[role="option"][data-uid="{target["uid"]}"], [data-e2e="mention-option"][data-uid="{target["uid"]}"]'
            )
            await choice.click()
            token = editor.locator(
                f'[data-uid="{target["uid"]}"], [data-user-id="{target["uid"]}"]'
            )
            if await token.count() != 1:
                raise PluginError(
                    "MENTION_ENTITY_UNVERIFIED", "编辑框中未形成可验证的真实提及实体。"
                )
            expected_mentions.append(target["uid"])
        expected_text = await editor.evaluate(
            "el => typeof el.value === 'string' ? el.value : el.innerText"
        )
        submit = await self._unique(ui.COMMENT_SUBMIT)
        await self._write_ready()
        start = self._seq
        try:
            await submit.click()
            response = await self._wait_data(
                start,
                lambda row: (
                    "/comment/publish/" in row["path"]
                    and row["post"].get("aweme_id") == [identifier]
                ),
            )
            if not response:
                return {
                    "status": "unknown_result",
                    "code": "COMMENT_RECEIPT_MISSING",
                    "video_ref": identifier,
                }
            payload = response["data"]
            if payload.get("status_code") != 0:
                return {
                    "status": "failed",
                    "code": "PLATFORM_REJECTED",
                    "platform_status": payload.get("status_code"),
                }
            created = payload.get("comment") or {}
            posted_text = response["post"].get("text", [""])[0]
            comment_ref = str(created.get("cid") or "")
            # 平台状态码不能代替评论记录；按本次实际请求核对正文与作品。
            if (
                not 200 <= response.get("http_status", 0) < 300
                or not re.fullmatch(r"\d{2,30}", comment_ref)
                or str(created.get("aweme_id") or identifier) != identifier
                or not isinstance(created.get("text"), str)
                or created["text"].replace("\r\n", "\n").strip()
                != posted_text.replace("\r\n", "\n").strip()
                or posted_text.replace("\r\n", "\n").strip()
                != expected_text.replace("\r\n", "\n").strip()
            ):
                return {
                    "status": "unknown_result",
                    "code": "COMMENT_RECEIPT_UNVERIFIED",
                    "video_ref": identifier,
                    "message": "发布响应缺少有效评论记录，或作品与正文不匹配，请按原请求核对结果。",
                }
            extras = {
                str(extra.get("user_id") or extra.get("uid") or "")
                for extra in created.get("text_extra", [])
                if isinstance(extra, dict)
            }
            mention_verified = all(uid in extras for uid in expected_mentions)
            reply_verified = (
                not reply_to or str(created.get("reply_id") or "") == reply_to
            )
            return {
                "status": "submitted"
                if mention_verified and reply_verified
                else "unknown_result",
                "code": "COMMENT_SUBMITTED"
                if mention_verified and reply_verified
                else "COMMENT_METADATA_UNVERIFIED",
                "video_ref": identifier,
                "comment_ref": comment_ref,
                "mentions_verified": mention_verified,
                "reply_verified": reply_verified,
                "visibility_verified": False,
                "verification": "platform_publish_response",
                "message": "平台已返回提交回执；公开可见性仍待核验。",
            }
        except (Exception, asyncio.CancelledError):
            return {
                "status": "unknown_result",
                "code": "COMMENT_RESULT_UNKNOWN",
                "video_ref": identifier,
            }

    async def _open_inbox(self):
        await self._read_ready()
        opener = await self._unique(ui.MESSAGES_OPEN)
        await opener.click()

    async def read_inbox(
        self, conversation_ref: str = "", limit: int = 20, cursor: str = ""
    ) -> dict:
        await self._open_inbox()
        if cursor:
            raise PluginError(
                "INBOX_CURSOR_UNSUPPORTED",
                "当前网页实现仅读取可见消息，不支持历史游标恢复。",
            )
        if not conversation_ref:
            rows = await self._page.locator(ui.INBOX_CONVERSATIONS).evaluate_all(
                "els => els.filter(el=>el.getClientRects().length).map(el=>({conversation_id:el.dataset.conversationId,text:el.innerText.slice(0,500)}))"
            )
            unique = {
                row["conversation_id"]: {
                    "conversation_ref": "conversation:" + row["conversation_id"],
                    "preview": row["text"],
                }
                for row in rows
                if re.fullmatch(
                    r"[A-Za-z0-9_:-]{2,160}", row.get("conversation_id", "")
                )
            }
            if not unique:
                raise PluginError(
                    "INBOX_IDENTITY_UNAVAILABLE",
                    "当前页面没有暴露可核对的稳定会话 ID。",
                )
            return {
                "conversations": list(unique.values())[:limit],
                "coverage": "visible_only",
            }
        thread = await self._conversation(conversation_ref)
        await thread.click()
        panel = await self._unique(
            f'[data-conversation-panel="{conversation_ref.removeprefix("conversation:")}"]'
        )
        rows = await panel.locator(ui.INBOX_MESSAGES).evaluate_all(
            "els => els.filter(el=>el.getClientRects().length).map(el=>({message_ref:el.dataset.messageId,text:el.innerText.slice(0,4000),direction:el.dataset.direction||'unknown'}))"
        )
        return {
            "conversation_ref": conversation_ref,
            "messages": rows[-limit:],
            "coverage": "visible_only",
            "cursor": None,
        }

    async def _conversation(self, reference: str):
        if not reference.startswith("conversation:") or not re.fullmatch(
            r"[A-Za-z0-9_:-]{2,160}", reference.removeprefix("conversation:")
        ):
            raise PluginError(
                "INVALID_CONVERSATION_REF", "需要已读取并核对的 conversation_ref。"
            )
        identifier = reference.removeprefix("conversation:")
        return await self._unique(f'[data-conversation-id="{identifier}"]')

    async def send_message(self, conversation_ref: str, text: str) -> dict:
        await self._open_inbox()
        thread = await self._conversation(conversation_ref)
        await thread.click()
        panel = await self._unique(
            f'[data-conversation-panel="{conversation_ref.removeprefix("conversation:")}"]'
        )
        editor = await self._unique(ui.MESSAGE_EDITOR, scope=panel)
        sender = await self._unique(ui.MESSAGE_SEND, scope=panel)
        before = set(
            await panel.locator(ui.INBOX_MESSAGES).evaluate_all(
                "els=>els.map(el=>el.dataset.messageId)"
            )
        )
        await editor.fill(text)
        await self._write_ready()
        try:
            await sender.click()
            for _ in range(20):
                rows = await panel.locator(
                    '[data-message-id][data-direction="outgoing"]'
                ).evaluate_all(
                    "els=>els.map(el=>({id:el.dataset.messageId,text:el.innerText,status:el.dataset.status}))"
                )
                for row in rows:
                    if (
                        row["id"] not in before
                        and row["text"] == text
                        and row["status"] in {"sent", "delivered"}
                    ):
                        return {
                            "status": "verified",
                            "code": "MESSAGE_VERIFIED",
                            "conversation_ref": conversation_ref,
                            "message_ref": row["id"],
                            "verification": "new_outgoing_message_with_sent_state",
                        }
                await asyncio.sleep(0.2)
            return {
                "status": "unknown_result",
                "code": "MESSAGE_RECEIPT_MISSING",
                "conversation_ref": conversation_ref,
            }
        except (Exception, asyncio.CancelledError):
            return {
                "status": "unknown_result",
                "code": "MESSAGE_RESULT_UNKNOWN",
                "conversation_ref": conversation_ref,
            }

    async def share_video(self, video_ref: str, target_ref: str) -> dict:
        """从原生分享面板发送视频，并只接受本次新增的稳定回执。

        Args:
            video_ref: 作品 ID 或标准抖音链接。
            target_ref: 已解析且获授权的用户标识。

        Returns:
            新回执 ID、作品和目标均匹配时返回 verified；无法关联本次提交
            或中断时返回 unknown_result。

        Raises:
            PluginError: 提交前无法核对账号、收件人、卡片或控件。
        """
        await self._read_ready()
        identifier = video_id(video_ref)
        target = self._contacts.get(target_ref)
        if not target or not target["uid"]:
            raise PluginError(
                "CONTACT_IDENTITY_UNAVAILABLE", "请先解析并核对分享对象。"
            )
        await self._load_video(identifier)
        opener = await self._unique(ui.SHARE_OPEN)
        await opener.click()
        dialog = await self._unique(ui.SHARE_DIALOG)
        recipient = await self._unique(f'[data-uid="{target["uid"]}"]', scope=dialog)
        await recipient.click()
        selected = dialog.locator(
            f'[data-uid="{target["uid"]}"][aria-selected="true"], [data-uid="{target["uid"]}"][data-selected="true"]'
        )
        if await selected.count() != 1:
            raise PluginError("SHARE_TARGET_UNVERIFIED", "分享面板未确认选中目标账号。")
        # 必须由原生视频面板发送卡片；不把发送文本链接冒充原生转发。
        card = dialog.locator(f'[data-aweme-id="{identifier}"]')
        if await card.count() != 1:
            raise PluginError("SHARE_CARD_UNVERIFIED", "分享面板无法核对原生视频卡片。")
        sender = dialog.get_by_role("button", name="发送", exact=True)
        if await sender.count() != 1:
            raise PluginError("SHARE_SEND_UNAVAILABLE", "分享面板没有唯一的发送按钮。")
        await self._write_ready()
        receipt_selector = f'[data-share-receipt][data-aweme-id="{identifier}"][data-target-uid="{target["uid"]}"][data-status="sent"]'
        receipt_ids = "els => els.map(el=>(el.dataset.messageId || el.dataset.receiptId || el.dataset.shareReceiptId || '').trim()).filter(id=>id && id.length<=256)"
        previous = set(
            await self._page.locator("[data-share-receipt]").evaluate_all(receipt_ids)
        )
        try:
            await sender.click()
            # 旧回执即使仍可见也不能用于本次操作，必须有新稳定 ID。
            for _ in range(20):
                identifiers = (
                    await self._page.locator(receipt_selector)
                    .filter(visible=True)
                    .evaluate_all(receipt_ids)
                )
                fresh = next(
                    (value for value in identifiers if value not in previous), None
                )
                if fresh:
                    return {
                        "status": "verified",
                        "code": "SHARE_VERIFIED",
                        "video_ref": identifier,
                        "target_ref": target_ref,
                        "message_ref": fresh,
                        "verification": "new_native_card_receipt_with_target",
                    }
                await asyncio.sleep(0.25)
            return {
                "status": "unknown_result",
                "code": "SHARE_RECEIPT_MISSING",
                "video_ref": identifier,
                "target_ref": target_ref,
            }
        except (Exception, asyncio.CancelledError):
            return {
                "status": "unknown_result",
                "code": "SHARE_RESULT_UNKNOWN",
                "video_ref": identifier,
                "target_ref": target_ref,
            }

    async def close(self):
        self._remote_closing = True
        self._remote_frame = None
        self._remote_blocked = False
        self._navigation_error = None
        for page in self._remote_pages:
            page.remove_listener("framenavigated", self._remote_navigated)
            page.remove_listener("popup", self._remote_popup)
            page.remove_listener("response", self._schedule_capture)
            page.remove_listener("close", self._remote_page_closed)
        self._remote_pages.clear()
        for task in list(self._remote_tasks):
            task.cancel()
        await asyncio.gather(*self._remote_tasks, return_exceptions=True)
        self._remote_tasks.clear()
        self._remote_main_page = self._remote_popup_page = None
        if self._remote_context is not None:
            try:
                await self._remote_context.unroute(
                    "**/*", self._remote_navigation_guard
                )
            except Exception:
                pass
            self._remote_context = None
        if self._page is not None:
            self._page.remove_listener("response", self._schedule_capture)
        for task in list(self._capture_tasks):
            task.cancel()
        await asyncio.gather(*self._capture_tasks, return_exceptions=True)
        self._capture_tasks.clear()
        try:
            if self._context is not None:
                await self._context.close()
        finally:
            self._context = self._page = None
            try:
                if self._playwright is not None:
                    await self._playwright.stop()
            finally:
                self._playwright = None
                if self._profile_lock is not None:
                    self._profile_lock.close()
                    self._profile_lock = None
                self._account = ""
                self._responses.clear()
                self._videos.clear()
                self._contacts.clear()
                self._comment_pages.clear()
                self._feed_seen.clear()
