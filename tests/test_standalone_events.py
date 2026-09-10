"""sign_meme 独立链路 + meme_manager 让位互斥测试。"""
import _bootstrap  # noqa: F401  路径引导(直跑时脚本目录自动入 path;pytest 由 conftest 处理)
import asyncio
import types

from backend.standalone_events import (
    SIGN_PROMPT_MARKER,
    build_sign_prompt_suffix,
    handle_decorating_result,
    handle_llm_request,
    handle_llm_response,
    parse_sign_response,
)


class _FakeConfig:
    def __init__(self, mode="standalone"):
        self.sign_mode = mode
        self.sign_text_llm_provider = ""
        self.sign_text_llm_model = ""


class _FakePlugin:
    def __init__(self, mode="standalone"):
        self.config = _FakeConfig(mode)
        self.generated = []
        self.cleaned = []

    @property
    def sign_mode(self):
        return self.config.sign_mode

    async def generate_for_meme_manager(self, sign_text, *, request_id=None):
        self.generated.append((sign_text, request_id))
        return {"ok": True, "template_id": "t1", "temporary_path": "/tmp/fake.png",
                "request_id": request_id}

    async def cleanup_generated(self, path):
        self.cleaned.append(path)
        return True

    def get_active_semantic_context(self):
        return {"caption": "评价", "tags": ["吐槽"], "visible_text": ""}


class _Extra(dict):
    def get_extra(self, k, d=None):
        return self.get(k, d)

    def set_extra(self, k, v):
        self[k] = v

    def get_result(self):
        return self.get("_result")


class _Req:
    def __init__(self):
        self.system_prompt = ""


class _Resp:
    def __init__(self, text):
        self.completion_text = text


class _Result:
    def __init__(self):
        self.chain = [types.SimpleNamespace(text="hi")]


def test_parse_variants():
    assert parse_sign_response('{"reply":"你好","sign_text":"好耶"}') == ("你好", "好耶")
    assert parse_sign_response("纯文本") is None
    assert parse_sign_response('废话{"reply":"正文","sign_text":""}尾巴') == ("正文", "")
    assert parse_sign_response('{"reply":123,"sign_text":"x"}') is None


def test_request_injection_standalone_only():
    p = _FakePlugin("standalone")
    req = _Req()
    asyncio.run(handle_llm_request(p, _Extra(), req))
    assert SIGN_PROMPT_MARKER in req.system_prompt and "评价" in req.system_prompt
    # 重复注入防重
    asyncio.run(handle_llm_request(p, _Extra(), req))
    assert req.system_prompt.count(SIGN_PROMPT_MARKER) == 1
    # integrated 不注入
    p2 = _FakePlugin("integrated")
    req2 = _Req()
    asyncio.run(handle_llm_request(p2, _Extra(), req2))
    assert req2.system_prompt == ""


def test_response_parse_and_coexistence_flag():
    p = _FakePlugin("standalone")
    ev = _Extra()
    resp = _Resp('{"reply":"回答","sign_text":"好"}')
    asyncio.run(handle_llm_response(p, ev, resp))
    assert resp.completion_text == "回答"
    assert ev["meme_manager_sign_text"] == "好"
    assert ev["meme_manager_sign_structured"] is True
    # meme_manager 已处理过 → 让位
    ev2 = _Extra(meme_manager_sign_structured=True)
    resp2 = _Resp('{"reply":"a","sign_text":"b"}')
    asyncio.run(handle_llm_response(p, ev2, resp2))
    assert resp2.completion_text == '{"reply":"a","sign_text":"b"}'
    # integrated 不解析
    p3 = _FakePlugin("integrated")
    ev3 = _Extra()
    resp3 = _Resp('{"reply":"回答","sign_text":"好"}')
    asyncio.run(handle_llm_response(p3, ev3, resp3))
    assert resp3.completion_text == '{"reply":"回答","sign_text":"好"}'
    assert "meme_manager_sign_text" not in ev3


def test_decorating_renders_once_and_integrated_skips(monkeypatch=None):
    from backend import standalone_events as se
    from astrbot.core.message.components import Image as _Img  # 容器内可用

    p = _FakePlugin("standalone")
    ev = _Extra()
    ev["meme_manager_sign_text"] = "举牌！"
    ev["_result"] = _Result()
    from pathlib import Path as _P

    se.Path = _P  # 确保用真实 Path
    # 伪造 temporary_path 存在
    import tempfile, os
    with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as f:
        f.write(b"x")
        tmp = f.name
    async def fake_gen(sign_text, *, request_id=None):
        return {"ok": True, "template_id": "t1", "temporary_path": tmp, "request_id": request_id}
    p.generate_for_meme_manager = fake_gen
    asyncio.run(handle_decorating_result(p, ev))
    assert len(ev["_result"].chain) == 2, "成品图应追加进链"
    os.unlink(tmp)

    # integrated 模式不动链
    p2 = _FakePlugin("integrated")
    ev2 = _Extra()
    ev2["meme_manager_sign_text"] = "举牌！"
    ev2["_result"] = _Result()
    asyncio.run(handle_decorating_result(p2, ev2))
    assert len(ev2["_result"].chain) == 1


def test_meme_manager_yields_to_self_managed():
    # 直接验证让位逻辑(不导入完整插件,模拟 mixin 方法所在类)
    import sys

    sys.path.insert(0, "/AstrBot/data/plugins")
    from astrbot_plugin_meme_manager.mixins.event_handlers import EventHandlerMixin

    class _Ctx:
        def get_registered_star(self, name):
            if name == "sign_meme":
                m = types.SimpleNamespace()
                m.star_cls = FakeSelfManaged()
                return m
            return None

    class FakeSelfManaged:
        self_managed_sign_events = True

        async def generate_for_meme_manager(self, *a, **k):
            return {"ok": True}

    class _Host(EventHandlerMixin):
        def __init__(self):
            self.context = _Ctx()

    host = _Host()
    assert host._sign_plugin_self_managed() is True
    req = _Req()
    host._append_sign_meme_prompt(req)
    assert req.system_prompt == "", "自管时旧链路不得注入"

    # 无自管能力(旧版 sign_meme) → 旧链路照常
    class _CtxOld(_Ctx):
        def get_registered_star(self, name):
            if name == "sign_meme":
                m = types.SimpleNamespace()
                m.star_cls = FakeOldPlugin()  # 无 self_managed 标志
                return m
            return None

    class FakeOldPlugin:
        async def generate_for_meme_manager(self, *a, **k):
            return {"ok": True}

    host2 = _Host()
    host2.context = _CtxOld()
    assert host2._sign_plugin_self_managed() is False
    req2 = _Req()
    host2._append_sign_meme_prompt(req2)
    assert SIGN_PROMPT_MARKER in req2.system_prompt, "旧版插件时旧链路应注入"


if __name__ == "__main__":
    test_parse_variants()
    test_request_injection_standalone_only()
    test_response_parse_and_coexistence_flag()
    test_decorating_renders_once_and_integrated_skips()
    test_meme_manager_yields_to_self_managed()
    print("standalone/yield tests PASS (5)")
