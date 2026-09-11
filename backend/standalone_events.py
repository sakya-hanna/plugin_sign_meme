"""sign_meme 独立模式事件链路(从 meme_manager event_handlers 平移而来)。

职责(仅 standalone 模式激活):
- on_llm_request: 注入结构化举牌输出协议(marker 防重);
- on_llm_response: 解析 {"reply","sign_text"},剥离 JSON;
- on_decorating_result: 渲染激活模板并把成品图追加到消息链;
- after_message_sent: 清理临时文件。

meme_manager 侧旧链路检测 self_managed_sign_events 标志后让位;
两套链路通过同一 marker 与 extra key 互斥,不会双注入/双发图。
"""
from __future__ import annotations

import json
import re
import uuid
from pathlib import Path
from typing import Any

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, MessageChain
from astrbot.core.message.components import Image
from astrbot.core.provider.entities import ProviderRequest

SIGN_PROMPT_MARKER = "<!-- sign_meme_prompt:v1 -->"
EXTRA_SIGN_TEXT = "meme_manager_sign_text"          # 与旧链路共用 key:任何一侧消费后另一侧不会再发
EXTRA_SIGN_ATTEMPTED = "meme_manager_sign_attempted"
EXTRA_SIGN_PLUGIN = "meme_manager_sign_plugin"
EXTRA_SIGN_TEMP_FILE = "meme_manager_sign_temp_file"
EXTRA_SIGN_REQUEST_ID = "meme_manager_sign_request_id"
# integrated: 模型从历史样本模仿输出 {"reply","sign_text"} 协议时,残留里的
# sign_text 存到这里供渲染管线直接复用(省一次二次 LLM)。挂在本插件命名空间,
# 不写官方旧 key(官方旧链路可能消费导致双发)。
EXTRA_MODEL_SIGN_TEXT = "sign_meme_integrated_model_sign_text"

SIGN_PROMPT_BODY = (
    "\n\n" + SIGN_PROMPT_MARKER + "\n"
    + "当且仅当当前回复适合配一张举牌表情包时，生成适合写在牌面上的简短短句；"
    + "否则 sign_text 必须为空字符串。牌面短句不得超过40个字符，不得截断。"
    + "必须只输出JSON对象，格式为："
    + '{"reply":"给用户的正常回复","sign_text":"牌面短句或空字符串"}。'
    + "reply 中不要解释字段，不要输出 Markdown 代码围栏。"
)


def build_sign_prompt_suffix(semantic_context: dict[str, Any] | None) -> str:
    """协议主体 + 当前激活模板语义提示。"""
    suffix = SIGN_PROMPT_BODY
    if semantic_context:
        caption = str(semantic_context.get("caption") or "").strip()
        tags = semantic_context.get("tags") or []
        if caption or tags:
            suffix += (
                "\n当前激活举牌模板的语义信息（仅用于判断是否适合使用，不要原样输出）："
                + json.dumps(
                    {"caption": caption, "tags": tags, "visible_text": ""},
                    ensure_ascii=False,
                )
            )
    return suffix


def parse_sign_response(text: str) -> tuple[str, str] | None:
    """解析模型结构化回复;格式不符返回 None,不破坏原回复。"""
    raw = str(text or "").strip()
    if not raw:
        return None
    data = None
    try:
        data = json.loads(raw)
    except (TypeError, ValueError):
        match = re.search(r"\{[\s\S]*\}", raw)
        if match:
            try:
                data = json.loads(match.group(0))
            except (TypeError, ValueError):
                data = None
    if not isinstance(data, dict) or "reply" not in data or "sign_text" not in data:
        return None
    reply = data.get("reply")
    sign_text = data.get("sign_text")
    if not isinstance(reply, str) or not isinstance(sign_text, str):
        return None
    return reply.strip(), sign_text.strip()


def _effective_mode(plugin) -> str:
    """统一读"实际生效"模式(H2,2026-09-11)。

    必须走 plugin._effective_sign_mode()(integrated+上游不可用时降级 standalone),
    而不是配置值 plugin.sign_mode——此前内部 guard 读配置值,导致降级期间
    main.py 按 effective 派发进来的 standalone 链路又被这里的 guard 挡回去,
    举牌功能整体静默失效。fake 插件(测试)无该方法时回退配置值。
    """
    getter = getattr(plugin, "_effective_sign_mode", None)
    if callable(getter):
        try:
            return str(getter() or "standalone")
        except Exception:
            pass
    return str(getattr(plugin, "sign_mode", "standalone") or "standalone")


async def handle_llm_request(plugin, event: AstrMessageEvent, req: ProviderRequest) -> None:
    """独立模式:向主回复请求注入举牌协议。"""
    if _effective_mode(plugin) != "standalone":
        return
    current = str(getattr(req, "system_prompt", "") or "")
    if SIGN_PROMPT_MARKER in current:
        return  # meme_manager 旧链路已注入(migration 期共存)
    try:
        context = plugin.get_active_semantic_context()
    except Exception as exc:
        logger.warning("[sign_meme] 读取模板语义快照失败: %s", exc)
        context = None
    req.system_prompt = current + build_sign_prompt_suffix(context)
    logger.debug("[sign_meme] standalone 协议已注入")


async def handle_llm_response(plugin, event: AstrMessageEvent, response) -> None:
    """独立模式:解析结构化回复,剥离 JSON,记录 sign_text。

    integrated 模式同样兜底剥离(2026-09-10 22:04 实测回归):旧链路协议
    可能经对话历史样本被模型模仿输出;渲染管线崩溃或无人剥离时,原始
    JSON 会原样发给用户。剥离出的 sign_text 写入本插件 extra 供渲染
    管线复用,不写官方旧 key(防官方旧路径消费导致双发)。
    """
    if event.get_extra("meme_manager_sign_structured"):
        return  # 旧链路已处理(migration 期共存)
    text = str(getattr(response, "completion_text", "") or "")
    if SIGN_PROMPT_MARKER not in text and not text.strip().startswith("{"):
        # 无协议痕迹且非 JSON 形态,快速跳过(协议未注入给本轮的概率高)。
        # L5(2026-09-11): 窗口放宽到全文首个 '{'。此前只看前 200 字符,
        # 长回复里 JSON 出现在 200 字符之后时会漏剥离,协议原文发给用户。
        if "{" not in text:
            return
    structured = parse_sign_response(text)
    if structured is None:
        return
    visible_reply, sign_text = structured
    response.completion_text = visible_reply
    # H2: 按实际生效模式写 extra。降级期间(配置 integrated+上游不可用)
    # effective=standalone → 写 EXTRA_SIGN_TEXT 供 standalone 渲染链消费;
    # 写错 key 会导致降级轮解析出了牌面文字却永远无人渲染。
    if _effective_mode(plugin) == "standalone":
        event.set_extra(EXTRA_SIGN_TEXT, sign_text)
        logger.info("[sign_meme] standalone 解析结构化回复 sign_text=%s", bool(sign_text))
    else:
        event.set_extra(EXTRA_MODEL_SIGN_TEXT, sign_text)
        logger.info(
            "[sign_meme] integrated 模式剥离模型模仿的 JSON 协议残留 sign_text=%s",
            bool(sign_text),
        )
    event.set_extra("meme_manager_sign_structured", True)


async def handle_decorating_result(plugin, event: AstrMessageEvent) -> None:
    """独立模式:渲染激活模板并把成品图追加到消息链。"""
    if _effective_mode(plugin) != "standalone":
        return
    sign_text = str(event.get_extra(EXTRA_SIGN_TEXT) or "").strip()
    if not sign_text:
        return
    if event.get_extra(EXTRA_SIGN_ATTEMPTED):
        return
    event.set_extra(EXTRA_SIGN_ATTEMPTED, True)

    request_id = uuid.uuid4().hex
    event.set_extra(EXTRA_SIGN_REQUEST_ID, request_id)
    try:
        result = await plugin.generate_for_meme_manager(sign_text, request_id=request_id)
    except Exception as exc:
        logger.error("[sign_meme] standalone 渲染异常 request_id=%s error=%s", request_id, exc, exc_info=True)
        return
    if not isinstance(result, dict) or not result.get("ok"):
        logger.warning(
            "[sign_meme] standalone 渲染跳过 request_id=%s code=%s",
            request_id,
            result.get("error_code") if isinstance(result, dict) else "invalid_result",
        )
        return
    path = str(result.get("temporary_path") or "").strip()
    if not path or not Path(path).is_file():
        logger.error("[sign_meme] standalone 渲染路径无效 request_id=%s", request_id)
        return
    event.set_extra(EXTRA_SIGN_PLUGIN, plugin)
    event.set_extra(EXTRA_SIGN_TEMP_FILE, path)
    try:
        result_obj = event.get_result()
        if result_obj is None:
            logger.warning("[sign_meme] standalone 无可附加的结果链,跳过发图")
            return
        image = Image.fromFileSystem(path)
        chain = getattr(result_obj, "chain", None)
        if isinstance(chain, list):
            result_obj.chain = chain + [image]
        else:
            result_obj.chain = MessageChain([image]) if not chain else chain
        logger.info(
            "[sign_meme] standalone 已附加举牌图 request_id=%s template_id=%s",
            request_id, result.get("template_id", ""),
        )
    except Exception as exc:
        logger.error("[sign_meme] standalone 附加图片失败 request_id=%s error=%s", request_id, exc, exc_info=True)


async def handle_after_message_sent(plugin, event: AstrMessageEvent) -> None:
    """独立模式:清理本轮临时文件。"""
    path = str(event.get_extra(EXTRA_SIGN_TEMP_FILE) or "").strip()
    if not path:
        return
    event.set_extra(EXTRA_SIGN_TEMP_FILE, None)
    try:
        await plugin.cleanup_generated(path)
    except Exception as exc:
        logger.warning("[sign_meme] standalone 临时文件清理失败 path=%s error=%s", path, exc)
