import asyncio
import importlib.util
import json
import os
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from astrbot_plugin_douyin import page_api
from astrbot_plugin_douyin.core.models import Caller, DashboardCaller, PluginError
from astrbot_plugin_douyin.core.service import DouyinService
from astrbot_plugin_douyin.core.settings import Settings
from astrbot_plugin_douyin.series_diagnostics import DiagnosticBuffer

ADMIN = DashboardCaller.from_username("admin")
OTHER = DashboardCaller.from_username("other")
BOT = Caller("qq:FriendMessage:owner", "owner", True)


@pytest.fixture
def service(tmp_path):
    settings = Settings(
        enabled=True,
        allowed_origins=(BOT.umo,),
        allowed_actor_ids=(BOT.actor_id,),
        allowed_actions=("set_like",),
        expected_account_ref="user:12345",
    )
    browser = SimpleNamespace(
        status=AsyncMock(
            return_value={
                "authenticated": True,
                "account_ref": "user:12345",
                "browser_started": True,
            }
        ),
        remote_frame=AsyncMock(
            return_value={
                "image": "data:image/jpeg;base64,abcd",
                "frame_id": "frame-1",
                "width": 1280,
                "height": 900,
            }
        ),
        remote_input=AsyncMock(
            return_value={"status": "submitted", "code": "MANUAL_INPUT_DISPATCHED"}
        ),
        remote_navigate=AsyncMock(return_value={"status": "ok"}),
        browse=AsyncMock(return_value={"videos": []}),
        set_like=AsyncMock(return_value={"status": "verified"}),
        start_login=AsyncMock(),
        close=AsyncMock(),
    )
    return DouyinService(
        tmp_path,
        settings,
        browser,
        SimpleNamespace(close=AsyncMock()),
        DiagnosticBuffer(),
    )


class Config(dict):
    async def save_config_async(self, changes):
        self.update(changes)
        return True


@pytest.fixture
def api(service, monkeypatch):
    monkeypatch.setattr(
        page_api,
        "request",
        SimpleNamespace(
            username="admin",
            body=AsyncMock(return_value=b"{}"),
            json=AsyncMock(return_value={}),
        ),
    )
    result = page_api.PageApi(
        SimpleNamespace(register_web_api=Mock()), Config(), service
    )
    result.register()
    return result


def payload(api, data):
    page_api.request.json.return_value = data
    page_api.request.body.return_value = json.dumps(data).encode()


@pytest.mark.parametrize("username", [None, "", " ", "api_key:123", 123])
def test_dashboard_identity_fails_closed(username):
    with pytest.raises(PluginError):
        DashboardCaller.from_username(username)


async def test_plugin_api_key_and_forged_identity_cannot_control(api):
    page_api.request.username = "api_key:123"
    payload(api, {"action": "acquire", "is_admin": True, "username": "admin"})
    response = await api.handle("control")
    assert response["status_code"] == 403
    assert not api.service._control_active()


async def test_chat_admin_cannot_call_page_control(service):
    result = await service.page_execute(BOT, "control", {"action": "acquire"})
    assert result["code"] == "ROLE_FORBIDDEN"


async def test_page_shares_browser_with_bot_and_releases_lock(service):
    assert (await service.page_execute(ADMIN, "control", {"action": "acquire"}))[
        "status"
    ] == "ok"
    assert (await service.execute("browse", BOT))["code"] == "ACCOUNT_CONTROLLED"
    assert (await service.execute("browse", ADMIN))["code"] == "ACCOUNT_CONTROLLED"
    assert (await service.login(BOT))["code"] == "ACCOUNT_CONTROLLED"
    service.browser.browse.assert_not_awaited()
    frame = await service.page_execute(ADMIN, "frame")
    assert frame["data"]["image"].startswith("data:image/jpeg")
    await service.page_execute(ADMIN, "control", {"action": "release"})
    assert (await service.execute("browse", BOT))["status"] == "ok"
    service.browser.browse.assert_awaited_once()


async def test_page_takeover_waits_for_bot_inflight(service):
    started, finish = asyncio.Event(), asyncio.Event()

    async def browse(**kwargs):
        started.set()
        await finish.wait()
        return {"videos": []}

    service.browser.browse.side_effect = browse
    task = asyncio.create_task(service.execute("browse", BOT))
    await started.wait()
    control = asyncio.create_task(
        service.page_execute(ADMIN, "control", {"action": "acquire"})
    )
    await asyncio.sleep(0)
    assert not control.done()
    finish.set()
    await task
    assert (await control)["data"]["control"]["owned"]


async def test_expired_owner_does_not_block_bot(service):
    await service.page_execute(ADMIN, "control", {"action": "acquire"})
    service._page_deadline = 0
    assert (await service.execute("browse", BOT))["status"] == "ok"
    assert (await service.page_execute(ADMIN, "frame"))["code"] == "CONTROL_REQUIRED"


@pytest.mark.parametrize(
    "operation,params",
    [
        ("frame", {}),
        ("input", {"kind": "text", "frame_id": "frame-1", "text": "secret"}),
        ("control", {"action": "release"}),
        ("control", {"action": "login"}),
    ],
)
async def test_other_admin_cannot_use_or_release_lease(service, operation, params):
    await service.page_execute(ADMIN, "control", {"action": "acquire"})
    result = await service.page_execute(OTHER, operation, params)
    assert result["code"] == "CONTROL_REQUIRED"
    service.browser.remote_input.assert_not_awaited()


async def test_manual_input_is_not_business_receipt_or_diagnostic_text(service):
    await service.page_execute(ADMIN, "control", {"action": "acquire"})
    service.browser.remote_input.side_effect = RuntimeError(
        "input secret-verification-code"
    )
    result = await service.page_execute(
        ADMIN,
        "input",
        {"kind": "text", "frame_id": "1", "text": "secret-verification-code"},
    )
    assert result["code"] == "PAGE_OPERATION_FAILED"
    assert "secret-verification-code" not in json.dumps(service.diagnostics.events())
    assert not service.store.state["actions"]


async def test_dashboard_operations_keep_action_and_account_gates(service):
    service.settings = replace(
        service.settings, allowed_origins=(), allowed_actor_ids=(), allowed_actions=()
    )
    assert (await service.execute("browse", ADMIN))["status"] == "ok"
    result = await service.execute(
        "set_like", ADMIN, video_ref="123456", liked=True, request_id="page-test-001"
    )
    assert result["code"] == "ACTION_NOT_GRANTED"
    service.settings = replace(
        service.settings,
        allowed_actions=("set_like",),
        expected_account_ref="user:other",
    )
    result = await service.execute(
        "set_like", ADMIN, video_ref="123456", liked=True, request_id="page-test-002"
    )
    assert result["code"] == "ACCOUNT_MISMATCH"
    service.browser.set_like.assert_not_awaited()


async def test_binding_reads_real_browser_identity_and_saves(api):
    api.service.browser.status.return_value["account_ref"] = "user:new"
    result = (await api.handle("bind"))["body"]["result"]
    assert result["status"] == "ok"
    assert (
        api.config["expected_account_ref"]
        == api.service.settings.expected_account_ref
        == "user:new"
    )
    payload(api, {"account_ref": "fake"})
    assert (await api.handle("bind"))["body"]["result"]["code"] == "INVALID_ARGUMENT"


async def test_bind_requires_logged_in_browser(api):
    api.service.browser.status.return_value = {"authenticated": False}
    result = (await api.handle("bind"))["body"]["result"]
    assert result["code"] == "LOGIN_REQUIRED"
    assert not api.config


async def test_invalid_config_does_not_mutate_runtime(api):
    payload(api, {"config": {"allowed_actions": ["delete_account"]}})
    result = (await api.handle("settings"))["body"]["result"]
    assert result["code"] == "CONFIG_INVALID"
    assert api.service.settings.allowed_actions == ("set_like",)
    assert not api.config


async def test_config_save_failure_rolls_back(api):
    async def fail(changes):
        api.config.update(changes)
        raise OSError("not writable")

    api.config.save_config_async = fail
    payload(api, {"config": {"enabled": False}})
    assert (await api.handle("settings"))["body"]["result"]["status"] == "failed"
    assert api.service.settings.enabled
    assert "enabled" not in api.config


async def test_cancelled_save_finishes_disk_and_runtime_together(service):
    started, finish = asyncio.Event(), asyncio.Event()
    saved = []

    async def save(changes, settings):
        started.set()
        await finish.wait()
        saved.append(changes)

    task = asyncio.create_task(
        service.page_execute(
            ADMIN, "settings", {"config": {"enabled": False}}, save_config=save
        )
    )
    await started.wait()
    task.cancel()
    finish.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert saved == [{"enabled": False}]
    assert not service.settings.enabled


async def test_page_writes_require_client_request_id_and_preserve_envelope(api):
    payload(
        api, {"operation": "set_like", "params": {"video_ref": "123456", "liked": True}}
    )
    assert (await api.handle("action"))["body"]["result"][
        "code"
    ] == "REQUEST_ID_REQUIRED"
    payload(
        api,
        {
            "operation": "set_like",
            "params": {"video_ref": "123456", "liked": True},
            "request_id": "page-test-003",
        },
    )
    first = (await api.handle("action"))["body"]["result"]
    second = (await api.handle("action"))["body"]["result"]
    assert first["status"] == "verified" and first["request_id"] == "page-test-003"
    assert second["data"]["replayed"]
    api.service.browser.set_like.assert_awaited_once()


async def test_page_routes_cache_and_closed_lifecycle(api):
    calls = api.context.register_web_api.call_args_list
    assert len(calls) == 9 and len({c.args[0] for c in calls}) == 9
    result = await api.handle("status")
    assert result["headers"]["Cache-Control"] == "no-store"
    assert result["body"]["result"]["data"]["config_writable"]
    api.close()
    for call in calls:
        assert (await call.args[1]())["status_code"] == 503


async def test_large_payload_rejected_before_json(api):
    page_api.request.body.return_value = b"x" * 16385
    result = await api.handle("input")
    assert result["status_code"] == 413
    page_api.request.json.assert_not_awaited()


@pytest.mark.parametrize(
    "data",
    [
        [],
        {"operation": "browse", "params": {}, "is_admin": True},
        {"operation": "browse", "params": {"request_id": "fake"}},
        {"operation": "control", "params": {}},
    ],
)
async def test_frontend_identity_and_unknown_actions_rejected(api, data):
    payload(api, data)
    assert (await api.handle("action"))["status_code"] == 400


@pytest.mark.parametrize(
    "username,expected_status", [("admin", 200), (None, 403), ("api_key:123", 403)]
)
async def test_with_actual_astrbot_public_web_module(
    api, monkeypatch, username, expected_status
):
    source = os.environ.get("ASTRBOT_PUBLIC_WEB_SOURCE")
    if not source:
        pytest.skip(
            "Set ASTRBOT_PUBLIC_WEB_SOURCE to an installed host astrbot/api/web.py"
        )
    from starlette.requests import Request

    spec = importlib.util.spec_from_file_location("douyin_actual_host_web", source)
    host_web = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(host_web)
    for name in ("request", "json_response", "error_response"):
        monkeypatch.setattr(page_api, name, getattr(host_web, name))
    request = Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/api/v1/plugins/extensions/astrbot_plugin_douyin/page/status",
            "headers": [],
            "query_string": b"",
        }
    )
    with host_web.bind_request_context(
        host_web.PluginRequest(request, username=username)
    ):
        response = await api.handle("status")
    assert response.status_code == expected_status
    data = json.loads(response.body)
    if expected_status == 200:
        assert data["result"]["schema_version"] == "douyin.result.v1"
        assert response.headers["Cache-Control"] == "no-store"
