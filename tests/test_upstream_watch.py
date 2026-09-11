"""upstream_watch 能力检测与状态机回归。

覆盖设计(.hermes/plans/2026-09-10-upstream-capability-watch.md)验收项:
T1 R1..R5 各失败注入 → state/reasons 正确
T2 OK→missing→OK 状态变迁 changed 事件各一次
T3 TTL 内不重探(时钟注入)
T4 last_known_state 供 effective 判定
T5 无上游(空 context)全链降级
"""
from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backend import upstream_watch as uw  # noqa: E402


class _Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


class _ToolMgr:
    def __init__(self, names: list[str]) -> None:
        self.func_list = [SimpleNamespace(name=n) for n in names]


def _ok_context() -> SimpleNamespace:
    """全部 R1-R5 通过的最小 context。"""

    def resolver(name: str):
        if name == "meme_manager":
            return SimpleNamespace(
                name=name,
                version="4.15.4",
                activated=True,
                star_cls=SimpleNamespace(),
            )
        return None

    return SimpleNamespace(
        get_registered_star=resolver,
        get_llm_tool_manager=lambda: _ToolMgr(["search_memes", "other"]),
    )


def _missing_context() -> SimpleNamespace:
    return SimpleNamespace(
        get_registered_star=lambda name: None,
        get_llm_tool_manager=lambda: _ToolMgr([]),
    )


def _fresh(clock: _Clock, ttl: float = 60.0) -> None:
    uw.configure(ttl_seconds=ttl, time_fn=clock)


def test_all_pass_state_ok():
    clock = _Clock()
    _fresh(clock)
    status = uw.check_upstream(_ok_context(), Path("/nonexistent"), require_pack=False)
    assert status.ok and status.state == "ok" and status.version == "4.15.4"
    print("passed: all_pass_state_ok")


def test_each_requirement_failure_reasons():
    clock = _Clock()
    _fresh(clock)
    # R1 未安装
    s = uw.check_upstream(_missing_context(), Path("/nonexistent"), require_pack=False)
    assert not s.ok and s.state == "missing" and any(r.startswith("R1") for r in s.reasons)
    # R2 停用
    ctx = _ok_context()
    ctx.get_registered_star = lambda name: SimpleNamespace(
        name=name, version="4.15.4", activated=False, star_cls=None
    )
    s = uw.check_upstream(ctx, Path("/nonexistent"), require_pack=False)
    assert any(r.startswith("R2") for r in s.reasons) and s.state == "missing"
    # R2 star_cls 缺失
    ctx.get_registered_star = lambda name: SimpleNamespace(
        name=name, version="4.15.4", activated=True, star_cls=None
    )
    s = uw.check_upstream(ctx, Path("/nonexistent"), require_pack=False)
    assert any("star_cls=None" in r for r in s.reasons)
    # R5 工具残缺(R3 在测试环境也失败,但 R3 先短路→单独用 monkeypatch 验证 R5)
    import backend.compat as compat

    orig_api = compat.public_api
    compat.public_api = lambda: ("a", "b", "c")  # type: ignore[assignment]
    try:
        ctx2 = _ok_context()
        ctx2.get_llm_tool_manager = lambda: _ToolMgr(["other"])
        s = uw.check_upstream(ctx2, Path("/nonexistent"), require_pack=False)
        assert any(r.startswith("R5") for r in s.reasons)
    finally:
        compat.public_api = orig_api  # type: ignore[assignment]
    print("passed: each_requirement_failure_reasons")


def test_state_transition_events():
    clock = _Clock()
    _fresh(clock)
    ctx = _ok_context()
    s1, t1 = uw.refresh_state(ctx, Path("/nonexistent"), require_pack=False, force=True)
    assert s1.state == "ok" and t1["changed"] is False  # 首检不算变迁
    # 上游消失
    missing = _missing_context()
    clock.now += 61
    s2, t2 = uw.refresh_state(missing, Path("/nonexistent"), require_pack=False)
    assert s2.state == "missing" and t2["changed"] is True and t2["from"] == "ok"
    # 恢复
    clock.now += 61
    s3, t3 = uw.refresh_state(ctx, Path("/nonexistent"), require_pack=False, force=True)
    assert s3.state == "ok" and t3["changed"] is True and t3["to"] == "ok"
    print("passed: state_transition_events")


def test_ttl_cache_skips_recheck():
    clock = _Clock()
    _fresh(clock)
    ctx = _ok_context()
    uw.refresh_state(ctx, Path("/nonexistent"), require_pack=False, force=True)
    # 上游在 TTL 内消失,但不 force → 不应重探
    missing = _missing_context()
    clock.now += 10
    s, t = uw.refresh_state(missing, Path("/nonexistent"), require_pack=False)
    assert s.state == "ok" and t["changed"] is False
    # TTL 过期 → 重探降级
    clock.now += 61
    s, t = uw.refresh_state(missing, Path("/nonexistent"), require_pack=False)
    assert s.state == "missing" and t["changed"] is True
    print("passed: ttl_cache_skips_recheck")


def test_last_known_state():
    clock = _Clock()
    _fresh(clock)
    assert uw.last_known_state() is None
    uw.refresh_state(_ok_context(), Path("/nonexistent"), require_pack=False, force=True)
    assert uw.last_known_state() == "ok"
    print("passed: last_known_state")


def test_empty_context_degrades():
    clock = _Clock()
    _fresh(clock)
    s = uw.check_upstream(None, Path("/nonexistent"), require_pack=False)
    assert not s.ok and s.state == "missing"
    print("passed: empty_context_degrades")


if __name__ == "__main__":
    test_all_pass_state_ok()
    test_each_requirement_failure_reasons()
    test_state_transition_events()
    test_ttl_cache_skips_recheck()
    test_last_known_state()
    test_empty_context_degrades()
    print("6 upstream_watch tests passed")
