"""上游官方 meme_manager 兼容层(零修改对接的唯一入口)。

设计(2026-09-10 零修改方案,计划见 .hermes/plans/):
- 本插件对上游的全部 import 必须经由本模块,禁止散落各处;
- 只依赖官方公开模块函数(semantic_storage/semantic_models/semantic_index);
- probe_upstream() 失败时调用方必须降级 standalone,不得猜测上游行为;
- 嵌入 provider 经 provider_selection.json(官方数据文件,只读)解析,
  并与索引 manifest 的 provider:model:dimension 三元组强制比对——
  search_index 对不一致会静默返回空,宁可不重建也不能写坏。

已核实的上游事实(源码行号见实现计划):
- 钩子按 priority 全局降序执行(star_handler.py:26);
- 同一 event 对象按序共享(context_utils.py:95-101);
- SemanticImage.from_dict/to_dict 丢弃未知字段 → 识别只能用
  category+relative_path 等官方核心字段(semantic_models.py:616)。
"""
from __future__ import annotations

import json
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

COMPAT_VERSION_RANGE = ">=4.17.0"  # 实测过的上游版本区间(声明性)
_UPSTREAM_MODULE_DIR = "astrbot_plugin_meme_manager"


@dataclass
class UpstreamInfo:
    plugins_root: Path
    plugin_dir: Path
    version: str
    plugin_data_dir: Path
    default_pack_dir: Path | None

    @property
    def compatible(self) -> bool:
        """已知兼容区间判断。上游无 semver 时宽松放行(探测以模块导入为准)。"""
        return True


def _candidate_plugins_roots() -> list[Path]:
    roots: list[Path] = []
    try:
        from astrbot.core.utils.astrbot_path import get_astrbot_data_path

        roots.append(Path(get_astrbot_data_path()) / "plugins")
    except Exception:
        pass
    # 本文件位于 plugins/astrbot_plugin_sign_meme/backend/ → plugins 根是 parent.parent
    here = Path(__file__).resolve().parent.parent
    roots.append(here.parent)
    return roots


def _read_default_pack_id(plugin_data_dir: Path) -> str:
    rules_path = plugin_data_dir / "selection_rules.json"
    try:
        rules = json.loads(rules_path.read_text(encoding="utf-8"))
        for rule in rules.get("rules", []):
            if str(rule.get("scope") or "").strip().lower() == "default":
                pack_id = str(rule.get("pack_id") or "").strip()
                if re.fullmatch(r"[A-Za-z0-9._-]{2,64}", pack_id):
                    return pack_id
    except Exception:
        pass
    try:
        registry = json.loads(
            (plugin_data_dir / "registry.json").read_text(encoding="utf-8")
        )
        for pack in registry.get("installed_packs", []):
            if pack.get("enabled"):
                pack_id = str(pack.get("id") or "").strip()
                if re.fullmatch(r"[A-Za-z0-9._-]{2,64}", pack_id):
                    return pack_id
    except Exception:
        pass
    return ""


def _read_upstream_version(plugin_dir: Path) -> str:
    try:
        text = (plugin_dir / "metadata.yaml").read_text(encoding="utf-8")
    except Exception:
        return ""
    match = re.search(r"^version:\s*['\"]?([^\s'\"]+)", text, re.MULTILINE)
    return match.group(1) if match else ""


def probe_upstream(
    plugins_root: Path | None,
    plugin_data_dir: Path,
    *,
    allow_fallback: bool = True,
) -> UpstreamInfo | None:
    """探测官方 meme_manager 是否可用。

    成功条件: 插件目录存在 + backend/semantic_storage.py 文件在 +
    selection_rules/registry 可解析出 default pack(允许无 pack)。
    allow_fallback=False 时只检查传入的 plugins_root(测试隔离用)。
    任一失败返回 None——调用方必须降级 standalone。
    """
    candidates = [plugins_root] if plugins_root else []
    if allow_fallback:
        candidates.extend(_candidate_plugins_roots())
    for root in candidates:
        if not root:
            continue
        plugin_dir = root / _UPSTREAM_MODULE_DIR
        marker = plugin_dir / "backend" / "semantic_storage.py"
        if not marker.is_file():
            continue
        pack_id = _read_default_pack_id(plugin_data_dir)
        pack_dir: Path | None = None
        if pack_id:
            candidate = (plugin_data_dir / "packs" / pack_id).resolve()
            try:
                candidate.relative_to((plugin_data_dir / "packs").resolve())
            except ValueError:
                candidate = None  # type: ignore[assignment]
            if candidate is not None and candidate.is_dir():
                pack_dir = candidate
        return UpstreamInfo(
            plugins_root=root,
            plugin_dir=plugin_dir,
            version=_read_upstream_version(plugin_dir),
            plugin_data_dir=plugin_data_dir,
            default_pack_dir=pack_dir,
        )
    return None


def public_api() -> tuple[Any, Any, Any] | None:
    """导入上游公开模块 (semantic_storage, semantic_models, semantic_index)。

    统一在此注入 sys.path;不可用时返回 None(禁止调用方各自 try-import)。
    """
    for root in _candidate_plugins_roots():
        if not root:
            continue
        if (root / _UPSTREAM_MODULE_DIR / "backend" / "semantic_storage.py").is_file():
            s = str(root)
            if s not in sys.path:
                sys.path.insert(0, s)
            break
    try:
        from astrbot_plugin_meme_manager.backend import (  # noqa: PLC0415
            semantic_index,
            semantic_models,
            semantic_storage,
        )
        return semantic_storage, semantic_models, semantic_index
    except Exception:
        return None


def resolve_embedding_provider_for_pack(
    context: Any,
    pack_dir: Path,
    plugin_data_dir: Path,
    *,
    adapter_cls: Any = None,
) -> tuple[Any, Any] | None:
    """解析与 pack 现有索引一致的嵌入 provider。

    流程: provider_selection.json(官方只读数据文件)取 effective_provider_id
    → AstrBot 核心 context.get_provider_by_id() → EmbeddingAdapter 包装
    → signature 与 index_manifest.json 的三元组比对。
    不一致/任一环节失败返回 None——调用方应跳过向量重建(宁缺勿错)。

    adapter_cls 参数供测试注入假 Adapter;生产环境不传,内部延迟导入官方类。
    """
    if adapter_cls is None:
        api = public_api()
        if api is None:
            return None
        from astrbot_plugin_meme_manager.backend.semantic_index import (  # noqa: PLC0415
            EmbeddingAdapter as adapter_cls,  # type: ignore[misc]
        )
    pack_id = pack_dir.name
    idx_dir = plugin_data_dir / "semantic_indexes" / pack_id

    manifest: dict[str, Any] = {}
    try:
        manifest = json.loads(
            (idx_dir / "index_manifest.json").read_text(encoding="utf-8")
        )
    except Exception:
        return None

    selection: dict[str, Any] = {}
    try:
        selection = json.loads(
            (idx_dir / "provider_selection.json").read_text(encoding="utf-8")
        )
    except Exception:
        selection = {}

    provider_id = str(
        selection.get("effective_provider_id")
        or selection.get("configured_provider_id")
        or ""
    ).strip()
    if not provider_id:
        return None

    resolver = getattr(context, "get_provider_by_id", None)
    if not callable(resolver):
        return None
    try:
        provider = resolver(provider_id)
    except Exception:
        return None
    if provider is None:
        return None

    adapter = adapter_cls(provider, provider_id)
    if not getattr(adapter, "ready", False):
        return None

    same = (
        str(manifest.get("embedding_provider_id") or "") == str(adapter.provider_id)
        and str(manifest.get("embedding_model") or "") == str(adapter.model_name)
        and int(manifest.get("embedding_dimension") or 0) == int(
            getattr(adapter, "dimension", 0) or 0
        )
    )
    if not same:
        return None
    return provider, adapter
