"""语义池 pending 向量队列测试: 登记/去重/取出。

背景: 模板编辑 caption/tags 后,语义池 upsert 把向量状态打回 pending,
但 FAISS 重建只挂在模式切换——编辑后检索用的还是旧向量。修复后由
service 队列登记、main.py 在创建/更新 API 成功后异步消费。
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from backend.service import SignMemeService  # noqa: E402


def _make_service(tmp_path: Path) -> SignMemeService:
    return SignMemeService(tmp_path / "sign_meme")


def test_queue_register_and_dedup(tmp_path):
    service = _make_service(tmp_path)
    service._queue_vector_rebuild("meme:aaaa")
    service._queue_vector_rebuild("meme:bbbb")
    service._queue_vector_rebuild("meme:aaaa")  # 重复登记必须去重
    assert service.drain_pending_vector_ids() == ["meme:aaaa", "meme:bbbb"]


def test_drain_empties_queue(tmp_path):
    service = _make_service(tmp_path)
    service._queue_vector_rebuild("meme:cccc")
    assert service.drain_pending_vector_ids() == ["meme:cccc"]
    assert service.drain_pending_vector_ids() == [], "二次 drain 必须为空"


def test_drain_empty_queue_no_io(tmp_path):
    service = _make_service(tmp_path)
    assert service.drain_pending_vector_ids() == []


def test_needs_vector_false_when_text_unchanged_and_done(tmp_path):
    """语义文本未变且向量已完成: upsert 必须返回 needs_vector=False(省 API)。"""
    service = _make_service(tmp_path)
    # 注入假池: 模拟"同 entry 二次 upsert,文本未变,向量 done"

    def fake_upsert(template, image_path):
        return {"ok": True, "entry_id": "meme:deadbeef", "needs_vector": False}

    class _Pool:
        def upsert(self, template, image_path):
            return fake_upsert(template, image_path)

    service.semantic_pool = _Pool()
    service._sign_mode = lambda: "integrated"  # type: ignore[assignment]
    service._pool_sync_template(
        {
            "id": "tpl1",
            "relative_path": "templates/tpl1.png",
            "caption": "x",
            "tags": ["y"],
        }
    )
    assert service.drain_pending_vector_ids() == [], "needs_vector=False 不得入队"

if __name__ == "__main__":
    import tempfile

    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        test_queue_register_and_dedup(root / "a")
        test_drain_empties_queue(root / "b")
        test_drain_empty_queue_no_io(root / "c")
        test_needs_vector_false_when_text_unchanged_and_done(root / "d")
    print("4 pending-vector queue tests passed")
