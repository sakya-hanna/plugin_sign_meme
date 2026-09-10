"""main.py 钩子注册矩阵与分派逻辑测试(源码级断言 + 假件冒烟)。"""
import ast
import asyncio
from pathlib import Path

import _bootstrap  # noqa: F401  路径引导

MAIN_PY = Path(__file__).resolve().parent.parent / "main.py"


def _decorator_priorities():
    """解析 main.py 中 on_llm_response/on_decorating_result 的 priority 参数。"""
    tree = ast.parse(MAIN_PY.read_text(encoding="utf-8"))
    found = {}
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for dec in node.decorator_list:
            if not isinstance(dec, ast.Call):
                continue
            func = dec.func
            name = getattr(func, "attr", None) or getattr(func, "id", "")
            if name in ("on_llm_response", "on_decorating_result", "on_llm_request"):
                priority = None
                for kw in dec.keywords:
                    if kw.arg == "priority":
                        priority = kw.value.value
                found.setdefault(name, []).append((node.name, priority))
    return found


def test_response_hooks_have_two_priorities():
    """response 必须有两个钩子: 100000(拦截) + 99998(上游检索后移除)。"""
    found = _decorator_priorities()
    resp = found.get("on_llm_response", [])
    priorities = sorted({p for _, p in resp if p is not None}, reverse=True)
    assert 100000 in priorities, f"缺少 100000 前置钩子: {resp}"
    assert 99998 in priorities, f"缺少 99998 上游后钩子: {resp}"


def test_decorating_hook_has_priority_100000():
    found = _decorator_priorities()
    dec = found.get("on_decorating_result", [])
    priorities = [p for _, p in dec if p is not None]
    assert 100000 in priorities, f"decorating 必须带 100000: {dec}"


def test_no_default_priority_hooks():
    """不允许存在未指定 priority 的 response/decorating 钩子(默认0会晚于上游)。"""
    found = _decorator_priorities()
    for name in ("on_llm_response", "on_decorating_result"):
        for fn_name, priority in found.get(name, []):
            assert priority is not None, f"{fnBasename(fn_name)} 缺少显式 priority"


def fnBasename(full_name):
    return full_name.rsplit(".", 1)[-1]


def test_dispatch_standalone_and_integrated():
    """分派函数: standalone 走旧链路,integrated 走新管线。"""
    from backend import integrated_events
    from backend import standalone_events

    assert hasattr(standalone_events, "handle_llm_response")
    assert hasattr(integrated_events, "on_llm_response_first")
    assert hasattr(integrated_events, "on_llm_response_second")
    assert hasattr(integrated_events, "on_decorating_result_first")


if __name__ == "__main__":
    test_response_hooks_have_two_priorities()
    test_decorating_hook_has_priority_100000()
    test_no_default_priority_hooks()
    test_dispatch_standalone_and_integrated()
    print("main hooks tests PASS (4)")
