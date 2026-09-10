"""语义池同步(对接模式)测试: upsert/remove/reconcile 与元数据形态。"""
import json
import sys
from pathlib import Path
from tempfile import TemporaryDirectory

sys.path.insert(0, str(Path(__file__).resolve().parent))

from semantic_pool import (  # noqa: E402
    SIGN_CATEGORY, SIGN_FLAG, SIGN_ID_FIELD,
    SemanticPoolSync, build_sign_entry,
)


def _make_env(td: Path):
    """构造最小 meme_manager 布局: 主 pack + selection_rules + 空 metadata。"""
    root = td / "plugin_data" / "meme_manager"
    pack = root / "packs" / "main-pack"
    (pack / "memes" / "selected").mkdir(parents=True)
    (root / "selection_rules.json").write_text(json.dumps({
        "rules": [{"id": "d", "scope": "default", "pack_id": "main-pack"}]
    }), encoding="utf-8")
    (root / "registry.json").write_text(json.dumps({
        "installed_packs": [{"id": "main-pack", "enabled": True}]
    }), encoding="utf-8")
    return root, pack


def _template(tid="t1", caption="评价场景", tags=None):
    return {"id": tid, "name": "测试模板", "caption": caption,
            "tags": tags or ["吐槽"], "active": True}


def _image(td: Path, tid="t1", marker=b"png1"):
    p = td / f"{tid}.png"
    p.write_bytes(marker)
    return p


def test_build_entry_shape():
    e = build_sign_entry(_template(), "memes/举牌模板/t1.png", "eid1")
    assert e[SIGN_FLAG] is True
    assert e[SIGN_ID_FIELD] == "t1"
    assert e["category"] == SIGN_CATEGORY
    assert e["caption_status"] == "done"
    assert e["tags"][0] == f"category:{SIGN_CATEGORY}"
    assert e["relative_path"] == "memes/举牌模板/t1.png"


def test_upsert_and_remove_roundtrip():
    with TemporaryDirectory() as tmp:
        td = Path(tmp)
        root, pack = _make_env(td)
        sync = SemanticPoolSync(root=root)
        assert sync.available()
        img = _image(td)
        r = sync.upsert(_template(), img)
        assert r["ok"] and r["needs_vector"]
        meta = json.loads((pack / "semantic_metadata.json").read_text(encoding="utf-8")) \
            if (pack / "semantic_metadata.json").exists() \
            else sync.storage.load_metadata(pack)
        sign_entries = [v for v in meta["images"].values() if v.get(SIGN_FLAG)]
        assert len(sign_entries) == 1
        assert sign_entries[0][SIGN_ID_FIELD] == "t1"
        assert (pack / "memes" / SIGN_CATEGORY / "t1.png").is_file()
        # upsert 幂等
        r2 = sync.upsert(_template(caption="改"), img)
        assert r2["ok"]
        meta2 = sync.storage.load_metadata(pack)
        sign2 = [v for v in meta2["images"].values() if v.get(SIGN_FLAG)]
        assert len(sign2) == 1 and sign2[0]["caption"] == "改"
        # remove
        r3 = sync.remove("t1")
        assert r3["ok"] and len(r3["removed"]) == 1
        meta3 = sync.storage.load_metadata(pack)
        assert not [v for v in meta3["images"].values() if v.get(SIGN_FLAG)]
        assert not (pack / "memes" / SIGN_CATEGORY / "t1.png").exists()


def test_reconcile_integrated_then_standalone():
    with TemporaryDirectory() as tmp:
        td = Path(tmp)
        root, pack = _make_env(td)
        sync = SemanticPoolSync(root=root)
        img1, img2 = _image(td, "t1", b"a"), _image(td, "t2", b"b")
        resolver = lambda t: {"t1": img1, "t2": img2}.get(t["id"])
        # integrated: 两个模板入池
        r = sync.reconcile("integrated", [_template("t1"), _template("t2", "吐槽")], resolver)
        assert r["ok"] and sorted(r["added"]) == ["t1", "t2"]
        # 源删除 t1 → reconcile 清残留
        r2 = sync.reconcile("integrated", [_template("t2", "吐槽")], resolver)
        assert r2["ok"] and "t1" in r2["removed"]
        # 回 standalone: 全部出池
        r3 = sync.reconcile("standalone", [_template("t2", "吐槽")], resolver)
        assert r3["ok"] and "t2" in r3["removed"]
        meta = sync.storage.load_metadata(pack)
        assert not [v for v in meta["images"].values() if v.get(SIGN_FLAG)]


def test_default_pack_missing_graceful():
    with TemporaryDirectory() as tmp:
        sync = SemanticPoolSync(root=Path(tmp))
        assert sync.available() is False
        r = sync.upsert(_template(), Path(tmp) / "x.png")
        assert r["ok"] is False


def test_upsert_keeps_pack_totals_and_gate_consistent():
    """防回归(2026-09-10): upsert 后 metadata 顶层快照必须与磁盘一致。

    曾经只改 images 字典不重算 file_total/unique_total,导致
    semantic_metadata_is_complete() 永远 False,挡住 search_memes 门禁。
    """
    with TemporaryDirectory() as tmp:
        td = Path(tmp)
        root, pack = _make_env(td)
        sync = SemanticPoolSync(root=root)
        r = sync.upsert(_template(), _image(td))
        assert r["ok"]
        meta = sync.storage.load_metadata(pack)
        disk_files = [
            p for p in (pack / "memes").rglob("*")
            if p.is_file() and p.suffix.lower() in sync.storage.IMAGE_EXTENSIONS
        ]
        assert meta["file_total"] == len(disk_files) == len(meta["images"])
        assert meta["unique_total"] == len(meta["images"])
        assert meta["content_unique_total"] == len(
            {v["content_sha256"] for v in meta["images"].values()}
        )
        # meme_manager 完整性检查(快照维度)通过
        assert sync.storage.semantic_metadata_is_complete(
            pack, meta, require_embeddings=False
        )
        # 幂等 upsert 不重复计数
        sync.upsert(_template(caption="改"), _image(td))
        meta2 = sync.storage.load_metadata(pack)
        assert meta2["file_total"] == len(meta["images"])


def test_remove_restores_pack_totals():
    with TemporaryDirectory() as tmp:
        td = Path(tmp)
        root, pack = _make_env(td)
        sync = SemanticPoolSync(root=root)
        sync.upsert(_template(), _image(td))
        meta_after_upsert = sync.storage.load_metadata(pack)
        assert sync.remove("t1")["ok"]
        meta = sync.storage.load_metadata(pack)
        assert meta["file_total"] == meta_after_upsert["file_total"] - 1
        assert meta["unique_total"] == meta_after_upsert["unique_total"] - 1
        assert not meta["images"]  # 测试环境无普通记录


def test_upsert_rejects_blank_caption_without_orphan_file():
    """空 caption 模板拒绝入池,且不留孤儿镜像文件。"""
    with TemporaryDirectory() as tmp:
        td = Path(tmp)
        root, pack = _make_env(td)
        sync = SemanticPoolSync(root=root)
        r = sync.upsert(_template(caption=""), _image(td))
        assert r["ok"] is False and "caption" in r["reason"]
        assert not (pack / "memes" / SIGN_CATEGORY / "t1.png").exists()
        meta = sync.storage.load_metadata(pack)
        assert not [v for v in meta["images"].values() if v.get(SIGN_FLAG)]


def test_upsert_preserves_done_embedding_on_unchanged_text():
    """防回归(2026-09-10): 启动 reconcile 反复 upsert 不能把向量状态打回 pending。"""
    with TemporaryDirectory() as tmp:
        td = Path(tmp)
        root, pack = _make_env(td)
        sync = SemanticPoolSync(root=root)
        sync.upsert(_template(), _image(td))
        # 模拟向量重建完成后的记录状态(不伪造 context hash——
        # meme_manager 的规范化层会按真实 context hash 校验并向量一致性)
        meta = sync.storage.load_metadata(pack)
        eid = next(k for k, v in meta["images"].items() if v.get(SIGN_FLAG))
        meta["images"][eid].update({
            "embedding_status": "done",
            "text_hash": "a" * 64,
        })
        sync.storage.save_metadata(pack, meta)
        # 同内容重新 upsert(启动 reconcile 场景)
        r = sync.upsert(_template(), _image(td))
        assert r["ok"]
        meta2 = sync.storage.load_metadata(pack)
        entry2 = meta2["images"][eid]
        assert entry2["embedding_status"] == "done"
        assert entry2["text_hash"] == "a" * 64
        # caption 变了 → 必须重置为 pending(旧向量对应旧文本)
        sync.upsert(_template(caption="新文案"), _image(td))
        meta3 = sync.storage.load_metadata(pack)
        entry3 = meta3["images"][eid]
        assert entry3["embedding_status"] == "pending"


if __name__ == "__main__":
    test_build_entry_shape()
    test_upsert_and_remove_roundtrip()
    test_reconcile_integrated_then_standalone()
    test_default_pack_missing_graceful()
    test_upsert_keeps_pack_totals_and_gate_consistent()
    test_remove_restores_pack_totals()
    test_upsert_rejects_blank_caption_without_orphan_file()
    test_upsert_preserves_done_embedding_on_unchanged_text()
    print("semantic pool tests PASS (8)")
