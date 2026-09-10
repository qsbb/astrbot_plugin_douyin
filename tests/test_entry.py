import inspect
import json
import re
from dataclasses import asdict
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
import yaml
from astrbot_plugin_douyin import main
from astrbot_plugin_douyin.core.models import PLUGIN_ID, PLUGIN_NAME, PluginError
from astrbot_plugin_douyin.core.settings import Settings
from astrbot_plugin_douyin.tools import caller_from_event

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def plugin(tmp_path, monkeypatch):
    monkeypatch.setattr(
        main.StarTools, "get_data_dir", lambda _: tmp_path / "host-data"
    )
    context = SimpleNamespace(
        unregister_llm_tool=Mock(),
        get_registered_star=Mock(return_value=None),
        register_web_api=Mock(),
    )
    return main.DouyinPlugin(context, {})


def test_metadata_is_only_version_source():
    metadata = yaml.safe_load((ROOT / "metadata.yaml").read_text(encoding="utf-8"))
    assert metadata["display_name"] == PLUGIN_NAME
    assert (
        ROOT.name
        == metadata["name"]
        == main.DouyinPlugin.registered_metadata[0]
        == PLUGIN_ID
    )
    assert (
        metadata["version"]
        == main.__version__
        == main.DouyinPlugin.registered_metadata[3]
    )


def test_schema_defaults_match_validated_settings():
    schema = json.loads((ROOT / "_conf_schema.json").read_text(encoding="utf-8"))
    assert set(schema) == set(asdict(Settings()))
    defaults = {key: item["default"] for key, item in schema.items()}
    assert Settings.from_mapping(defaults) == Settings()
    assert all(
        {"type", "default", "description"} <= set(item) for item in schema.values()
    )


@pytest.mark.parametrize(
    "config",
    [
        {"enabled": "false"},
        {"action_limit_per_hour": True},
        {"allowed_origins": [1]},
        {"allowed_actions": ["delete_account"]},
        {"browser_channel": "arbitrary executable"},
    ],
)
def test_invalid_settings_fail_closed(config):
    with pytest.raises(PluginError):
        Settings.from_mapping(config)


def test_all_tool_docstrings_supply_each_parameter():
    for name in main.TOOL_NAMES:
        method = getattr(main.DouyinPlugin, name)
        signature = inspect.signature(method)
        expected = set(signature.parameters) - {"self", "event"}
        actual = set(
            re.findall(
                r"^\s+(\w+)\((?:string|number|boolean|object|array)\):",
                inspect.getdoc(method),
                re.M,
            )
        )
        assert actual == expected, name
        assert method.tool_name == name


def test_management_commands_require_admin():
    for name in ("dy_status", "dy_login", "dy_pause", "dy_resume", "dy_receipts"):
        assert getattr(main.DouyinPlugin, name).required_role == "admin"


def test_identity_is_from_host_event():
    event = SimpleNamespace(
        unified_msg_origin="real-origin",
        get_sender_id=lambda: "real-owner",
        is_admin=lambda: True,
    )
    assert caller_from_event(event).actor_id == "real-owner"


def test_init_does_not_open_browser_or_write_install_dir(plugin, tmp_path):
    assert plugin.service.browser._page is None
    assert not (tmp_path / "host-data").exists()
    assert plugin.plugin_health()["status"] == "ok"
    assert plugin.diagnostic_log_contract()["storage"] == "memory_only"
    assert plugin.webui_panel_data("status")["success"]
    assert (
        plugin.webui_panel_action("status", "send", {"role": "owner"})["success"]
        is False
    )


async def test_terminate_cleans_service_and_only_owned_tools(plugin):
    plugin.service.close = AsyncMock()
    await plugin.terminate()
    plugin.service.close.assert_awaited_once()
    assert plugin.page_api.closed
    assert [
        call.args[0] for call in plugin.context.unregister_llm_tool.call_args_list
    ] == list(main.TOOL_NAMES)


def test_plugin_registers_page_api_without_starting_browser(plugin):
    assert plugin.page_api.registered
    assert plugin.context.register_web_api.call_count == 10
    assert all(
        call.args[0].startswith(f"/{PLUGIN_ID}/page/")
        for call in plugin.context.register_web_api.call_args_list
    )
    assert plugin.service.browser._page is None


async def test_host_initialize_starts_browser_preparation_in_background(plugin):
    plugin.browser_runtime.start = Mock()
    await plugin.initialize()
    plugin.browser_runtime.start.assert_called_once_with()


async def test_invalid_mentions_never_reach_executor(plugin):
    plugin.service.execute = AsyncMock()
    result = json.loads(
        await plugin.douyin_post_comment(
            None, "123456", "评论", "intent-0001", mentions_json="{"
        )
    )
    assert result["code"] == "INVALID_ARGUMENT"
    plugin.service.execute.assert_not_awaited()
