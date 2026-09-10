import _bootstrap  # noqa: F401  路径引导(直跑时脚本目录自动入 path;pytest 由 conftest 处理)
import hashlib
import json
from io import BytesIO
from pathlib import Path
from tempfile import TemporaryDirectory

from PIL import Image

from backend.meme_manager_mirror import MemeManagerMirror


PACK_ID = "sign-meme-templates"
TEMPLATE_ID = "template-a"


def _png_bytes(color="white"):
    image = Image.new("RGB", (48, 32), color)
    output = BytesIO()
    image.save(output, "PNG")
    return output.getvalue()


def _template(template_id=TEMPLATE_ID, *, name="模板 A", caption="鼓励", tags=None, active=True):
    return {
        "id": template_id,
        "name": name,
        "caption": caption,
        "tags": tags or ["鼓励"],
        "active": active,
        "rect": {"x": 1, "y": 2, "width": 30, "height": 20},
        "updated_at": 123,
    }


def test_sync_creates_browsable_pack_without_changing_selection_rules():
    with TemporaryDirectory() as td:
        root = Path(td)
        manager_dir = root / "meme_manager"
        rules = manager_dir / "selection_rules.json"
        rules.parent.mkdir(parents=True)
        rules.write_text(json.dumps({"rules": [{"scope": "default", "pack_id": "manosaba-001"}]}), encoding="utf-8")
        source = root / "source.png"
        source_bytes = _png_bytes()
        source.write_bytes(source_bytes)

        mirror = MemeManagerMirror(manager_dir)
        mirror.sync(_template(), source)

        pack = manager_dir / "packs" / PACK_ID
        assert (pack / "memes" / "举牌模板" / f"{TEMPLATE_ID}.png").read_bytes() == source_bytes
        registry = json.loads((manager_dir / "registry.json").read_text(encoding="utf-8"))
        assert registry["installed_packs"] == [{"id": PACK_ID, "name": "举牌模板", "version": "1.0.0", "enabled": True}]
        metadata = json.loads((pack / "sign_meme_mirror.json").read_text(encoding="utf-8"))
        assert metadata["templates"][TEMPLATE_ID]["sha256"] == hashlib.sha256(source_bytes).hexdigest()
        assert metadata["templates"][TEMPLATE_ID]["caption"] == "鼓励"
        assert json.loads(rules.read_text(encoding="utf-8"))["rules"][0]["pack_id"] == "manosaba-001"


def test_remove_only_deletes_matching_template_mirror():
    with TemporaryDirectory() as td:
        root = Path(td)
        mirror = MemeManagerMirror(root / "meme_manager")
        first = root / "one.png"; first.write_bytes(_png_bytes("white"))
        second = root / "two.png"; second.write_bytes(_png_bytes("black"))
        mirror.sync(_template("one"), first)
        mirror.sync(_template("two"), second)

        mirror.remove("one")

        pack = root / "meme_manager" / "packs" / PACK_ID
        assert not (pack / "memes" / "举牌模板" / "one.png").exists()
        assert (pack / "memes" / "举牌模板" / "two.png").is_file()
        metadata = json.loads((pack / "sign_meme_mirror.json").read_text(encoding="utf-8"))
        assert sorted(metadata["templates"]) == ["two"]


if __name__ == "__main__":
    test_sync_creates_browsable_pack_without_changing_selection_rules()
    test_remove_only_deletes_matching_template_mirror()
    print("2 mirror tests passed")
