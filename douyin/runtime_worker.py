"""独立子进程中的浏览器准备流程，不修改 AstrBot 进程环境。"""

import errno
import json
import os
import shutil
import subprocess
import sys
import time
from contextlib import contextmanager
from importlib.metadata import version
from pathlib import Path

EVENT_PREFIX = "DOUYIN_BROWSER_RUNTIME "


class PreparationError(Exception):
    def __init__(self, code: str, message: str, detail: str = ""):
        super().__init__(message)
        self.code = code
        self.message = message
        self.detail = detail


def emit(state: str, message: str, **fields) -> None:
    print(
        "\n"
        + EVENT_PREFIX
        + json.dumps({"state": state, "message": message, **fields}, ensure_ascii=True),
        flush=True,
    )


@contextmanager
def installation_lock(root: Path):
    """串行准备共享的数据目录；进程退出时由操作系统释放文件锁。

    Args:
        root: 当前插件的浏览器运行目录。

    Yields:
        无值；持锁期间可检查、下载和验证浏览器。

    Raises:
        PreparationError: 数据目录不可写、锁不支持或等待其他实例超时。
    """
    try:
        root.mkdir(parents=True, exist_ok=True)
        handle = (root / "prepare.lock").open("a+b")
    except OSError as exc:
        raise PreparationError(
            "BROWSER_RUNTIME_STORAGE_FAILED",
            "浏览器运行目录不可写，请检查 AstrBot 数据目录权限。",
            str(exc),
        ) from exc
    try:
        handle.seek(0, 2)
        if handle.tell() == 0:
            handle.write(b"0")
            handle.flush()
        deadline = time.monotonic() + 840
        while True:
            handle.seek(0)
            try:
                if os.name == "nt":
                    import msvcrt

                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError as exc:
                if exc.errno not in (errno.EACCES, errno.EAGAIN):
                    raise PreparationError(
                        "BROWSER_RUNTIME_STORAGE_FAILED",
                        "当前数据目录不支持浏览器准备锁。",
                        str(exc),
                    ) from exc
                if time.monotonic() >= deadline:
                    raise PreparationError(
                        "BROWSER_PREPARATION_BUSY",
                        "其他实例仍在准备浏览器，请稍后重试。",
                    ) from exc
                time.sleep(0.5)
        yield
    finally:
        handle.close()


def locate_browser() -> Path:
    from playwright.sync_api import sync_playwright

    with sync_playwright() as playwright:
        return Path(playwright.chromium.executable_path).resolve()


def verify_browser(executable: Path) -> None:
    from playwright.sync_api import sync_playwright

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(
            executable_path=str(executable), headless=True, timeout=30000
        )
        try:
            page = browser.new_page()
            page.set_content("<!doctype html><title>Browser ready</title><p>ready</p>")
            if page.title() != "Browser ready":
                raise RuntimeError("Browser verification returned an unexpected page")
        finally:
            browser.close()


def prepare(root: Path, *, force: bool = False) -> Path:
    """准备与当前 Python Playwright 匹配的 Chromium 并验证实际启动。

    Args:
        root: 插件自有数据目录中的 browser-runtime 路径。
        force: 明确重试下载或启动验证失败时，重新安装不完整的缓存。

    Returns:
        已验证、位于插件浏览器缓存内的可执行文件路径。

    Raises:
        PreparationError: 下载失败、系统库不足且不能自动补齐，或启动验证失败。
    """
    with installation_lock(root):
        cache = (root / "browsers").resolve()
        emit("checking", "正在检查浏览器运行环境。")
        executable = locate_browser()
        if not executable.is_relative_to(cache):
            raise PreparationError(
                "BROWSER_RUNTIME_PATH_INVALID", "浏览器缓存路径不符合当前插件的配置。"
            )
        if force or not executable.is_file():
            emit(
                "installing_browser",
                "正在下载匹配版本的 Chromium，首次准备需要一些时间。",
            )
            try:
                subprocess.run(
                    [
                        sys.executable,
                        "-m",
                        "playwright",
                        "install",
                        "chromium",
                        "--no-shell",
                        *(["--force"] if force else []),
                    ],
                    check=True,
                    stdin=subprocess.DEVNULL,
                )
            except subprocess.CalledProcessError as exc:
                raise PreparationError(
                    "BROWSER_DOWNLOAD_FAILED",
                    "浏览器下载未完成，请检查服务器网络后点击重试。",
                ) from exc
        if not executable.is_file():
            raise PreparationError(
                "BROWSER_DOWNLOAD_FAILED", "下载结束后未找到匹配的浏览器，请重试准备。"
            )
        emit("verifying", "正在验证浏览器能否正常启动。")
        try:
            verify_browser(executable)
        except Exception as exc:
            detail = str(exc)
            missing_dependencies = any(
                value in detail.lower()
                for value in (
                    "host system is missing dependencies",
                    "error while loading shared libraries",
                    "cannot open shared object file",
                )
            )
            if not missing_dependencies:
                raise PreparationError(
                    "BROWSER_RUNTIME_VERIFY_FAILED",
                    "浏览器已下载，但未能通过启动检查。",
                    detail,
                ) from exc
            if not (
                sys.platform.startswith("linux")
                and getattr(os, "geteuid", lambda: -1)() == 0
                and shutil.which("apt-get")
            ):
                raise PreparationError(
                    "BROWSER_SYSTEM_DEPENDENCIES_REQUIRED",
                    "当前系统缺少浏览器运行库，且没有自动安装所需的权限或包管理器。",
                    detail,
                ) from exc
            emit("installing_dependencies", "正在补齐浏览器所需的系统运行库。")
            try:
                subprocess.run(
                    [sys.executable, "-m", "playwright", "install-deps", "chromium"],
                    check=True,
                    stdin=subprocess.DEVNULL,
                    env={**os.environ, "DEBIAN_FRONTEND": "noninteractive"},
                )
            except subprocess.CalledProcessError as error:
                raise PreparationError(
                    "BROWSER_SYSTEM_DEPENDENCIES_REQUIRED",
                    "系统运行库未能自动安装，请检查当前系统的包管理器和网络。",
                ) from error
            emit("verifying", "正在重新验证浏览器。")
            try:
                verify_browser(executable)
            except Exception as error:
                raise PreparationError(
                    "BROWSER_RUNTIME_VERIFY_FAILED",
                    "浏览器未能通过启动检查。",
                    str(error),
                ) from error
        emit(
            "ready",
            "浏览器已就绪。",
            executable_path=str(executable),
            playwright_version=version("playwright"),
        )
        return executable


def main() -> int:
    try:
        prepare(Path(sys.argv[1]).resolve(), force="--force" in sys.argv[2:])
        return 0
    except PreparationError as exc:
        emit("failed", exc.message, error_code=exc.code, detail=exc.detail)
    except ModuleNotFoundError as exc:
        emit(
            "failed",
            "插件的 Python 依赖不完整，请重新安装插件依赖。",
            error_code="PLAYWRIGHT_NOT_INSTALLED",
            detail=str(exc),
        )
    except Exception as exc:
        emit(
            "failed",
            "浏览器准备失败，请重试或查看具体原因。",
            error_code="BROWSER_PREPARATION_FAILED",
            detail=str(exc),
        )
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
