import asyncio
import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from astrbot_plugin_douyin.core.models import Caller, PluginError
from astrbot_plugin_douyin.core.service import DouyinService
from astrbot_plugin_douyin.core.settings import Settings
from astrbot_plugin_douyin.core.store import StateStore
from astrbot_plugin_douyin.series_diagnostics import DiagnosticBuffer

OWNER = Caller("test:FriendMessage:owner", "owner", True)
VIDEO = {
    "video_ref": "123456",
    "canonical_url": "https://www.douyin.com/video/123456",
    "title": "标题不是逐字稿",
    "media": {"video_url": "https://v.douyinvod.com/a?token=secret-video"},
}


@pytest.fixture
def service(tmp_path):
    settings = Settings(
        enabled=True,
        allowed_origins=(OWNER.umo,),
        allowed_actor_ids=(OWNER.actor_id,),
        allowed_actions=("set_like", "post_comment", "share_video", "send_message"),
        expected_account_ref="user:12345",
        allowed_target_refs=("user:22222", "conversation:33333"),
        action_cooldown_seconds=1,
    )
    browser = SimpleNamespace(
        status=AsyncMock(
            return_value={"authenticated": True, "account_ref": "user:12345"}
        ),
        set_like=AsyncMock(
            return_value={"status": "verified", "code": "LIKE_VERIFIED"}
        ),
        post_comment=AsyncMock(
            return_value={"status": "submitted", "comment_ref": "54321"}
        ),
        share_video=AsyncMock(return_value={"status": "submitted"}),
        send_message=AsyncMock(return_value={"status": "verified"}),
        watch=AsyncMock(return_value=VIDEO),
        browse=AsyncMock(return_value={"videos": [VIDEO]}),
        search=AsyncMock(return_value={"videos": [VIDEO]}),
        close=AsyncMock(),
        start_login=AsyncMock(return_value={"status": "ok"}),
    )
    analyzer = SimpleNamespace(
        analyze=AsyncMock(
            return_value={
                "status": "partial",
                "transcript": "真实音轨",
                "missing": ["VISION_UNAVAILABLE"],
            }
        ),
        close=AsyncMock(),
    )
    return DouyinService(tmp_path, settings, browser, analyzer, DiagnosticBuffer())


async def test_write_idempotency_survives_restart(service, tmp_path):
    args = dict(video_ref="123456", liked=True, request_id="intent-0001")
    first = await service.execute("set_like", OWNER, **args)
    second = await service.execute("set_like", OWNER, **args)
    restored = DouyinService(
        tmp_path,
        service.settings,
        service.browser,
        service.analyzer,
        service.diagnostics,
    )
    third = await restored.execute("set_like", OWNER, **args)
    assert first["status"] == "verified"
    assert second["data"]["replayed"] and third["data"]["replayed"]
    assert service.browser.set_like.await_count == 1


async def test_unknown_action_is_never_resent(service):
    service.browser.post_comment.side_effect = TimeoutError()
    params = dict(video_ref="123456", text="评论", request_id="intent-0002")
    first = await service.execute("post_comment", OWNER, **params)
    second = await service.execute("post_comment", OWNER, **params)
    assert first["status"] == second["status"] == "unknown_result"
    assert service.browser.post_comment.await_count == 1


async def test_request_id_cannot_change_payload(service):
    await service.execute(
        "set_like", OWNER, video_ref="123456", liked=True, request_id="intent-0003"
    )
    result = await service.execute(
        "set_like", OWNER, video_ref="123456", liked=False, request_id="intent-0003"
    )
    assert result["code"] == "REQUEST_ID_CONFLICT"
    assert service.browser.set_like.await_count == 1


@pytest.mark.parametrize(
    "changes,code",
    [
        ({"allowed_origins": ()}, "ROLE_FORBIDDEN"),
        ({"allowed_actor_ids": ()}, "ROLE_FORBIDDEN"),
        ({"allowed_actions": ()}, "ACTION_NOT_GRANTED"),
        ({"expected_account_ref": ""}, "ACCOUNT_NOT_BOUND"),
        ({"enabled": False}, "PLUGIN_DISABLED"),
    ],
)
async def test_write_gate_before_browser(service, changes, code):
    service.settings = replace(service.settings, **changes)
    result = await service.execute(
        "set_like", OWNER, video_ref="123456", liked=True, request_id="intent-gate"
    )
    assert result["code"] == code
    service.browser.set_like.assert_not_awaited()


async def test_non_admin_cannot_write_even_if_listed(service):
    result = await service.execute(
        "set_like",
        replace(OWNER, is_admin=False),
        video_ref="123456",
        liked=True,
        request_id="intent-user",
    )
    assert result["code"] == "ROLE_FORBIDDEN"
    service.browser.set_like.assert_not_awaited()


async def test_account_switch_denied(service):
    service.browser.status.return_value["account_ref"] = "user:other"
    result = await service.execute(
        "set_like", OWNER, video_ref="123456", liked=True, request_id="intent-acct"
    )
    assert result["code"] == "ACCOUNT_MISMATCH"
    service.browser.set_like.assert_not_awaited()


@pytest.mark.parametrize(
    "operation,params",
    [
        ("share_video", {"video_ref": "123456", "target_ref": "user:stranger"}),
        ("send_message", {"conversation_ref": "conversation:other", "text": "消息"}),
        (
            "post_comment",
            {
                "video_ref": "123456",
                "text": "评论",
                "mentions": [{"target_ref": "user:other"}],
            },
        ),
    ],
)
async def test_recipient_scope_cannot_be_overridden(service, operation, params):
    result = await service.execute(
        operation, OWNER, request_id="intent-target", **params
    )
    assert result["code"] == "TARGET_NOT_GRANTED"
    getattr(service.browser, operation).assert_not_awaited()


async def test_rate_limit_counts_unknown_results(service):
    service.settings = replace(service.settings, action_limit_per_hour=1)
    service.browser.set_like.side_effect = TimeoutError()
    await service.execute(
        "set_like", OWNER, video_ref="123456", liked=True, request_id="intent-limit1"
    )
    result = await service.execute(
        "set_like", OWNER, video_ref="654321", liked=True, request_id="intent-limit2"
    )
    assert result["code"] == "RATE_LIMITED"


async def test_queue_rechecks_pause_before_side_effect(service):
    await service._operation_lock.acquire()
    call = asyncio.create_task(
        service.execute(
            "set_like",
            OWNER,
            video_ref="123456",
            liked=True,
            request_id="intent-paused",
        )
    )
    await asyncio.sleep(0)
    await service.set_paused(OWNER, True)
    service._operation_lock.release()
    result = await call
    assert result["code"] == "ACCOUNT_PAUSED"
    service.browser.set_like.assert_not_awaited()


async def test_task_bound_to_origin_and_sender(service):
    pending = asyncio.Event()

    async def analyze(*_):
        await pending.wait()
        return {"status": "ok"}

    service.analyzer.analyze.side_effect = analyze
    started = await service.execute("watch", OWNER, video_ref="123456", depth="preview")
    task_id = started["data"]["task_id"]
    second = Caller("other-origin", "owner", True)
    service.settings = replace(
        service.settings, allowed_origins=(OWNER.umo, second.umo)
    )
    denied = await service.execute("task", second, task_id=task_id)
    assert denied["code"] == "TASK_NOT_FOUND"
    cancelled = await service.execute("task", OWNER, task_id=task_id, cancel=True)
    assert cancelled["status"] == "cancelled"
    assert "secret-video" not in json.dumps(started)


async def test_metadata_does_not_run_models(service):
    result = await service.execute("watch", OWNER, video_ref="123456", depth="metadata")
    assert result["data"]["title"] == VIDEO["title"]
    assert "media" not in result["data"]
    service.analyzer.analyze.assert_not_awaited()


@pytest.mark.parametrize(
    "operation,params,code",
    [
        ("set_like", {"video_ref": "123456", "liked": True}, "REQUEST_ID_REQUIRED"),
        (
            "set_like",
            {"video_ref": "123456", "liked": "false", "request_id": "intent-bool"},
            "INVALID_ARGUMENT",
        ),
        ("browse", {"limit": True}, "INVALID_ARGUMENT"),
        ("browse", {"limit": 100}, "INVALID_ARGUMENT"),
        ("watch", {"video_ref": "123456", "depth": "invented"}, "INVALID_ARGUMENT"),
        ("search", {"query": "词", "account_ref": "evil"}, "INVALID_ARGUMENT"),
    ],
)
async def test_invalid_tool_inputs(service, operation, params, code):
    assert (await service.execute(operation, OWNER, **params))["code"] == code


async def test_shutdown_cleans_both_resources(service):
    service.browser.close.side_effect = RuntimeError("test failure")
    await service.close()
    service.analyzer.close.assert_awaited_once()
    assert service.snapshot()["closed"]


def test_invalid_state_is_not_silently_reset(tmp_path):
    (tmp_path / "runtime.json").write_text('{"schema_version": 999}', encoding="utf-8")
    with pytest.raises(PluginError, match="运行状态"):
        StateStore(tmp_path)


async def test_pending_after_restart_becomes_unknown(tmp_path):
    store = StateStore(tmp_path)
    await store.update(
        lambda state: state["actions"].update(
            {
                "key": {
                    "fingerprint": "hash",
                    "created_at": datetime.now(UTC).isoformat(),
                    "receipt": {"status": "pending"},
                }
            }
        )
    )
    assert (
        StateStore(tmp_path).state["actions"]["key"]["receipt"]["status"]
        == "unknown_result"
    )


async def test_old_unknown_receipt_is_retained(tmp_path):
    store = StateStore(tmp_path)
    await store.update(
        lambda state: state["actions"].update(
            {
                "old": {
                    "fingerprint": "hash",
                    "created_at": (datetime.now(UTC) - timedelta(days=30)).isoformat(),
                    "receipt": {"status": "unknown_result"},
                }
            }
        )
    )
    assert "old" in store.state["actions"]


async def test_shutdown_cancels_active_write_and_keeps_unknown_receipt(service):
    started = asyncio.Event()

    async def delayed(**_):
        started.set()
        await asyncio.Event().wait()

    service.browser.set_like.side_effect = delayed
    call = asyncio.create_task(
        service.execute(
            "set_like",
            OWNER,
            video_ref="123456",
            liked=True,
            request_id="intent-shutdown",
        )
    )
    await started.wait()
    await service.close()
    result = await call
    assert result["status"] == "unknown_result"
    assert not service._executions
    assert (
        next(iter(service.store.state["actions"].values()))["receipt"]["status"]
        == "unknown_result"
    )


async def test_nonfinite_playback_data_remains_json_serializable(service):
    service.browser.browse.return_value = {"duration": float("inf")}
    result = await service.execute("browse", OWNER)
    assert result["data"]["duration"] is None
    json.dumps(result, allow_nan=False)


async def test_invalid_request_id_is_not_echoed_as_secret(service):
    result = await service.execute("status", OWNER, request_id="password=leak-me")
    assert result["code"] == "INVALID_REQUEST_ID"
    assert "leak-me" not in json.dumps(result)


async def test_receipt_lookup_works_while_paused_and_never_resends(service):
    await service.execute(
        "set_like", OWNER, video_ref="123456", liked=True, request_id="intent-query"
    )
    await service.set_paused(OWNER, True)
    result = await service.execute("receipt", OWNER, request_id="intent-query")
    assert result["status"] == "verified"
    assert result["data"]["video_ref"] == "123456"
    assert service.browser.set_like.await_count == 1


async def test_numeric_and_canonical_video_share_one_idempotency_key(service):
    await service.execute(
        "set_like", OWNER, video_ref="123456", liked=True, request_id="intent-normal"
    )
    result = await service.execute(
        "set_like",
        OWNER,
        video_ref="https://www.douyin.com/video/123456",
        liked=True,
        request_id="intent-normal",
    )
    assert result["data"]["replayed"] is True
    assert service.browser.set_like.await_count == 1


async def test_admin_can_login_before_plugin_enabled(service):
    service.settings = replace(service.settings, enabled=False)
    result = await service.login(OWNER)
    assert result["status"] == "ok"
    service.browser.start_login.assert_awaited_once()


async def test_pause_interrupts_inflight_write_and_preserves_uncertainty(service):
    started = asyncio.Event()

    async def delayed(**_):
        started.set()
        await asyncio.Event().wait()

    service.browser.post_comment.side_effect = delayed
    call = asyncio.create_task(
        service.execute(
            "post_comment",
            OWNER,
            video_ref="123456",
            text="评论",
            request_id="intent-pause-live",
        )
    )
    await started.wait()
    await service.set_paused(OWNER, True)
    assert (await call)["status"] == "unknown_result"
    assert (await service.execute("receipt", OWNER, request_id="intent-pause-live"))[
        "status"
    ] == "unknown_result"
    assert service.browser.post_comment.await_count == 1
