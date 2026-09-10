# -*- coding: utf-8 -*-
"""直跑引导: python3 tests/test_xxx.py 时先加载 conftest 的路径设置。"""
import sys
from pathlib import Path

_conftest_dir = Path(__file__).resolve().parent
_plugin_dir = _conftest_dir.parent
for _p in (str(_plugin_dir), str(_plugin_dir.parent), str(_conftest_dir)):
    if _p not in sys.path:
        sys.path.insert(0, _p)
