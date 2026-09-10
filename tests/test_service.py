import _bootstrap  # noqa: F401  路径引导(直跑时脚本目录自动入 path;pytest 由 conftest 处理)
from io import BytesIO
from pathlib import Path
from tempfile import TemporaryDirectory

from PIL import Image

from backend.service import SignMemeError, SignMemeService


def test_create_activate_generate_and_cleanup():
    with TemporaryDirectory() as td:
        service = SignMemeService(td)
        base = Image.new("RGBA", (800, 500), (240, 240, 240, 255))
        buf = BytesIO()
        base.save(buf, "PNG")
        item = service.create_template(
            name="测试举牌",
            description="测试",
            caption="适合表达鼓励和开心",
            tags=["鼓励", "开心", "调皮"],
            image_bytes=buf.getvalue(),
            original_name="base.png",
            rect={"x": 120, "y": 80, "width": 560, "height": 340},
        )
        assert item["rect"] == {"x": 120, "y": 80, "width": 560, "height": 340}
        assert item["caption"] == "适合表达鼓励和开心"
        assert item["tags"] == ["鼓励", "开心", "调皮"]
        assert item["visible_text"] == ""
        assert item["image_url"] == (
            f"/api/v1/plugins/extensions/sign_meme/image/{item['id']}"
        )
        assert service.active_semantic_context()["tags"] == ["鼓励", "开心", "调皮"]
        assert item["active"] is True
        result = service.generate("测试中文举牌", request_id="test-request")
        assert result["ok"] is True
        path = Path(result["temporary_path"])
        assert path.is_file()
        with Image.open(path) as rendered:
            assert rendered.size == (800, 500)
            assert rendered.format == "PNG"
        assert service.cleanup(str(path)) is True
        assert not path.exists()


def test_single_line_auto_fit_prefers_no_wrap():
    """排版规则(2026-09-10): 字号自适应优先单行,只有放不下才换行。"""
    from PIL import ImageDraw

    with TemporaryDirectory() as td:
        service = SignMemeService(td)
        # 560x340 牌面,6 个字很短——旧行为会从 170 号开始,单行必然超宽,
        # 直接折行;新行为应缩小字号到单行放得下。
        layer = service._render_text_layer("才没有等你！", 560, 340)
        # 验证单行: 在垂直中线附近只有一行像素带
        alpha = layer.split()[3]
        rows_with_ink = [
            y for y in range(layer.height)
            if alpha.getpixel((layer.width // 2, y)) > 0
        ]
        # 中线单列采样: 单行时应存在连续墨迹区间,且高度 < 半个牌面
        assert rows_with_ink, "文字未渲染"
        # 连续段检测: 单行时所有墨迹行应连续(间距<字号)
        gaps = [b - a for a, b in zip(rows_with_ink, rows_with_ink[1:]) if b - a > 30]
        assert not gaps, f"出现了多行(行间距>{30}px): {gaps}"

        # 长文本: 30 字在 560px 宽单行放不下,应兜底多行而不是 text_overflow
        long_text = "这是一条用来验证换行兜底逻辑的三十个字符长句号结束"
        layer2 = service._render_text_layer(long_text, 560, 340)
        assert layer2 is not None

        # 短文本在窄牌面: 3 个字在 100px 宽内仍能单行(字号缩小)
        layer3 = service._render_text_layer("你好呀", 100, 100)
        assert layer3 is not None


def test_plugin_page_bridge_contract():
    page = (Path(__file__).parent.parent / "pages" / "举牌模板" / "index.html").read_text(encoding="utf-8")
    assert '<script src="/api/plugin/page/bridge-sdk.js"></script>' in page
    assert 'upload: async()=>({})' not in page
    assert 'plugin_page_bridge_unavailable' in page


def test_page_shows_compat_status():
    """零修改方案: 页面必须展示兼容状态(降级提示),消费 mode API 新字段。"""
    page = (Path(__file__).parent.parent / "pages" / "举牌模板" / "index.html").read_text(encoding="utf-8")
    assert "compat_status" in page, "页面必须读取 compat_status"
    assert "compat_upstream_version" in page, "页面必须展示上游版本"
    assert "已降级" in page, "必须有降级提示文案"


def test_create_template_mirrors_to_meme_manager_pack():
    with TemporaryDirectory() as td:
        root = Path(td)
        service = SignMemeService(root / "plugin_data" / "sign_meme")
        base = Image.new("RGB", (100, 100), "white")
        buf = BytesIO(); base.save(buf, "PNG")
        item = service.create_template(
            name="镜像模板", description="", caption="用于鼓励", tags=["鼓励"],
            image_bytes=buf.getvalue(), original_name="mirror.png",
            rect={"x": 10, "y": 10, "width": 80, "height": 80},
        )
        mirrored = root / "plugin_data" / "meme_manager" / "packs" / "sign-meme-templates" / "memes" / "举牌模板" / f"{item['id']}.png"
        assert mirrored.is_file()


def test_update_and_delete_keep_mirror_in_sync():
    with TemporaryDirectory() as td:
        root = Path(td)
        service = SignMemeService(root / "plugin_data" / "sign_meme")
        buf = BytesIO(); Image.new("RGB", (100, 100), "white").save(buf, "PNG")
        item = service.create_template(name="初始", description="", image_bytes=buf.getvalue(), original_name="a.png", rect={"x": 10, "y": 10, "width": 80, "height": 80})
        service.update_template(item["id"], name="已更新", description="", caption="更新说明", tags=["更新"], rect={"x": 10, "y": 10, "width": 80, "height": 80})
        mirror_root = root / "plugin_data" / "meme_manager" / "packs" / "sign-meme-templates"
        metadata = __import__("json").loads((mirror_root / "sign_meme_mirror.json").read_text(encoding="utf-8"))
        assert metadata["templates"][item["id"]]["caption"] == "更新说明"
        other = service.create_template(name="其他", description="", image_bytes=buf.getvalue(), original_name="b.png", rect={"x": 10, "y": 10, "width": 80, "height": 80})
        service.set_active(other["id"])
        metadata = __import__("json").loads((mirror_root / "sign_meme_mirror.json").read_text(encoding="utf-8"))
        assert metadata["templates"][other["id"]]["active"] is True
        assert metadata["templates"][item["id"]]["active"] is False
        service.delete_template(item["id"])
        assert not (mirror_root / "memes" / "举牌模板" / f"{item['id']}.png").exists()


def test_single_active_and_overflow():
    with TemporaryDirectory() as td:
        service = SignMemeService(td)
        base = Image.new("RGB", (100, 100), "white")
        buf = BytesIO(); base.save(buf, "PNG")
        first = service.create_template(name="一", description="", image_bytes=buf.getvalue(), original_name="a.png", quad=[[10,10],[90,10],[90,90],[10,90]])
        second = service.create_template(name="二", description="", image_bytes=buf.getvalue(), original_name="b.png", quad=[[10,10],[90,10],[90,90],[10,90]])
        assert first["active"] is True and second["active"] is False
        service.set_active(second["id"])
        active = [x for x in service.list_templates() if x["active"]]
        assert [x["id"] for x in active] == [second["id"]]
        result = service.generate("x" * 41)
        assert result["skipped"] is True and result["error_code"] == "text_overflow"
        try:
            service.delete_template(second["id"])
        except SignMemeError as exc:
            assert exc.code == "active_template"
        else:
            raise AssertionError("active template deletion should fail")



def test_generate_with_template_ignores_active_state():
    """对接模式渲染: 指定模板 ID 渲染,不依赖/不改变激活状态。"""
    with TemporaryDirectory() as td:
        service = SignMemeService(td)
        base = Image.new("RGBA", (800, 500), (240, 240, 240, 255))
        buf = BytesIO(); base.save(buf, "PNG")
        first = service.create_template(name="甲", description="", caption="", tags=[],
            image_bytes=buf.getvalue(), original_name="a.png",
            rect={"x": 100, "y": 100, "width": 600, "height": 300})
        second = service.create_template(name="乙", description="", caption="", tags=[],
            image_bytes=buf.getvalue(), original_name="b.png",
            rect={"x": 50, "y": 50, "width": 500, "height": 250})
        service.set_active(first["id"])  # 激活的是"甲"
        # 用"乙"的 ID 渲染(非激活)
        result = service.generate_with_template(second["id"], "指定模板测试", request_id="t-req")
        assert result["ok"] is True
        assert result["template_id"] == second["id"]
        with Image.open(result["temporary_path"]) as rendered:
            assert rendered.size == (800, 500)
        # 激活状态未被改变
        assert service.active_template()["id"] == first["id"]
        service.cleanup(result["temporary_path"])
        # 不存在的模板
        missing = service.generate_with_template("nonexistent", "x")
        assert missing["ok"] is False and missing["error_code"] == "template_not_found"


if __name__ == "__main__":
    test_create_activate_generate_and_cleanup()
    test_plugin_page_bridge_contract()
    test_create_template_mirrors_to_meme_manager_pack()
    test_update_and_delete_keep_mirror_in_sync()
    test_single_active_and_overflow()
    test_single_line_auto_fit_prefers_no_wrap()
    test_generate_with_template_ignores_active_state()
    print("7 service/page tests passed")
