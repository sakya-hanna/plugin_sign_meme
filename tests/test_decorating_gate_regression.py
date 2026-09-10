"""回归: 口子D兜底清空不得误伤普通表情链路 (2026-09-10 18:24 修复)。

场景: integrated 模式下模型选择普通表情(非举牌)。
sign_meme on_decorating(100000) 先于官方 on_decorating(99999) 执行。
修复前: 无条件清空 meme_manager_semantic_selected_ids → 官方 decorating 读不到 → 普通表情静默丢失。
修复后: 仅当本轮存在举牌渲染成品(EXTRA_RENDERED_PATH)时才清空。
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from backend.integrated_events import (  # noqa: E402
    EXTRA_RENDERED_PATH,
    UPSTREAM_DEFAULT_ID,
    UPSTREAM_SELECTED_IDS,
    on_decorating_result_first,
)


class _Event:
    def __init__(self, extras=None):
        self._extras = dict(extras or {})

    def get_extra(self, key):
        return self._extras.get(key)

    def set_extra(self, key, value):
        self._extras[key] = value


class _Plugin:
    sign_mode = "integrated"


def _run(event):
    import asyncio

    asyncio.run(on_decorating_result_first(_Plugin(), event))
    return event


def test_no_sign_render_keeps_official_selected_ids():
    """普通表情轮: selected_ids 必须原样保留给官方 decorating。"""
    event = _Event(
        {
            UPSTREAM_SELECTED_IDS: ["meme:c27ddc489838"],
            UPSTREAM_DEFAULT_ID: "meme:c27ddc489838",
            EXTRA_RENDERED_PATH: "",
        }
    )
    _run(event)
    assert event.get_extra(UPSTREAM_SELECTED_IDS) == ["meme:c27ddc489838"], (
        "普通表情 selected_ids 被口子D误清空(回归 18:24 bug)"
    )
    assert event.get_extra(UPSTREAM_DEFAULT_ID) == "meme:c27ddc489838"
    print("passed: no_sign_render_keeps_official_selected_ids")


def test_sign_render_still_clears_and_appends():
    """举牌轮: 仍要兜底清空并追加成品图。"""
    event = _Event(
        {
            UPSTREAM_SELECTED_IDS: ["meme:a2bd8f1b47a4"],
            UPSTREAM_DEFAULT_ID: "",
            EXTRA_RENDERED_PATH: "/tmp/fake-render.png",
            EXTRA_RENDERED_PATH + "_rid": "rid-1",
        }
    )
    # 无真实 result chain, 追加段会走"无可附加的结果链"警告路径,
    # 但清空行为必须已发生。
    _run(event)
    assert event.get_extra(UPSTREAM_SELECTED_IDS) is None, (
        "举牌轮 selected_ids 应被兜底清空"
    )
    assert event.get_extra(EXTRA_RENDERED_PATH) is None
    print("passed: sign_render_still_clears_and_appends")


def test_standalone_mode_untouched():
    """standalone 模式: 钩子直接返回,不碰任何 extra。"""
    import asyncio

    class _StandalonePlugin:
        sign_mode = "standalone"

    event = _Event({UPSTREAM_SELECTED_IDS: ["meme:x"]})
    asyncio.run(on_decorating_result_first(_StandalonePlugin(), event))
    assert event.get_extra(UPSTREAM_SELECTED_IDS) == ["meme:x"]
    print("passed: standalone_mode_untouched")


if __name__ == "__main__":
    test_no_sign_render_keeps_official_selected_ids()
    test_sign_render_still_clears_and_appends()
    test_standalone_mode_untouched()
    print("3 decorator-gate regression tests passed")
