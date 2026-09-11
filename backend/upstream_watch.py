"""上游外部插件能力检测与状态机(integrated 模式依赖)。

设计(.hermes/plans/2026-09-10-upstream-capability-watch.md):
- 检测断言 R1..R5: installed/activated/semantic-modules/pack/hooks-alive;
- check_upstream() 返回 UpstreamStatus,失败短路(半初始化实例不调用方法);
- 模块级 TTL 缓存: 钩子高频入口不重复探测,测试可注入时钟;
- 状态变迁由 refresh_state() 判定并返回事件描述,调用方负责打日志与
  effective 模式联动(main.py);
- 只依赖 AstrBot 官方发现 API(Context.get_registered_star)与本插件
  compat.probe_upstream,不 import 上游业务模块(除 compat.public_api 的
  官方公开三模块导入探测)。
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

UPSTREAM_STAR_NAME = "meme_manager"

STATE_OK = "ok"
STATE_MISSING = "missing"
STATE_DEGRADED = "degraded"

_DEFAULT_TTL_SECONDS = 60.0


@dataclass
class UpstreamStatus:
    """一次能力检测的完整结果。"""

    ok: bool
    state: str
    reasons: list[str] = field(default_factory=list)
    version: str = ""
    checked_at: float = 0.0


@dataclass
class _Cache:
    status: UpstreamStatus | None = None
    expires_at: float = 0.0


_cache = _Cache()
_last_state: str | None = None
_TTL_SECONDS = _DEFAULT_TTL_SECONDS
_time_fn = time.monotonic


def configure(ttl_seconds: float | None = None, time_fn: Any | None = None) -> None:
    """测试注入: 调整 TTL 或时钟函数,并清空缓存与状态记忆。"""
    global _TTL_SECONDS, _time_fn, _last_state
    if ttl_seconds is not None:
        _TTL_SECONDS = float(ttl_seconds)
    if time_fn is not None:
        _time_fn = time_fn
    _last_state = None
    reset_cache()


def reset_cache() -> None:
    _cache.status = None
    _cache.expires_at = 0.0


def last_known_state() -> str | None:
    """上次检测结果的状态(未检测过返回 None),供日志去重。"""
    return _last_state


def current_status() -> UpstreamStatus | None:
    """当前缓存的一次检测结果(L3: 供 Web API 读取,避免摸私有 _cache)。"""
    return _cache.status


def _reason(status: UpstreamStatus, code: str, detail: str) -> None:
    status.reasons.append(f"{code} {detail}")


def _check_installed(context: Any, status: UpstreamStatus):
    """R1/R2: 官方注册表发现插件且已激活、实例可用。返回 star_cls 或 None。"""
    resolver = getattr(context, "get_registered_star", None) if context else None
    if not callable(resolver):
        _reason(status, "R1", "installed: AstrBot Context 无 get_registered_star")
        return None
    meta = None
    try:
        meta = resolver(UPSTREAM_STAR_NAME)
    except Exception:
        meta = None
    if meta is None:
        _reason(status, "R1", f"installed: {UPSTREAM_STAR_NAME} 未安装")
        return None
    status.version = str(getattr(meta, "version", "") or "")
    if not bool(getattr(meta, "activated", False)):
        _reason(status, "R2", "activated: 插件已安装但被停用")
        return None
    star_cls = getattr(meta, "star_cls", None)
    if star_cls is None:
        _reason(status, "R2", "activated: 插件已激活但实例不可用(star_cls=None)")
        return None
    return star_cls


def _check_semantic_modules(status: UpstreamStatus) -> bool:
    """R3: 官方公开语义模块可导入(经 compat 统一入口)。"""
    from . import compat  # 局部导入避免测试环境循环依赖

    if compat.public_api() is None:
        _reason(status, "R3", "semantic: 上游 backend 公开模块导入失败")
        return False
    return True


def _check_pack(plugin_data_dir: Path | None, status: UpstreamStatus, require_pack: bool) -> bool:
    """R4: 默认表情包目录可解析。require_pack=False 时仅告警不判失败。"""
    from . import compat  # 局部导入

    info = None
    try:
        info = compat.probe_upstream(None, plugin_data_dir or Path("/nonexistent"))
    except Exception:
        info = None
    pack_dir = info.default_pack_dir if info else None
    if pack_dir is None:
        if require_pack:
            _reason(status, "R4", "pack: 默认表情包目录不可用")
            return False
        status.reasons.append("R4 pack: 默认表情包目录未配置(允许,语义池为空)")
        return True
    return True


def _check_hooks_alive(context: Any, status: UpstreamStatus) -> bool:
    """R5: 上游 search_memes LLM 工具仍在注册表中。"""
    tool_mgr = getattr(context, "get_llm_tool_manager", None) if context else None
    if not callable(tool_mgr):
        _reason(status, "R5", "hooks: AstrBot 无 LLM 工具管理器")
        return False
    try:
        manager = tool_mgr()
        tools: Any = getattr(manager, "func_list", None)
        getter = getattr(manager, "get_full_tool_set", None)
        if tools is None and callable(getter):
            tools = getter()
        names: set[str] = set()
        for tool in tools or []:
            names.add(str(getattr(tool, "name", "") or ""))
        if "search_memes" not in names:
            _reason(status, "R5", "hooks: search_memes 工具未注册(上游功能残缺)")
            return False
    except Exception:
        _reason(status, "R5", "hooks: 工具注册表读取失败")
        return False
    return True


def check_upstream(
    context: Any,
    plugin_data_dir: Path | None = None,
    *,
    require_pack: bool = True,
) -> UpstreamStatus:
    """执行一次完整能力检测(R1→R5 失败短路)。"""
    status = UpstreamStatus(ok=False, state=STATE_MISSING)
    star_cls = _check_installed(context, status)
    if star_cls is not None and _check_semantic_modules(status):
        if _check_pack(plugin_data_dir, status, require_pack) and _check_hooks_alive(
            context, status
        ):
            status.ok = True
            status.state = STATE_OK
    status.checked_at = _time_fn()
    return status


def refresh_state(
    context: Any,
    plugin_data_dir: Path | None = None,
    *,
    require_pack: bool = True,
    force: bool = False,
) -> tuple[UpstreamStatus, dict]:
    """带 TTL 缓存的检测入口;返回 (status, transition)。

    transition={"changed": bool, "from": str|None, "to": str}
    仅在状态真正变迁时 changed=True,调用方据此打一条日志并触发 reconcile。
    """
    global _last_state
    now = _time_fn()
    if (
        not force
        and _cache.status is not None
        and now < _cache.expires_at
    ):
        return _cache.status, {"changed": False, "from": _last_state, "to": _last_state or ""}

    status = check_upstream(context, plugin_data_dir, require_pack=require_pack)
    _cache.status = status
    _cache.expires_at = now + _TTL_SECONDS

    previous = _last_state
    _last_state = status.state
    return status, {
        "changed": previous is not None and previous != status.state,
        "from": previous,
        "to": status.state,
    }
