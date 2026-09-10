"""举牌模板语义池同步(对接模式):写入/移除主 pack 检索池中的 sign 记录。

设计(2026-09-10 双模式架构 Task 3):
- 主 pack = selection_rules.json 的 default pack(当前 manosaba-001);
- 每个举牌模板在主 pack 中有一条语义记录:
  * 物理文件: 主 pack memes/举牌模板/<template_id>.png (第三镜像,真实字节拷贝,
    因 safe_relative_path 拒绝 .. 跨包穿越——实测 backend/semantic_storage.py:185);
  * metadata entry: is_sign_template=true + sign_template_id 供消费方识别;
  * caption/tags 来自模板(用户在举牌模板页维护), caption_status=done;
  * FAISS 向量: 调 meme_manager build_index(target_entry_ids={...}) 单条增量,
    不整包重嵌。
- standalone 模式: 所有 sign 记录从主 pack 池移除(含向量),reconcile 双向。
- 全部操作走 meme_manager 的公共函数(load/save_metadata, build_index),
  不手改 FAISS 文件。
"""
from __future__ import annotations

import hashlib
import json
import re
import shutil
from pathlib import Path
from typing import Any

SIGN_CATEGORY = "举牌模板"
SIGN_FLAG = "is_sign_template"
SIGN_ID_FIELD = "sign_template_id"


def _refresh_pack_totals(storage, pack_dir: Path, metadata: dict) -> None:
    """按 meme_manager 的约定重算 pack 顶层快照字段。

    semantic_metadata_is_complete() 除了逐条记录检查外,还要求 metadata
    顶层的 file_total/unique_total 与磁盘实际一致(semantic_storage.py
    在分类移动等写路径同样如此维护,见其 1772-1781 行)。只改 images
    字典不重算 totals,会让完整性检查永远 False,进而挡住 search_memes
    的 require_embeddings=True 门禁。防回归要点,见 README。
    """
    memes_root = pack_dir / "memes"
    scanned = storage.scan_images(pack_dir)
    scanned_entry_ids = {item["entry_id"] for item in scanned}
    metadata["file_total"] = sum(
        1
        for path in memes_root.rglob("*")
        if path.is_file() and path.suffix.lower() in storage.IMAGE_EXTENSIONS
    )
    metadata["unique_total"] = len(scanned_entry_ids)
    metadata["content_unique_total"] = len(
        {item["content_sha256"] for item in scanned}
    )


def _plugin_data_root() -> Path:
    try:
        from astrbot.core.utils.astrbot_path import get_astrbot_data_path
        return Path(get_astrbot_data_path()) / "plugin_data" / "meme_manager"
    except Exception:
        # 测试环境: 相对布局
        here = Path(__file__).resolve().parent
        return here.parent / "data" / "plugin_data" / "meme_manager"


def _mm_imports():
    """延迟导入 meme_manager 后端,插件未安装时返回 None。

    两条路径: AstrBot 运行时(astrbot 包可导入) → 标准插件目录;
    纯 pytest 环境 → 沿 plugin_data 上溯找 plugins/astrbot_plugin_meme_manager。
    """
    import sys
    candidates = []
    try:
        from astrbot.core.utils.astrbot_path import get_astrbot_data_path
        candidates.append(Path(get_astrbot_data_path()) / "plugins")
    except Exception:
        pass
    here = Path(__file__).resolve().parent
    # sign_meme 位于 plugins/<本插件>/ → plugins 根是 parent
    candidates.append(here.parent)
    for plugins_root in candidates:
        if (plugins_root / "astrbot_plugin_meme_manager" / "backend" / "semantic_storage.py").is_file():
            s = str(plugins_root)
            if s not in sys.path:
                sys.path.insert(0, s)
            break
    try:
        from astrbot_plugin_meme_manager.backend import semantic_storage
        from astrbot_plugin_meme_manager.backend import semantic_models
        return semantic_storage, semantic_models
    except Exception:
        return None, None


def _default_pack_dir(root: Path) -> Path | None:
    """主 pack = selection_rules default;找不到时回退 registry 第一个 enabled。"""
    rules_path = root / "selection_rules.json"
    pack_id = ""
    try:
        rules = json.loads(rules_path.read_text(encoding="utf-8"))
        for rule in rules.get("rules", []):
            if str(rule.get("scope") or "").strip().lower() == "default":
                pack_id = str(rule.get("pack_id") or "").strip()
                break
    except Exception:
        pass
    if not pack_id:
        try:
            registry = json.loads((root / "registry.json").read_text(encoding="utf-8"))
            for pack in registry.get("installed_packs", []):
                if pack.get("enabled"):
                    pack_id = str(pack.get("id") or "").strip()
                    break
        except Exception:
            pass
    if not pack_id or not re.fullmatch(r"[A-Za-z0-9._-]{2,64}", pack_id):
        return None
    pack_dir = (root / "packs" / pack_id).resolve()
    try:
        pack_dir.relative_to((root / "packs").resolve())
    except ValueError:
        return None
    return pack_dir if pack_dir.is_dir() else None


def _find_sign_entries(metadata: dict) -> dict[str, dict]:
    return {
        eid: item
        for eid, item in (metadata.get("images") or {}).items()
        if isinstance(item, dict) and item.get(SIGN_FLAG)
    }


def _template_digest(template: dict) -> str:
    return str(template.get("image_sha256") or template.get("sha256") or "").strip().lower()


def build_sign_entry(
    template: dict, relative_path: str, entry_id: str
) -> dict[str, Any]:
    """构建与 manosaba 记录同构的 sign 语义条目。"""
    caption = str(template.get("caption") or "").strip()
    tags = [str(t) for t in (template.get("tags") or []) if str(t).strip()]
    return {
        "entry_id": entry_id,
        "category": SIGN_CATEGORY,
        "category_tag": f"category:{SIGN_CATEGORY}",
        "relative_path": relative_path,
        "content_sha256": _template_digest(template),
        "caption": caption,
        "tags": [f"category:{SIGN_CATEGORY}"] + tags,
        "visible_text": "",
        "caption_status": "done" if caption else "done",
        "embedding_status": "pending",
        "error": "",
        "manual_override": True,
        "manual_caption": caption,
        "manual_tags": tags,
        "manual_visible_text": "",
        "category_review_status": "manual_confirmed",
        "category_description": "举牌模板(生成型表情包:命中后渲染文字再发送)",
        "provenance": "sign_meme",
        "updated_at": template.get("source_updated_at") or "",
        SIGN_FLAG: True,
        SIGN_ID_FIELD: str(template.get("id") or ""),
        "sign_template_name": str(template.get("name") or ""),
    }


class SemanticPoolSync:
    """对接模式语义池同步器。所有方法纯同步(mirror 事务内调用)。"""

    def __init__(self, root: Path | None = None, logger=None):
        self.root = root or _plugin_data_root()
        self.logger = logger
        storage, models = _mm_imports()
        self.storage = storage
        self.models = models

    def available(self) -> bool:
        return self.storage is not None and _default_pack_dir(self.root) is not None

    def _log(self, level: str, msg: str, **kw):
        if self.logger:
            getattr(self.logger, level)(msg, **kw)

    # ---- 记录维护(模板生命周期钩子调用) ----

    def upsert(self, template: dict, template_image: Path) -> dict[str, Any]:
        """新增/更新一个模板的池内记录+文件镜像。返回操作描述。"""
        if not self.available():
            return {"ok": False, "reason": "meme_manager 不可用"}
        pack_dir = _default_pack_dir(self.root)
        storage = self.storage
        models = self.models
        # 空 caption/tags 的记录过不了 semantic_caption_is_complete,
        # 会连带挡住整个 pack 的检索门禁——结构性不允许入池。
        # 必须在文件拷贝前拒绝,避免留下没有记录的孤儿镜像文件。
        if not str(template.get("caption") or "").strip() or not [
            str(t) for t in (template.get("tags") or []) if str(t).strip()
        ]:
            return {
                "ok": False,
                "reason": "模板 caption 或 tags 为空,不能入池(会挡住检索门禁)",
            }
        target_dir = pack_dir / "memes" / SIGN_CATEGORY
        target_dir.mkdir(parents=True, exist_ok=True)
        relative = f"memes/{SIGN_CATEGORY}/{template['id']}.png"
        target = pack_dir / relative
        shutil.copyfile(template_image, target)
        digest = hashlib.sha256(target.read_bytes()).hexdigest()
        entry_id = models.semantic_entry_id(digest, SIGN_CATEGORY, relative)
        metadata = storage.load_metadata(pack_dir)
        images: dict = metadata.setdefault("images", {})
        entry = build_sign_entry(
            {**template, "id": template["id"], "image_sha256": digest},
            relative,
            entry_id,
        )
        # 幂等保护: 同 entry_id 且语义文本(caption/tags/分类上下文)未变时,
        # 保留已完成的向量状态字段——否则每次启动 reconcile 都会把
        # embedding_status 打回 pending,已建好的 FAISS 向量被标记失效。
        # (向量按文本哈希对齐;语义文本变了才需要真正重嵌。)
        previous = images.get(entry_id)
        if isinstance(previous, dict) and previous.get(SIGN_FLAG):
            normalize_tags = models.normalize_tags
            text_unchanged = (
                str(previous.get("caption") or "") == str(entry.get("caption") or "")
                and sorted(normalize_tags(previous.get("tags")))
                == sorted(normalize_tags(entry.get("tags")))
            )
            if text_unchanged:
                for field in (
                    "embedding_status",
                    "text_hash",
                    "category_context_hash",
                    "category_review_context_hash",
                    "manual_confirmation_context_hash",
                ):
                    if field in previous:
                        entry[field] = previous[field]
        images[entry_id] = entry
        _refresh_pack_totals(storage, pack_dir, metadata)
        storage.save_metadata(pack_dir, metadata)
        self._log("info", "event=sign_pool_upserted template_id=%s entry_id=%s",
                  template_id=template["id"], entry_id=entry_id[:12])
        return {"ok": True, "entry_id": entry_id, "needs_vector": True}

    def remove(self, template_id: str) -> dict[str, Any]:
        """删除模板的池内记录+文件镜像。"""
        if not self.available():
            return {"ok": False, "reason": "meme_manager 不可用"}
        pack_dir = _default_pack_dir(self.root)
        storage = self.storage
        metadata = storage.load_metadata(pack_dir)
        removed = []
        for eid, item in list((metadata.get("images") or {}).items()):
            if isinstance(item, dict) and item.get(SIGN_FLAG) and str(
                item.get(SIGN_ID_FIELD) or ""
            ) == str(template_id):
                del metadata["images"][eid]
                removed.append(eid)
                rel = str(item.get("relative_path") or "")
                if rel:
                    f = pack_dir / rel
                    if f.is_file():
                        f.unlink()
        if removed:
            _refresh_pack_totals(storage, pack_dir, metadata)
            storage.save_metadata(pack_dir, metadata)
        self._log("info", "event=sign_pool_removed template_id=%s entries=%d",
                  template_id=template_id, entries=len(removed))
        return {"ok": True, "removed": removed}

    # ---- 模式切换 reconcile ----

    def reconcile(self, mode: str, templates: list[dict], image_resolver) -> dict:
        """双向对齐: integrated=补齐缺失;standalone=全部移除。"""
        if not self.available():
            return {"ok": False, "reason": "meme_manager 不可用"}
        pack_dir = _default_pack_dir(self.root)
        storage = self.storage
        metadata = storage.load_metadata(pack_dir)
        existing = _find_sign_entries(metadata)
        by_template = {
            str(v.get(SIGN_ID_FIELD) or ""): (eid, v) for eid, v in existing.items()
        }
        ops = {"added": [], "updated": [], "removed": []}
        if mode == "integrated":
            want_ids = {str(t.get("id")) for t in templates}
            for t in templates:
                tid = str(t.get("id"))
                img = image_resolver(t)
                if img is None or not Path(img).is_file():
                    continue
                result = self.upsert(t, Path(img))
                if result.get("ok"):
                    ops["added" if tid not in by_template else "updated"].append(tid)
            # 清掉源已不存在的残留记录
            for tid, (eid, item) in by_template.items():
                if tid not in want_ids:
                    r = self.remove(tid)
                    if r.get("ok"):
                        ops["removed"].append(tid)
        else:  # standalone: 全部出池
            for tid in by_template:
                r = self.remove(tid)
                if r.get("ok"):
                    ops["removed"].append(tid)
        return {"ok": True, **ops}

    # ---- 向量增量(异步,由调用方在事务提交后调度) ----

    async def rebuild_vectors(self, entry_ids: list[str] | None = None) -> dict:
        """按 entry 增量重建 FAISS;entry_ids=None 时重建整包。"""
        if not self.available():
            return {"ok": False, "reason": "meme_manager 不可用"}
        pack_dir = _default_pack_dir(self.root)
        try:
            from astrbot_plugin_meme_manager.backend.semantic_index import (
                EmbeddingAdapter, build_index,
            )
            # embedding provider 由调用方注入(需要 AstrBot context);
            # 这里不做,向量重建统一走 meme_manager 管理页已有的
            # "重建"入口/或由 main.py 在运行时触发。此处仅提供纯函数入口。
            return {"ok": False, "reason": "embedding_provider_required"}
        except Exception as exc:
            return {"ok": False, "reason": str(exc)}
