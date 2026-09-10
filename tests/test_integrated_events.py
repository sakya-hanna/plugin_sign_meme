"""integrated 拦截管线测试: 四口子覆盖 + 失败路径。

模拟官方 meme_manager 的 extras 约定(只依赖 key 名,不依赖上游代码):
- meme_manager_semantic_candidates: {id: {category, caption, ...}}
- meme_manager_semantic_default_id: str
- meme_manager_semantic_selected_ids: list[str]
- meme_manager_semantic_search_completed: bool
"""
import asyncio
import types

import _bootstrap  # noqa: F401  路径引导

from backend import integrated_events as ie


class _Extra(dict):
    """dict 式 event extra,模拟 AstrMessageEvent.get/set_extra。"""

    def get_extra(self, key, default=None):
        return dict.get(self, key, default)

    def set_extra(self, key, value):
        self[key] = value


class _Resp:
    def __init__(self, text):
        self.completion_text = text


class _Result:
    def __init__(self):
        self.chain = ["__keep__"]


class _Event(_Extra):
    def __init__(self, text="回复正文"):
        super().__init__()
        self._result = _Result()

    def get_result(self):
        return self._result


def _sign_candidate_map(meme_id="meme:a2bd8f1b47a4"):
    return {
        meme_id: {
            "id": meme_id,
            "category": "举牌模板",
            "caption": "评价场景",
            "tags": ["category:举牌模板", "吐槽"],
        }
    }


class _FakeService:
    def active_template(self):
        return {"id": "7aca29c8931f"}


class _FakePlugin:
    """模拟 main.py 插件: 记录渲染调用与清理调用。"""

    def __init__(self, mode="integrated", render_ok=True):
        self.config = types.SimpleNamespace(
            sign_mode=mode, sign_text_llm_provider="", sign_text_llm_model=""
        )
        self.service = _FakeService()
        self.render_calls = []
        self.cleanup_calls = []
        self._render_ok = render_ok
        self.self_managed_sign_events = False

    @property
    def sign_text_llm_provider(self):
        return self.config.sign_text_llm_provider

    @property
    def sign_text_llm_model(self):
        return self.config.sign_text_llm_model

    @property
    def sign_mode(self):
        return self.config.sign_mode

    async def render_for_meme_manager(self, template_id, sign_text, *, request_id=None):
        """与 main.py 真实公开接口同名同签名(防接口漂移回归)。"""
        self.render_calls.append((template_id, sign_text))
        if not self._render_ok:
            raise RuntimeError("render failed")
        return {
            "ok": True,
            "template_id": template_id,
            "temporary_path": "/tmp/fake_sign.png",
            "request_id": request_id,
        }

    async def cleanup_generated(self, path):
        self.cleanup_calls.append(path)
        return True


def _make_llm_response(text='{"sign_text": "你好呀"}'):
    async def fake_llm_generate(plugin, event, prompt, provider_id="", model=""):
        return _Resp(text)

    return fake_llm_generate


# ---------- 口子 A/B: tool 模式(response 100000) ----------

def test_tool_mode_sign_marker_stripped_and_rendered():
    plugin = _FakePlugin()
    ev = _Event()
    ev.set_extra("meme_manager_semantic_search_completed", True)
    ev.set_extra("meme_manager_semantic_candidates", _sign_candidate_map())
    ev.set_extra("meme_manager_semantic_default_id", "meme:a2bd8f1b47a4")
    resp = _Resp("哼，本小姐最可爱desuwa~\n\n&&meme:a2bd8f1b47a4&&")

    asyncio.run(
        ie.on_llm_response_first(plugin, ev, resp, llm_generate=_make_llm_response())
    )
    assert "meme:" not in resp.completion_text, "标记必须剥除"
    assert "哼" in resp.completion_text, "正文必须保留"
    assert ev.get_extra("meme_manager_semantic_default_id") == "", "default_id 必须清空(口子B)"
    assert plugin.render_calls, "必须触发渲染"
    assert ev.get_extra(ie.EXTRA_RENDERED_PATH) == "/tmp/fake_sign.png"


def test_tool_mode_normal_meme_untouched():
    """普通表情包候选(非举牌 category): 完全不动,交给上游。"""
    plugin = _FakePlugin()
    ev = _Event()
    ev.set_extra(
        "meme_manager_semantic_candidates",
        {
            "meme:9c8681a9b10c": {
                "id": "meme:9c8681a9b10c",
                "category": "selected",
                "caption": "普通表情",
                "tags": ["开心"],
            }
        },
    )
    ev.set_extra("meme_manager_semantic_default_id", "meme:9c8681a9b10c")
    resp = _Resp("开心\n\n&&meme:9c8681a9b10c&&")

    asyncio.run(ie.on_llm_response_first(plugin, ev, resp))
    assert "&&meme:9c8681a9b10c&&" in resp.completion_text, "普通标记必须保留给上游"
    assert ev.get_extra("meme_manager_semantic_default_id") == "meme:9c8681a9b10c"
    assert not plugin.render_calls


def test_tool_mode_no_candidates_no_action():
    plugin = _FakePlugin()
    ev = _Event()
    resp = _Resp("普通回复 &&meme:a2bd8f1b47a4&&")

    asyncio.run(ie.on_llm_response_first(plugin, ev, resp))
    assert "&&meme:a2bd8f1b47a4&&" in resp.completion_text, "无候选信息时不剥(交给上游兜底)"
    assert not plugin.render_calls


# ---------- 口子 C: llm/emotion 模式(response 99998, 上游检索之后) ----------

def test_llm_mode_selected_sign_removed_and_rendered():
    plugin = _FakePlugin()
    ev = _Event()
    ev.set_extra("meme_manager_semantic_candidates", _sign_candidate_map())
    ev.set_extra("meme_manager_semantic_selected_ids", ["meme:a2bd8f1b47a4"])
    ev.set_extra("meme_manager_sign_text", "举牌！")

    asyncio.run(
        ie.on_llm_response_second(plugin, ev, llm_generate=_make_llm_response())
    )
    assert ev.get_extra("meme_manager_semantic_selected_ids") == [], "selected_ids 必须清空"
    assert ev.get_extra("meme_manager_sign_text") is None, "sign_text extra 必须清除(防上游旧路径双发)"
    assert plugin.render_calls


def test_llm_mode_normal_selected_kept():
    plugin = _FakePlugin()
    ev = _Event()
    ev.set_extra(
        "meme_manager_semantic_candidates",
        {
            "meme:9c8681a9b10c": {
                "id": "meme:9c8681a9b10c",
                "category": "selected",
                "caption": "普通表情",
                "tags": ["开心"],
            }
        },
    )
    ev.set_extra("meme_manager_semantic_selected_ids", ["meme:9c8681a9b10c"])

    asyncio.run(ie.on_llm_response_second(plugin, ev))
    assert ev.get_extra("meme_manager_semantic_selected_ids") == ["meme:9c8681a9b10c"]
    assert not plugin.render_calls


# ---------- 口子 D: decorating 阶段兜底清空 ----------

def test_decorating_clears_extras_and_appends_rendered_image():
    plugin = _FakePlugin()
    ev = _Event()
    ev.set_extra("meme_manager_semantic_selected_ids", ["meme:whatever"])
    ev.set_extra("meme_manager_semantic_default_id", "meme:whatever")
    ev.set_extra(ie.EXTRA_RENDERED_PATH, "/tmp/fake_sign.png")

    asyncio.run(ie.on_decorating_result_first(plugin, ev))
    assert ev.get_extra("meme_manager_semantic_selected_ids") is None
    assert ev.get_extra("meme_manager_semantic_default_id") is None
    # 渲染成品图必须以组件形式追加进链(standalone 同款方式)
    assert len(ev._result.chain) == 2


def test_render_failure_no_image_and_clean_text():
    """渲染失败: 绝不发底图,文本已剥净,本轮优雅降级为纯文字。"""
    plugin = _FakePlugin(render_ok=False)
    ev = _Event()
    ev.set_extra("meme_manager_semantic_candidates", _sign_candidate_map())
    ev.set_extra("meme_manager_semantic_default_id", "meme:a2bd8f1b47a4")
    resp = _Resp("回复\n\n&&meme:a2bd8f1b47a4&&")

    asyncio.run(
        ie.on_llm_response_first(plugin, ev, resp, llm_generate=_make_llm_response())
    )
    assert "meme:" not in resp.completion_text
    assert ev.get_extra(ie.EXTRA_RENDERED_PATH) is None
    # decorating 不追加任何图
    ev2 = _Event()
    ev2._result.chain = list(ev._result.chain)
    asyncio.run(ie.on_decorating_result_first(plugin, ev))
    assert len(ev._result.chain) == 1


def test_cleanup_after_sent():
    plugin = _FakePlugin()
    ev = _Event()
    ev.set_extra(ie.EXTRA_RENDERED_PATH, "/tmp/fake_sign.png")
    asyncio.run(ie.after_message_sent(plugin, ev))
    assert plugin.cleanup_calls == ["/tmp/fake_sign.png"]
    assert ev.get_extra(ie.EXTRA_RENDERED_PATH) is None


# ---------- 事故回归: 2026-09-10 22:04 QQ 实测 ----------

def test_incident_20260910_json_leak_and_render_crash():
    """完整复刻事故载荷: 模型输出 JSON 协议残留 + &&meme:&& 标记。

    事故: 渲染调用了不存在的 plugin.generate_with_template → AttributeError,
    且无人剥离 JSON 协议文本 → 原始协议原样发给用户。
    修复后: JSON 先行剥离(main.py 钩子内顺序) → 复用残留 sign_text(好女孩)
    → 按模板渲染成功 → 文本无 JSON 无标记、正文保留。
    """
    from backend import standalone_events as se

    plugin = _FakePlugin()
    ev = _Event()
    ev.set_extra("meme_manager_semantic_search_completed", True)
    ev.set_extra("meme_manager_semantic_candidates", _sign_candidate_map())
    ev.set_extra("meme_manager_semantic_default_id", "meme:a2bd8f1b47a4")
    resp = _Resp(
        '{"reply":"哼，喏——本小姐亲自盖章：好女孩desuwa~！\\n","sign_text":"好女孩"}'
        "\n&&meme:a2bd8f1b47a4&&"
    )

    async def _hook():
        # 复刻 main.py 100000 钩子的派发顺序(剥离先行)
        await se.handle_llm_response(plugin, ev, resp)
        await ie.on_llm_response_first(plugin, ev, resp)

    asyncio.run(_hook())
    assert "{" not in resp.completion_text, "JSON 协议残留不得发给用户"
    assert "sign_text" not in resp.completion_text
    assert "meme:" not in resp.completion_text, "&&meme:&& 标记必须剥除"
    assert "好女孩desuwa" in resp.completion_text, "正文必须保留"
    assert plugin.render_calls, "必须触发渲染"
    assert plugin.render_calls[0][1] == "好女孩", "必须复用模型残留牌面文字(不烧二次 LLM)"
    assert ev.get_extra(ie.EXTRA_RENDERED_PATH) == "/tmp/fake_sign.png"


def test_render_interface_missing_degrades_cleanly():
    """插件缺渲染接口时: 明确降级纯文字,不抛 AttributeError。"""
    plugin = _FakePlugin()
    plugin.render_for_meme_manager = None  # 实例级覆盖,模拟接口缺失(不污染类)
    ev = _Event()
    ev.set_extra("meme_manager_semantic_candidates", _sign_candidate_map())
    ev.set_extra("meme_manager_semantic_default_id", "meme:a2bd8f1b47a4")
    resp = _Resp("回复\n\n&&meme:a2bd8f1b47a4&&")

    asyncio.run(
        ie.on_llm_response_first(plugin, ev, resp, llm_generate=_make_llm_response())
    )
    assert ev.get_extra(ie.EXTRA_RENDERED_PATH) is None, "缺接口必须降级,不得出现半成品状态"


if __name__ == "__main__":
    test_tool_mode_sign_marker_stripped_and_rendered()
    test_tool_mode_normal_meme_untouched()
    test_tool_mode_no_candidates_no_action()
    test_llm_mode_selected_sign_removed_and_rendered()
    test_llm_mode_normal_selected_kept()
    test_decorating_clears_extras_and_appends_rendered_image()
    test_render_failure_no_image_and_clean_text()
    test_cleanup_after_sent()
    test_incident_20260910_json_leak_and_render_crash()
    test_render_interface_missing_degrades_cleanly()
    print("integrated events tests PASS (10)")
