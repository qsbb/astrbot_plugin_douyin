import asyncio
import shutil
import socket
import wave
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from astrbot_plugin_douyin.core.models import PluginError
from astrbot_plugin_douyin.core.settings import Settings
from astrbot_plugin_douyin.perception.analyze import (
    MediaAnalyzer,
    PublicResolver,
    validate_media_url,
)
from astrbot_plugin_douyin.series_diagnostics import DiagnosticBuffer


@pytest.mark.parametrize(
    "url",
    [
        "http://v.douyinvod.com/a",
        "https://127.0.0.1/a",
        "https://v.douyinvod.com.evil.example/a",
        "https://user:secret@v.douyinvod.com/a",
        "file:///etc/passwd",
        "https://v.douyinvod.com:8443/a",
    ],
)
def test_download_url_scope(url):
    with pytest.raises(PluginError) as exc:
        validate_media_url(url)
    assert exc.value.code == "MEDIA_URL_REJECTED"


async def test_resolver_rejects_private_dns_answer(monkeypatch):
    loop = asyncio.get_running_loop()
    monkeypatch.setattr(
        loop,
        "getaddrinfo",
        AsyncMock(
            return_value=[
                (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 443))
            ]
        ),
    )
    with pytest.raises(OSError, match="Non-public"):
        await PublicResolver().resolve("v.douyinvod.com", 443)


class Response:
    def __init__(self, status=200, headers=None, chunks=()):
        self.status, self.headers = status, headers or {}
        self.content_length = int(self.headers.get("Content-Length", 0)) or None
        self.chunks = chunks
        self.content = self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        return None

    async def iter_chunked(self, _):
        for chunk in self.chunks:
            yield chunk


@pytest.fixture
def analyzer(tmp_path):
    return MediaAnalyzer(
        tmp_path, Settings(media_max_mb=1), SimpleNamespace(), DiagnosticBuffer()
    )


async def test_streamed_download_enforces_limit_without_header(analyzer, tmp_path):
    response = Response(chunks=(b"a" * 600000, b"b" * 600000))
    analyzer._session = SimpleNamespace(get=lambda *_, **__: response)
    with pytest.raises(PluginError) as exc:
        await analyzer._download("https://v.douyinvod.com/a", tmp_path / "download.mp4")
    assert exc.value.code == "MEDIA_TOO_LARGE"
    assert (tmp_path / "download.mp4").stat().st_size == 600000


async def test_redirect_is_revalidated_before_second_request(analyzer, tmp_path):
    calls = []

    def get(url, **_):
        calls.append(url)
        return Response(status=302, headers={"Location": "https://127.0.0.1/private"})

    analyzer._session = SimpleNamespace(get=get)
    with pytest.raises(PluginError) as exc:
        await analyzer._download("https://v.douyinvod.com/a", tmp_path / "download.mp4")
    assert exc.value.code == "MEDIA_URL_REJECTED" and len(calls) == 1


async def test_header_size_rejected_before_writing(analyzer, tmp_path):
    analyzer._session = SimpleNamespace(
        get=lambda *_, **__: Response(headers={"Content-Length": "99999999"})
    )
    destination = tmp_path / "download.mp4"
    with pytest.raises(PluginError):
        await analyzer._download("https://v.douyinvod.com/a", destination)
    assert not destination.exists()


async def test_metadata_is_not_invented_as_transcript(analyzer):
    result = await analyzer.analyze(
        {
            "video_ref": "123456",
            "canonical_url": "https://www.douyin.com/video/123456",
            "title": "标题",
        },
        "preview",
        "",
        "umo",
    )
    assert result["platform_description"] == "标题" and result["transcript"] is None
    assert result["missing"] == ["MEDIA_URL_UNAVAILABLE"]


async def test_real_ffmpeg_audio_and_frames_cleanup(analyzer, tmp_path):
    clip = tmp_path / "fixture.mp4"
    process = await asyncio.create_subprocess_exec(
        "ffmpeg",
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-f",
        "lavfi",
        "-i",
        "color=c=blue:s=160x120:r=10",
        "-f",
        "lavfi",
        "-i",
        "sine=frequency=440:sample_rate=16000",
        "-t",
        "2",
        "-c:v",
        "libx264",
        "-pix_fmt",
        "yuv420p",
        "-c:a",
        "aac",
        str(clip),
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
    )
    await process.wait()
    assert process.returncode == 0

    async def download(_, destination, *_args):
        shutil.copyfile(clip, destination)
        return clip.stat().st_size

    async def transcribe(audio, _):
        with wave.open(str(audio)) as stream:
            assert stream.getnchannels() == 1 and stream.getframerate() == 16000
        return "测试模型占位结果，不是真实语音识别"

    async def vision(frames, times, *_):
        assert len(frames) == 4 and len(times) == 4
        assert all(frame.exists() for frame in frames)
        return "测试视觉模型结果"

    analyzer._download = download
    analyzer.host = SimpleNamespace(transcribe=transcribe, vision=vision)
    result = await analyzer.analyze(
        {
            "video_ref": "123456",
            "canonical_url": "https://www.douyin.com/video/123456",
            "duration_ms": 2000,
            "media": {"video_url": "https://v.douyinvod.com/a"},
        },
        "full",
        "",
        "umo",
    )
    assert result["status"] == "ok"
    assert 1.9 <= result["coverage"]["audio"]["end_seconds"] <= 2
    assert result["coverage"]["full_video_understood"] is False
    assert not list(analyzer.root.iterdir())


async def test_ffmpeg_cancel_kills_child(analyzer, monkeypatch):
    started = asyncio.Event()

    class Process:
        returncode = None
        killed = False

        async def wait(self):
            started.set()
            if not self.killed:
                await asyncio.Event().wait()
            self.returncode = -9

        def kill(self):
            self.killed = True

    process = Process()
    monkeypatch.setattr(
        asyncio, "create_subprocess_exec", AsyncMock(return_value=process)
    )
    task = asyncio.create_task(analyzer._ffmpeg("-version"))
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert process.killed and not analyzer._processes


def test_stale_files_count_against_media_budget(analyzer):
    analyzer.settings = replace(analyzer.settings, media_cache_mb=10)
    analyzer.root.mkdir()
    (analyzer.root / "old.mp4").write_bytes(b"x" * (3 * 1024 * 1024))
    with pytest.raises(PluginError) as exc:
        analyzer._prepare_directory()
    assert exc.value.code == "MEDIA_CACHE_FULL"
