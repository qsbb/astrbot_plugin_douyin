"""只模拟插件使用的公开入口，便于在没有运行宿主时检查工具和生命周期。"""

import logging
import sys
from types import ModuleType, SimpleNamespace


def install():
    class Star:
        def __init__(self, context):
            self.context = context

    class Group:
        def __init__(self, name):
            self.name = name

        def command(self, name):
            def wrap(function):
                function.command_name = self.name + " " + name
                return function

            return wrap

    def llm_tool(name):
        def wrap(function):
            function.tool_name = name
            return function

        return wrap

    def permission_type(role):
        def wrap(function):
            function.required_role = role
            return function

        return wrap

    def register(*metadata):
        def wrap(cls):
            cls.registered_metadata = metadata
            return cls

        return wrap

    modules = {
        name: ModuleType(name)
        for name in (
            "astrbot",
            "astrbot.api",
            "astrbot.api.star",
            "astrbot.api.event",
            "astrbot.api.web",
        )
    }
    modules["astrbot.api"].AstrBotConfig = dict
    modules["astrbot.api"].logger = logging.getLogger("astrbot-test")
    star = modules["astrbot.api.star"]
    star.Star = Star
    star.Context = object
    star.StarTools = SimpleNamespace(get_data_dir=lambda _: None)
    star.register = register
    event = modules["astrbot.api.event"]
    event.AstrMessageEvent = object
    event.filter = SimpleNamespace(
        llm_tool=llm_tool,
        command_group=lambda name: lambda _: Group(name),
        permission_type=permission_type,
        PermissionType=SimpleNamespace(ADMIN="admin"),
    )
    web = modules["astrbot.api.web"]
    web.request = SimpleNamespace(username=None)
    web.json_response = lambda data, **kwargs: {
        "body": data,
        "status_code": 200,
        **kwargs,
    }
    web.error_response = lambda message, status_code=400: {
        "body": {"message": message},
        "status_code": status_code,
    }
    sys.modules.update(modules)
