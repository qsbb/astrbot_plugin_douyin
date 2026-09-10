"""受限下载、真实视频音轨和稀疏画面抽取；不持久化个人记忆。"""

import asyncio
import ipaddress
import socket
import tempfile
import wave
from pathlib import Path
from urllib.parse import urljoin, urlsplit

import aiohttp
from aiohttp.abc import AbstractResolver

from ..core.models import PluginError

MEDIA_DOMAINS = (
    "douyinvod.com",
    "douyin.com",
    "bytecdn.cn",
    "byteimg.com",
    "ibytedtos.com",
    "pstatp.com",
    "snssdk.com",
    "bytedance.com",
)


def validate_media_url(url: str) -> str:
    try:
        parsed = urlsplit(url)
        host = (parsed.hostname or "").lower()
        valid = (
            parsed.scheme == "https"
            and not parsed.username
            and not parsed.password
            and parsed.port in (None, 443)
        )
    except ValueError as exc:
        raise PluginError("MEDIA_URL_REJECTED", "媒体链接格式无效。") from exc
    if not valid or not any(
        host == domain or host.endswith("." + domain) for domain in MEDIA_DOMAINS
    ):
        raise PluginError("MEDIA_URL_REJECTED", "仅允许已知抖音媒体域的 HTTPS 链接。")
    return url


class PublicResolver(AbstractResolver):
    async def resolve(self, host: str, port: int = 0, family: int = socket.AF_INET):
        entries = await asyncio.get_running_loop().getaddrinfo(
            host, port, type=socket.SOCK_STREAM, family=family
        )
        rows = []
        for address_family, _, proto, _, address in entries:
            ip = ipaddress.ip_address(address[0])
            if not ip.is_global:
                raise OSError("Non-public media address rejected")
            rows.append(
                {
                    "hostname": host,
                    "host": str(ip),
                    "port": port,
                    "family": address_family,
                    "proto": proto,
                    "flags": socket.AI_NUMERICHOST,
                }
            )
        return rows

    async def close(self):
        return None


class MediaAnalyzer:
    def __init__(self, data_dir: Path, settings, host, diagnostics):
        self.root = data_dir / "media-cache"
        self.settings = settings
        self.host = host
        self.diagnostics = diagnostics
        self._lock = asyncio.Lock()
        self._session = None
        self._processes: set = set()
        self._closed = False

    def _prepare_directory(self) -> tuple[Path, int]:
        self.root.mkdir(parents=True, exist_ok=True)
        size = 0
        count = 0
        # 只检查自己的平铺媒体目录；旧崩溃文件占用预算，不静默删除未知内容。
        for directory in self.root.iterdir():
            if directory.is_symlink() or directory.is_junction():
                raise PluginError(
                    "MEDIA_CACHE_INVALID", "媒体目录含外部链接，请先检查。"
                )
            entries = directory.iterdir() if directory.is_dir() else [directory]
            for item in entries:
                count += 1
                if count > 1000 or not item.is_file() or item.is_symlink():
                    raise PluginError(
                        "MEDIA_CACHE_INVALID", "媒体目录结构或文件数量不受支持。"
                    )
                size += item.stat().st_size
        free = self.settings.media_cache_mb * 1024 * 1024 - size
        if free < 8 * 1024 * 1024:
            raise PluginError(
                "MEDIA_CACHE_FULL", "媒体临时空间不足，请检查旧任务残留文件。"
            )
        return Path(tempfile.mkdtemp(prefix="analysis-", dir=self.root)), free

    @staticmethod
    def _remove_directory(directory: Path):
        # 不递归删除、不跟随链接，只移除本任务创建的平铺文件。
        for child in directory.iterdir():
            if child.is_file() or child.is_symlink():
                child.unlink(missing_ok=True)
        directory.rmdir()

    async def _download(
        self, url: str, destination: Path, free_bytes: int | None = None
    ) -> int:
        if self._session is None:
            self._session = aiohttp.ClientSession(
                connector=aiohttp.TCPConnector(
                    resolver=PublicResolver(), limit=2, ttl_dns_cache=30
                ),
                timeout=aiohttp.ClientTimeout(total=90, connect=15, sock_read=20),
                trust_env=False,
            )
        maximum = (
            min(self.settings.media_max_mb, self.settings.media_cache_mb // 2)
            * 1024
            * 1024
        )
        if free_bytes is not None:
            maximum = min(maximum, free_bytes // 2)
        for _ in range(5):
            validate_media_url(url)
            async with self._session.get(
                url,
                allow_redirects=False,
                headers={"Referer": "https://www.douyin.com/"},
            ) as response:
                if response.status in {301, 302, 303, 307, 308}:
                    location = response.headers.get("Location")
                    if not location:
                        raise PluginError(
                            "MEDIA_DOWNLOAD_FAILED", "媒体重定向缺少地址。"
                        )
                    url = urljoin(url, location)
                    continue
                if response.status != 200:
                    raise PluginError(
                        "MEDIA_DOWNLOAD_FAILED",
                        "媒体下载失败。",
                        {"http_status": response.status},
                    )
                if response.content_length and response.content_length > maximum:
                    raise PluginError("MEDIA_TOO_LARGE", "视频文件超过本次下载上限。")
                size = 0
                with destination.open("wb") as handle:
                    async for chunk in response.content.iter_chunked(65536):
                        size += len(chunk)
                        if size > maximum:
                            raise PluginError(
                                "MEDIA_TOO_LARGE", "视频数据超过本次下载上限。"
                            )
                        await asyncio.to_thread(handle.write, chunk)
                if size == 0:
                    raise PluginError("MEDIA_EMPTY", "媒体文件为空。")
                return size
        raise PluginError("MEDIA_REDIRECT_LIMIT", "媒体链接重定向次数超过上限。")

    async def _ffmpeg(self, *arguments: str) -> bool:
        try:
            process = await asyncio.create_subprocess_exec(
                self.settings.ffmpeg_path,
                "-hide_banner",
                "-loglevel",
                "error",
                "-nostdin",
                "-y",
                *arguments,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
        except FileNotFoundError as exc:
            raise PluginError(
                "FFMPEG_UNAVAILABLE", "没有找到 FFmpeg，请配置 ffmpeg_path。"
            ) from exc
        self._processes.add(process)
        try:
            async with asyncio.timeout(60):
                await process.wait()
            return process.returncode == 0
        finally:
            if process.returncode is None:
                process.kill()
                await process.wait()
            self._processes.discard(process)

    async def analyze(self, video: dict, depth: str, question: str, umo: str) -> dict:
        """提取有限音轨和采样画面，调用宿主 Provider 生成客观观察。

        Args:
            video: 含作品标识、公开链接和内部媒体地址的已解析视频。
            depth: preview 处理前最多 20 秒，full 处理至配置的时长上限。
            question: 本次客观观察的问题，不改变宿主人格。
            umo: 用于选择宿主模型的统一消息来源。

        Returns:
            douyin.observation.v1 数据，包含转录、画面描述、覆盖范围和
            缺失能力；不把采样结果声明为完整视频理解。

        Raises:
            PluginError: 服务已停止、媒体来源无效或下载/空间限制不满足。
            asyncio.CancelledError: 任务被取消；临时媒体仍会进入清理流程。
        """
        if self._closed:
            raise PluginError("SERVICE_CLOSED", "内容感知模块已停止。")
        result = {
            "schema_version": "douyin.observation.v1",
            "status": "partial",
            "video_ref": video["video_ref"],
            "canonical_url": video["canonical_url"],
            "platform_description": video.get("title", ""),
            "transcript": None,
            "visual_description": None,
            "coverage": {
                "audio": None,
                "frame_times_seconds": [],
                "full_video_understood": False,
            },
            "missing": [],
            "fact_verification": "not_verified",
        }
        media_url = video.get("media", {}).get("video_url", "")
        if not media_url:
            result["missing"].append("MEDIA_URL_UNAVAILABLE")
            return result
        async with self._lock:
            directory, free_bytes = await asyncio.to_thread(self._prepare_directory)
            try:
                source = directory / "source.mp4"
                downloaded = await self._download(media_url, source, free_bytes)
                duration = max(0, float(video.get("duration_ms") or 0) / 1000)
                limit = min(
                    self.settings.media_max_seconds,
                    20 if depth == "preview" else self.settings.media_max_seconds,
                )
                seconds = min(duration, limit) if duration else limit
                # 给四张有尺寸上限的 JPEG 预留 4 MB，余量决定 PCM 长度。
                seconds = min(
                    seconds, max(1, (free_bytes - downloaded - 4 * 1024 * 1024) / 32000)
                )
                audio = directory / "audio.wav"
                # 只从实际视频抽取音轨；不使用作品 music/BGM 字段。
                extracted = await self._ffmpeg(
                    "-protocol_whitelist",
                    "file,pipe",
                    "-f",
                    "mp4",
                    "-i",
                    str(source),
                    "-t",
                    str(seconds),
                    "-vn",
                    "-ac",
                    "1",
                    "-ar",
                    "16000",
                    "-f",
                    "wav",
                    str(audio),
                )
                if extracted and audio.exists() and audio.stat().st_size > 44:
                    try:
                        result["transcript"] = await self.host.transcribe(audio, umo)
                        with wave.open(str(audio), "rb") as stream:
                            actual_seconds = stream.getnframes() / stream.getframerate()
                        result["coverage"]["audio"] = {
                            "start_seconds": 0,
                            "end_seconds": round(min(seconds, actual_seconds), 2),
                            "timestamps_available": False,
                        }
                    except Exception as exc:
                        code = (
                            exc.code if isinstance(exc, PluginError) else "STT_FAILED"
                        )
                        result["missing"].append(code)
                        self.diagnostics.emit(
                            "WARNING",
                            code,
                            "Speech extraction unavailable",
                            {"error": str(exc)},
                        )
                else:
                    result["missing"].append("AUDIO_UNAVAILABLE")
                times = [
                    round(seconds * fraction, 2) for fraction in (0.05, 0.35, 0.65, 0.9)
                ]
                frames, sampled = [], []
                for index, timestamp in enumerate(times):
                    frame = directory / f"frame-{index}.jpg"
                    ok = await self._ffmpeg(
                        "-protocol_whitelist",
                        "file,pipe",
                        "-f",
                        "mp4",
                        "-ss",
                        str(timestamp),
                        "-i",
                        str(source),
                        "-frames:v",
                        "1",
                        "-vf",
                        "scale=640:640:force_original_aspect_ratio=decrease",
                        "-q:v",
                        "4",
                        str(frame),
                    )
                    if ok and frame.exists() and frame.stat().st_size:
                        frames.append(frame)
                        sampled.append(timestamp)
                result["coverage"]["frame_times_seconds"] = sampled
                if frames:
                    try:
                        result["visual_description"] = await self.host.vision(
                            frames, sampled, question, umo
                        )
                    except Exception as exc:
                        code = (
                            exc.code
                            if isinstance(exc, PluginError)
                            else "VISION_FAILED"
                        )
                        result["missing"].append(code)
                        self.diagnostics.emit(
                            "WARNING",
                            code,
                            "Visual extraction unavailable",
                            {"error": str(exc)},
                        )
                else:
                    result["missing"].append("FRAMES_UNAVAILABLE")
                if not duration or duration > seconds:
                    result["missing"].append("VIDEO_PARTIALLY_SAMPLED")
                # 稀疏采样不能声称理解整段视频；complete 只表示请求的提取步骤完成。
                result["status"] = "partial" if result["missing"] else "ok"
                return result
            except PluginError as exc:
                result["missing"].append(exc.code)
                return result
            finally:
                await asyncio.to_thread(self._remove_directory, directory)

    async def close(self):
        self._closed = True
        for process in list(self._processes):
            if process.returncode is None:
                process.kill()
                await process.wait()
        if self._session is not None:
            await self._session.close()
