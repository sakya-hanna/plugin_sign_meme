"""compat 层测试: 上游探测与公开函数导入。"""
import json
from pathlib import Path
from tempfile import TemporaryDirectory

import _bootstrap  # noqa: F401  路径引导

from backend import compat


def _make_upstream_layout(td: Path) -> Path:
    """构造最小官方 meme_manager 布局。"""
    root = td / "plugins" / "astrbot_plugin_meme_manager"
    (root / "backend").mkdir(parents=True)
    (root / "backend" / "semantic_storage.py").write_text(
        "# marker\n", encoding="utf-8"
    )
    (root / "metadata.yaml").write_text(
        "name: astrbot_plugin_meme_manager\nversion: 4.17.0\n", encoding="utf-8"
    )
    pdd = td / "plugin_data" / "meme_manager"
    pack = pdd / "packs" / "main-pack"
    (pack / "memes").mkdir(parents=True)
    (pdd / "selection_rules.json").write_text(
        json.dumps({"rules": [{"id": "d", "scope": "default", "pack_id": "main-pack"}]}),
        encoding="utf-8",
    )
    (pack / "semantic_metadata.json").write_text(
        json.dumps({"images": {}}), encoding="utf-8"
    )
    return td


def test_probe_returns_none_on_missing_upstream():
    with TemporaryDirectory() as tmp:
        td = Path(tmp)
        # 传入显式不存在的 plugins_root,且禁用自动回退(测试隔离)
        info = compat.probe_upstream(
            td / "plugins", td / "plugin_data" / "meme_manager", allow_fallback=False
        )
        assert info is None, "上游不存在时必须返回 None(触发降级)"


def test_probe_returns_version_and_pack_dir():
    with TemporaryDirectory() as tmp:
        td = _make_upstream_layout(Path(tmp))
        info = compat.probe_upstream(
            td / "plugins", td / "plugin_data" / "meme_manager", allow_fallback=False
        )
        assert info is not None
        assert info.version == "4.17.0"
        assert info.default_pack_dir is not None
        assert info.default_pack_dir.name == "main-pack"


def test_resolve_provider_mismatch_returns_none():
    """provider signature 与索引 manifest 不一致时必须返回 None,禁止错模型重建。"""
    with TemporaryDirectory() as tmp:
        td = Path(tmp)
        pdd = td / "plugin_data" / "meme_manager"
        pack = pdd / "packs" / "main-pack"
        idx = pdd / "semantic_indexes" / "main-pack"
        idx.mkdir(parents=True)

        class _FakeAdapter:
            def __init__(self, provider, provider_id=""):
                self.provider = provider
                self.provider_id = provider_id or getattr(provider, "meta_id", "")
                self.model_name = "text-embedding-v4"
                self.dimension = 1024
            @property
            def signature(self):
                return f"{self.provider_id}:{self.model_name}:{self.dimension}"
            @property
            def ready(self):
                return True

        # manifest 声明的是 another_model
        (idx / "index_manifest.json").write_text(
            json.dumps({
                "embedding_provider_id": "other_embed",
                "embedding_model": "other-model",
                "embedding_dimension": 1024,
                "items": {},
            }),
            encoding="utf-8",
        )
        (pdd / "semantic_indexes" / "main-pack" / "provider_selection.json").write_text(
            json.dumps({"effective_provider_id": "other_embed"}),
            encoding="utf-8",
        )

        class _FakeCtx:
            def get_provider_by_id(self, pid):
                return object()

        result = compat.resolve_embedding_provider_for_pack(
            _FakeCtx(), pack, pdd, adapter_cls=_FakeAdapter
        )
        assert result is None, "签名不一致必须返回 None"


def test_resolve_provider_match_returns_provider():
    with TemporaryDirectory() as tmp:
        td = Path(tmp)
        pdd = td / "plugin_data" / "meme_manager"
        pack = pdd / "packs" / "main-pack"
        idx = pdd / "semantic_indexes" / "main-pack"
        idx.mkdir(parents=True)

        class _FakeAdapter:
            def __init__(self, provider, provider_id=""):
                self.provider = provider
                self.provider_id = provider_id or getattr(provider, "meta_id", "")
                self.model_name = "text-embedding-v4"
                self.dimension = 1024
            @property
            def signature(self):
                return f"{self.provider_id}:{self.model_name}:{self.dimension}"
            @property
            def ready(self):
                return True

        (idx / "index_manifest.json").write_text(
            json.dumps({
                "embedding_provider_id": "my_embed",
                "embedding_model": "text-embedding-v4",
                "embedding_dimension": 1024,
                "items": {},
            }),
            encoding="utf-8",
        )
        (pdd / "semantic_indexes" / "main-pack" / "provider_selection.json").write_text(
            json.dumps({"effective_provider_id": "my_embed"}),
            encoding="utf-8",
        )
        sent = {}

        class _FakeCtx:
            def get_provider_by_id(self, pid):
                sent["pid"] = pid
                return ("provider-object", pid)

        result = compat.resolve_embedding_provider_for_pack(
            _FakeCtx(), pack, pdd, adapter_cls=_FakeAdapter
        )
        assert result is not None
        provider, adapter = result
        assert sent["pid"] == "my_embed"
        assert provider == ("provider-object", "my_embed")
        assert adapter.signature == "my_embed:text-embedding-v4:1024"


if __name__ == "__main__":
    test_probe_returns_none_on_missing_upstream()
    test_probe_returns_version_and_pack_dir()
    test_resolve_provider_mismatch_returns_none()
    test_resolve_provider_match_returns_provider()
    print("compat tests PASS (4)")
