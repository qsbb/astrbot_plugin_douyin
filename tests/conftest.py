"""离线测试按宿主包导入规则加载插件，不启动真实 AstrBot。"""

import importlib.util
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "astrbot_plugin_douyin",
    ROOT / "__init__.py",
    submodule_search_locations=[str(ROOT)],
)
MODULE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)

STUB_SPEC = importlib.util.spec_from_file_location(
    "douyin_test_stubs", ROOT / "tests" / "astrbot_stubs.py"
)
STUB = importlib.util.module_from_spec(STUB_SPEC)
STUB_SPEC.loader.exec_module(STUB)
STUB.install()
