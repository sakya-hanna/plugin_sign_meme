"""测试路径引导: 与 meme_manager/tests/conftest.py 同款约定。

- PLUGIN_DIR 加入 sys.path: 测试以顶层模块方式导入 backend 包
  (from backend.service import ...), 与插件在 AstrBot 内以
  astrbot_plugin_sign_meme 包加载时的相对导入等价;
- PLUGIN_PARENT 加入 sys.path: 允许跨插件导入
  astrbot_plugin_meme_manager(semantic_pool 集成场景)。
"""
import sys
from pathlib import Path

PLUGIN_DIR = Path(__file__).resolve().parents[1]
if str(PLUGIN_DIR) not in sys.path:
    sys.path.insert(0, str(PLUGIN_DIR))
PLUGIN_PARENT = PLUGIN_DIR.parent
if str(PLUGIN_PARENT) not in sys.path:
    sys.path.insert(0, str(PLUGIN_PARENT))
