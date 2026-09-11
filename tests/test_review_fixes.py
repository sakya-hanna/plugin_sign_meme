"""2026-09-11 code review 修复回归测试。

对应 docs/code-review-2026-09-11.md:
- H1 integrated 临时图泄漏(decorating 提前置空 → after_message_sent 永不清理)
- H2 降级链路失效(事件模块内部 guard 读配置值而非 effective 值)
- H3 staging/generated TTL 兜底清扫
- M2 update/set_active 镜像失败时 DB 不落新值
"""
import asyncio
import json
import os
import tempfile
import time
import types
from io import BytesIO
from pathlib import Path
from tempfile import TemporaryDirectory

import _bootstrap  # noqa: F401  路径引导

from PIL import Image

from backend import integrated_events as ie
from backend import standalone_events as se
from backend.service import SignMemeError, SignMemeService


# ---------- 通用假件 ----------

class _Extra(dict):
    def get_extra(self, key, default=None):
        return dict.get(self, key, default)

    def set_extra(self, key, value):
        self[key] = value


class _Result:
    def __init__(self):
        self.chain = ["__keep__"]


class _Event(_Extra):
    def __init__(self):
        super().__init__()
        self._result = _Result()

    def get_result(self):
        return self._result


def _png_bytes(size=(100, 100)):
    buf = BytesIO()
    Image.new("RGB", size, "white").save(buf, "PNG")
    return buf.getvalue()


# ---------- H1: integrated 追加成功后才置空清理标记 ----------

def test_h1_rendered_path_survives_until_cleanup():
    """decorating 追加成功后 EXTRA_RENDERED_PATH 必须仍在 → after_message_sent 可清理。"""
    plugin = types.SimpleNamespace(
        sign_mode="integrated",
        cleanup_generated=lambda path: _ok_clean(path),
    )
    ev = _Event()
    ev.set_extra(ie.EXTRA_RENDERED_PATH, "/tmp/fake_sign.png")
    ev.set_extra(ie.EXTRA_RENDERED_REQUEST_ID, "req-1")
    ev.set_extra("meme_manager_semantic_selected_ids", ["meme:x"])

    asyncio.run(ie.on_decorating_result_first(plugin, ev))

    assert len(ev._result.chain) == 2, "成品图应追加进链"
    # H1 核心断言: 追加成功后待清理标记必须保留,交给 after_message_sent 消费
    assert ev.get_extra(ie.EXTRA_RENDERED_PATH) == "/tmp/fake_sign.png", (
        "H1 回归: decorating 不得提前清掉待清理标记(会导致临时图泄漏)"
    )


def _ok_clean(path):
    async def _inner(p):
        return True
    return _inner(path)


def test_h1_append_failure_discards_temp_file():
    """追加失败(无结果链)时必须立即清理临时文件并清标记,不留泄漏。"""
    cleaned = []

    async def fake_clean(path):
        cleaned.append(path)
        return True

    plugin = types.SimpleNamespace(sign_mode="integrated", cleanup_generated=fake_clean)
    ev = _Event()
    ev.set_extra(ie.EXTRA_RENDERED_PATH, "/tmp/fake_sign.png")
    ev._result = None  # get_result() 返回 None → 无可附加结果链

    asyncio.run(ie.on_decorating_result_first(plugin, ev))

    assert ev.get_extra(ie.EXTRA_RENDERED_PATH) is None, "失败路径必须清掉待清理标记"
    assert cleaned == ["/tmp/fake_sign.png"], "失败路径必须立即清理临时文件"


def test_h1_full_roundtrip_cleanup_called():
    """端到端: decorating 追加 → after_message_sent 必须收到清理调用。"""
    cleaned = []

    async def fake_clean(path):
        cleaned.append(path)
        return True

    plugin = types.SimpleNamespace(sign_mode="integrated", cleanup_generated=fake_clean)
    ev = _Event()
    ev.set_extra(ie.EXTRA_RENDERED_PATH, "/tmp/fake_sign.png")

    async def _roundtrip():
        await ie.on_decorating_result_first(plugin, ev)
        await ie.after_message_sent(plugin, ev)

    asyncio.run(_roundtrip())
    assert cleaned == ["/tmp/fake_sign.png"], (
        "H1 端到端回归: 追加成功后 after_message_sent 必须能清理临时文件"
    )


# ---------- H2: 降级场景事件链路必须工作 ----------

class _DegradePlugin:
    """配置 integrated 但上游不可用 → effective=standalone 的插件快照。"""

    def __init__(self):
        self.config = types.SimpleNamespace(
            sign_mode="integrated", sign_text_llm_provider="", sign_text_llm_model=""
        )
        self.generated = []
        self.cleaned = []

    @property
    def sign_mode(self):
        return self.config.sign_mode

    def _effective_sign_mode(self):
        return "standalone"  # 模拟上游 missing 时的 effective 判定

    def get_active_semantic_context(self):
        return {"caption": "评价", "tags": ["吐槽"], "visible_text": ""}

    async def generate_for_meme_manager(self, sign_text, *, request_id=None):
        self.generated.append((sign_text, request_id))
        return {"ok": True, "template_id": "t1", "temporary_path": "/tmp/fake.png",
                "request_id": request_id}

    async def cleanup_generated(self, path):
        self.cleaned.append(path)
        return True


class _Req:
    def __init__(self):
        self.system_prompt = ""


def test_h2_degraded_request_injection_works():
    """降级(integrated 配置+standalone effective)时协议注入不得被 guard 挡掉。"""
    plugin = _DegradePlugin()
    req = _Req()
    asyncio.run(se.handle_llm_request(plugin, _Event(), req))
    assert se.SIGN_PROMPT_MARKER in req.system_prompt, (
        "H2 回归: 降级期间必须按 effective 模式注入协议(此前被配置值 guard 挡掉)"
    )


def test_h2_degraded_decorating_renders():
    """降级时 decorating 必须照常渲染(此前被配置值 guard 直接 return)。"""
    import tempfile as _tf

    plugin = _DegradePlugin()
    ev = _Event()
    ev.set_extra(se.EXTRA_SIGN_TEXT, "举牌！")
    with _tf.NamedTemporaryFile(suffix=".png", delete=False) as f:
        f.write(b"x")
        tmp = f.name
    async def fake_gen(sign_text, *, request_id=None):
        plugin.generated.append((sign_text, request_id))
        return {"ok": True, "template_id": "t1", "temporary_path": tmp,
                "request_id": request_id}
    plugin.generate_for_meme_manager = fake_gen
    try:
        asyncio.run(se.handle_decorating_result(plugin, ev))
        assert plugin.generated, "H2 回归: 降级期间渲染不得被 guard 挡掉"
    finally:
        os.unlink(tmp)


def test_h2_degraded_response_writes_standalone_extra():
    """降级时剥离出的 sign_text 必须写 EXTRA_SIGN_TEXT(standalone 渲染链消费的 key)。"""
    plugin = _DegradePlugin()

    class _Resp:
        completion_text = '{"reply":"回答","sign_text":"好"}'

    ev = _Event()
    asyncio.run(se.handle_llm_response(plugin, ev, _Resp()))
    assert ev.get_extra(se.EXTRA_SIGN_TEXT) == "好", (
        "H2 回归: 降级期间应写 standalone extra,否则解析出的牌面文字无人渲染"
    )
    assert ev.get_extra(se.EXTRA_MODEL_SIGN_TEXT) is None


# ---------- H3: TTL sweep ----------

def test_h3_sweep_expired_removes_only_stale_temp_files():
    with TemporaryDirectory() as td:
        service = SignMemeService(td)
        old = Path(td) / "generated" / "old.png"
        fresh = Path(td) / "generated" / "fresh.png"
        stale_staging = Path(td) / "staging" / "stale.bin"
        keep_template = Path(td) / "templates" / "keep.png"
        for p, age in ((old, 48 * 3600), (fresh, 60), (stale_staging, 48 * 3600),
                       (keep_template, 48 * 3600)):
            p.write_bytes(b"x")
            past = time.time() - age
            os.utime(p, (past, past))
        removed = service.sweep_expired()
        assert removed == {"staging": 1, "generated": 1}
        assert not old.exists() and not stale_staging.exists()
        assert fresh.exists(), "TTL 内的文件不得清理"
        assert keep_template.exists(), "templates/ 目录永不触碰"


def test_h3_sweep_never_touches_saved_or_templates():
    with TemporaryDirectory() as td:
        service = SignMemeService(td)
        saved = Path(td) / "saved" / "p.png"
        saved.write_bytes(b"x")
        old = time.time() - 72 * 3600
        os.utime(saved, (old, old))
        service.sweep_expired()
        assert saved.exists(), "saved/ 目录永不触碰"


# ---------- M2: 镜像失败时 DB 不落新值 ----------

def test_m2_update_rollback_keeps_db_old_value_on_mirror_failure():
    with TemporaryDirectory() as td:
        root = Path(td)
        service = SignMemeService(root / "plugin_data" / "sign_meme")
        item = service.create_template(
            name="初始", description="", caption="初始语义", tags=["初始"],
            image_bytes=_png_bytes(), original_name="a.png",
            rect={"x": 10, "y": 10, "width": 80, "height": 80},
        )
        tid = item["id"]

        def _boom(template, source):
            raise RuntimeError("mirror down")

        service.meme_manager_mirror.sync = _boom
        try:
            service.update_template(
                tid, name="已更新", description="", caption="新语义", tags=["新"],
                rect={"x": 10, "y": 10, "width": 80, "height": 80},
            )
        except SignMemeError as exc:
            assert exc.code == "mirror_failed"
        else:
            raise AssertionError("镜像失败时 update 必须抛 mirror_failed")
        # M2 核心断言: DB 保持旧值(名称/版本均未变)
        db_item = service.get_template(tid)
        assert db_item["name"] == "初始", "镜像失败后 DB 名称不得更新"
        assert db_item["version"] == 1, "镜像失败后 DB 版本不得递增"
        assert db_item["caption"] == "初始语义"


def test_m2_set_active_rollback_keeps_db_old_value_on_mirror_failure():
    with TemporaryDirectory() as td:
        root = Path(td)
        service = SignMemeService(root / "plugin_data" / "sign_meme")
        first = service.create_template(
            name="甲", description="", image_bytes=_png_bytes(), original_name="a.png",
            rect={"x": 10, "y": 10, "width": 80, "height": 80},
        )
        second = service.create_template(
            name="乙", description="", image_bytes=_png_bytes(), original_name="b.png",
            rect={"x": 10, "y": 10, "width": 80, "height": 80},
        )

        def _boom(template, source):
            raise RuntimeError("mirror down")

        service.meme_manager_mirror.sync = _boom
        try:
            service.set_active(second["id"])
        except SignMemeError:
            pass
        else:
            raise AssertionError("镜像失败时 set_active 必须抛 mirror_failed")
        db_items = {x["name"]: x["active"] for x in service.list_templates()}
        assert db_items == {"甲": True, "乙": False}, (
            "镜像失败后激活状态必须保持原样(甲仍激活)"
        )


# ---------- M1(服务层语义): text_overflow 从 _render_with_template 穿透 ----------

def test_m1_text_overflow_signmemeerror_propagates():
    """确认 api_generate 捕获的那个异常确实会从 service 层抛出。"""
    with TemporaryDirectory() as td:
        service = SignMemeService(td)
        service.create_template(
            name="小牌面", description="", image_bytes=_png_bytes(), original_name="a.png",
            rect={"x": 0, "y": 0, "width": 16, "height": 16},
        )
        try:
            service.generate("四个字的牌面", request_id="m1")
        except SignMemeError as exc:
            assert exc.code == "text_overflow"
        else:
            raise AssertionError("小牌面+文本应触发 text_overflow")


# ---------- L3: current_status 公共接口 ----------

def test_l3_current_status_public_api():
    from backend import upstream_watch as uw

    uw.configure(ttl_seconds=60)
    assert uw.current_status() is None
    status, _ = uw.refresh_state(None, Path("/nonexistent"), require_pack=False, force=True)
    assert uw.current_status() is status


if __name__ == "__main__":
    test_h1_rendered_path_survives_until_cleanup()
    test_h1_append_failure_discards_temp_file()
    test_h1_full_roundtrip_cleanup_called()
    test_h2_degraded_request_injection_works()
    test_h2_degraded_decorating_renders()
    test_h2_degraded_response_writes_standalone_extra()
    test_h3_sweep_expired_removes_only_stale_temp_files()
    test_h3_sweep_never_touches_saved_or_templates()
    test_m2_update_rollback_keeps_db_old_value_on_mirror_failure()
    test_m2_set_active_rollback_keeps_db_old_value_on_mirror_failure()
    test_m1_text_overflow_signmemeerror_propagates()
    test_l3_current_status_public_api()
    print("review-fix regression tests PASS (12)")
