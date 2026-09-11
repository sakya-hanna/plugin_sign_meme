"""integrated 模式拦截管线(零修改对接官方 meme_manager)。

设计(2026-09-10,计划 .hermes/plans/2026-09-10_170328):
sign_meme 以更高 priority 的钩子先于官方 meme_manager(99999/0)执行,
在官方看到数据之前处理举牌候选;官方全程无感知,可独立升级。

四个泄漏口子与拦截时点:
- 口子A tool 模式 &&meme:id&& 标记 → on_llm_response_first(100000) 剥标记
- 口子B tool 模式 default_id 自动回退 → 同上钩子清空该 extra
- 口子C llm/emotion 模式 selected_ids → on_llm_response_second(99998,
  在官方 response(99999) 检索选图之后、decorating 之前)移除
- 口子D 流式兼容路径 → decorating(100000) 先行清空 selected_ids +
  default_id,自身成品图以消息链组件追加

识别依据: event extra "meme_manager_semantic_candidates"(官方
remember_candidates 写入)中该 id 的 category 是否为举牌模板分类。
官方原版候选 dict 含 category 字段(semantic_index search_index 产出)。
"""
from __future__ import annotations

import asyncio
import json
import re
import uuid
from typing import Any, Awaitable, Callable

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent

from .semantic_pool import SIGN_CATEGORY
from .standalone_events import EXTRA_MODEL_SIGN_TEXT

# 本插件自己的 extra key(前缀区分,不与上游冲突)
EXTRA_RENDERED_PATH = "sign_meme_integrated_rendered_path"
EXTRA_RENDERED_REQUEST_ID = "sign_meme_integrated_request_id"
EXTRA_PIPELINE_ATTEMPTED = "sign_meme_integrated_attempted"

# 官方 extras key(只读消费;官方原版即存在的稳定 key)
UPSTREAM_CANDIDATES = "meme_manager_semantic_candidates"
UPSTREAM_DEFAULT_ID = "meme_manager_semantic_default_id"
UPSTREAM_SELECTED_IDS = "meme_manager_semantic_selected_ids"
UPSTREAM_SIGN_TEXT = "meme_manager_sign_text"

_MEME_MARKER = re.compile(r"&&\s*meme:[^&\n]+&&", re.IGNORECASE)


def lookup_candidate_category(event: AstrMessageEvent, meme_id: str) -> str | None:
    """从官方候选映射查该 id 的 category。查不到返回 None(不是举牌)。"""
    candidates = event.get_extra(UPSTREAM_CANDIDATES)
    if not isinstance(candidates, dict):
        return None
    candidate = candidates.get(str(meme_id or "").strip())
    if not isinstance(candidate, dict):
        return None
    category = str(candidate.get("category") or "").strip()
    return category or None


def extract_meme_ids(text: str) -> list[str]:
    """从文本中提取 &&meme:xxx&& 引用的 id 列表(保持出现顺序)。"""
    ids: list[str] = []
    for match in re.finditer(r"&&\s*(meme:[^&\n]+?)\s*&&", text or "", re.IGNORECASE):
        normalized = f"meme:{match.group(1)[5:].strip()}"
        if normalized not in ids:
            ids.append(normalized)
    return ids


def strip_meme_markers(text: str, ids_to_strip: list[str]) -> tuple[str, bool]:
    """剥除指定 id 的 &&meme:..&& 标记。返回 (新文本, 是否有剥除)。"""
    changed = False
    for meme_id in ids_to_strip:
        literal = f"&&{meme_id}&&"
        if literal in (text or ""):
            text = re.sub(
                r"&&\s*" + re.escape(meme_id) + r"\s*&&", "", text, flags=re.IGNORECASE
            )
            changed = True
    # 剥除后清理残留空标记行
    text = _MEME_MARKER.sub("", text or "")
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    return text, changed


def _active_sign_template_id(plugin) -> str:
    """当前激活模板 id(从插件 service 读,保持单一来源)。"""
    try:
        template = plugin.service.active_template()
        return str((template or {}).get("id") or "")
    except Exception:
        return ""


async def _generate_sign_text(
    plugin,
    event: AstrMessageEvent,
    llm_generate: Callable[..., Awaitable[Any]] | None = None,
) -> str:
    """二次 LLM: 基于本轮回复语境生成牌面短句。

    llm_generate 可注入(测试);生产默认经 plugin 的 AstrBot context 调用。
    """
    provider_id = str(plugin.sign_text_llm_provider or "").strip()
    model = str(plugin.sign_text_llm_model or "").strip()
    # 留空时复用本轮回复模型(官方 on_llm_request 记录的本轮 provider/model)
    if not provider_id:
        provider_id = str(event.get_extra("meme_manager_reply_provider_id") or "").strip()
    if not model:
        model = str(event.get_extra("meme_manager_reply_model") or "").strip()

    reply_context = str(event.get_extra("sign_meme_integrated_reply_context") or "")
    prompt = (
        "根据机器人即将发送的回复，生成一句适合写在举牌牌面上的简短短句。"
        "要求：不超过40个字符；贴合回复的情绪与潜台词；只输出JSON对象，"
        '格式为：{"sign_text":"牌面短句"}；不要输出其他内容。\n'
        f"机器人回复：{reply_context}"
    )
    if llm_generate is not None:
        response = await llm_generate(plugin, event, prompt, provider_id, model)
    else:
        if not provider_id:
            # 兜底: 官方未记录本轮 provider 时用全局默认对话模型。
            try:
                provider_id = str(
                    plugin.context.provider_manager.provider_settings.get(
                        "default_provider_id", ""
                    )
                    or ""
                ).strip()
            except Exception:
                provider_id = ""
        if not provider_id:
            logger.warning(
                "[sign_meme][integrated] 无可用 LLM provider,跳过牌面文字生成"
            )
            return ""
        response = await plugin.llm_generate(prompt, provider_id=provider_id, model=model)
    raw = str(getattr(response, "completion_text", "") or "").strip()
    try:
        data = json.loads(raw)
        if isinstance(data, dict):
            return str(data.get("sign_text") or "").strip()
    except (TypeError, ValueError):
        pass
    match = re.search(r"\{[\s\S]*\}", raw)
    if match:
        try:
            data = json.loads(match.group(0))
            if isinstance(data, dict):
                return str(data.get("sign_text") or "").strip()
        except (TypeError, ValueError):
            pass
    return ""


async def run_render_pipeline(
    plugin,
    event: AstrMessageEvent,
    llm_generate: Callable[..., Awaitable[Any]] | None = None,
) -> bool:
    """拦截确认后的完整渲染链路。成功返回 True(成品路径写入 extra)。

    失败路径: 记日志、EXTRA_RENDERED_PATH 保持 None、本轮优雅降级为
    纯文字——绝不发送未渲染的空白底图。
    """
    if event.get_extra(EXTRA_PIPELINE_ATTEMPTED):
        return False
    event.set_extra(EXTRA_PIPELINE_ATTEMPTED, True)
    template_id = _active_sign_template_id(plugin)
    if not template_id:
        logger.warning("[sign_meme][integrated] 无激活模板,放弃举牌渲染")
        return False
    # 牌面文字来源优先级:
    # 1) 模型本轮自己输出的 JSON 协议残留(handle_llm_response 兜底剥离所得)
    # 2) 二次 LLM 生成
    sign_text = str(event.get_extra(EXTRA_MODEL_SIGN_TEXT) or "").strip()
    if sign_text:
        event.set_extra(EXTRA_MODEL_SIGN_TEXT, None)
        logger.info("[sign_meme][integrated] 复用模型协议残留牌面文字=%s", sign_text)
    else:
        try:
            sign_text = await _generate_sign_text(plugin, event, llm_generate)
        except Exception as exc:
            logger.error(
                "[sign_meme][integrated] 牌面文字生成失败 error=%s", exc, exc_info=True
            )
            return False
    if not sign_text:
        logger.info("[sign_meme][integrated] 未产出牌面文字,跳过渲染")
        return False
    request_id = uuid.uuid4().hex
    try:
        # 真实接口: main.py 上的公开渲染方法(按模板渲染,不依赖激活态)。
        # getattr 防护: 接口缺失时明确降级为纯文字,不抛 AttributeError。
        render = getattr(plugin, "render_for_meme_manager", None)
        if render is None:
            logger.error(
                "[sign_meme][integrated] 插件缺少 render_for_meme_manager 接口,"
                "放弃渲染(降级纯文字)"
            )
            return False
        result = await render(template_id, sign_text, request_id=request_id)
    except Exception as exc:
        logger.error(
            "[sign_meme][integrated] 渲染异常 request_id=%s error=%s",
            request_id,
            exc,
            exc_info=True,
        )
        return False
    if not isinstance(result, dict) or not result.get("ok"):
        logger.warning(
            "[sign_meme][integrated] 渲染跳过 request_id=%s code=%s",
            request_id,
            result.get("error_code") if isinstance(result, dict) else "invalid_result",
        )
        return False
    path = str(result.get("temporary_path") or "").strip()
    if not path:
        logger.error("[sign_meme][integrated] 渲染路径为空 request_id=%s", request_id)
        return False
    event.set_extra(EXTRA_RENDERED_PATH, path)
    event.set_extra(EXTRA_RENDERED_REQUEST_ID, request_id)
    logger.info(
        "[sign_meme][integrated] 渲染成功 request_id=%s template=%s text=%s",
        request_id,
        template_id,
        sign_text,
    )
    return True


async def on_llm_response_first(
    plugin,
    event: AstrMessageEvent,
    response,
    llm_generate: Callable[..., Awaitable[Any]] | None = None,
) -> None:
    """priority=100000。口子A/B: tool 模式标记剥除 + default_id 清空。

    官方 response(99999) 在本钩子之后执行,看到的文本已无举牌标记、
    default_id 已空 → 不会发送举牌底图。普通候选完全不动。
    """
    if plugin.sign_mode != "integrated":
        return
    text = str(getattr(response, "completion_text", "") or "")
    if not text:
        return
    ids = extract_meme_ids(text)
    sign_ids = [
        meme_id
        for meme_id in ids
        if lookup_candidate_category(event, meme_id) == SIGN_CATEGORY
    ]
    # 记录本轮回复语境供二次 LLM 使用(在剥标记前取正文)
    if sign_ids:
        visible = text
        for meme_id in sign_ids:
            visible = visible.replace(f"&&{meme_id}&&", "").replace(meme_id, "")
        event.set_extra("sign_meme_integrated_reply_context", visible.strip()[:300])
    if sign_ids:
        stripped, _ = strip_meme_markers(text, sign_ids)
        response.completion_text = stripped
    default_id = str(event.get_extra(UPSTREAM_DEFAULT_ID) or "").strip()
    if default_id and lookup_candidate_category(event, default_id) == SIGN_CATEGORY:
        event.set_extra(UPSTREAM_DEFAULT_ID, "")
        if not sign_ids:
            sign_ids = [default_id]
    if sign_ids:
        logger.info(
            "[sign_meme][integrated] 拦截举牌候选 ids=%d,启动渲染管线", len(sign_ids)
        )
        await run_render_pipeline(plugin, event, llm_generate)


async def on_llm_response_second(
    plugin,
    event: AstrMessageEvent,
    llm_generate: Callable[..., Awaitable[Any]] | None = None,
) -> None:
    """priority=99998。口子C: llm/emotion 模式在官方检索选图后移除举牌候选。

    时序: 官方 response(99999) 内部完成 search_memes 与选图并写入
    selected_ids → 本钩子(99998) 移除举牌 id 并启动渲染管线 →
    官方 decorating(99999) 消费 selected_ids 时已无举牌。
    """
    if plugin.sign_mode != "integrated":
        return
    selected = event.get_extra(UPSTREAM_SELECTED_IDS)
    if not isinstance(selected, list) or not selected:
        return
    sign_selected = [
        meme_id
        for meme_id in selected
        if lookup_candidate_category(event, meme_id) == SIGN_CATEGORY
    ]
    if not sign_selected:
        return
    remaining = [meme_id for meme_id in selected if meme_id not in sign_selected]
    event.set_extra(UPSTREAM_SELECTED_IDS, remaining)
    # 清掉 sign_text extra,防止官方旧路径(若有)二次发图
    event.set_extra(UPSTREAM_SIGN_TEXT, None)
    # 回复语境: 从 result chain/响应外不可得,用空语境退化(prompt 仍可用)
    if event.get_extra("sign_meme_integrated_reply_context") is None:
        event.set_extra("sign_meme_integrated_reply_context", "")
    logger.info(
        "[sign_meme][integrated] 移除举牌候选(selected) ids=%s", sign_selected
    )
    await run_render_pipeline(plugin, event, llm_generate)


async def on_decorating_result_first(plugin, event: AstrMessageEvent) -> None:
    """priority=100000。口子D兜底清空 + 成品图追加进消息链。

    兜底清空仅允许在"本轮确实存在举牌候选渲染"时执行。
    无条件清空会把官方 on_llm_response(99999) 刚写入的普通表情
    selected_ids 一并抹掉（ decorating 100000 先于官方 99999 执行），
    导致普通表情图片永远不被发送（2026-09-10 18:24 实测回归）。
    """
    if plugin.sign_mode != "integrated":
        return
    path = str(event.get_extra(EXTRA_RENDERED_PATH) or "").strip()
    if not path:
        # 本轮没有举牌渲染: 不得触碰官方 selected_ids/default_id,
        # 让普通表情链路原样走官方 decorating(99999)。
        return
    # 有举牌成品图待追加: 此时才兜底清空,防止官方 decorating 再次消费举牌候选。
    event.set_extra(UPSTREAM_SELECTED_IDS, None)
    event.set_extra(UPSTREAM_DEFAULT_ID, None)
    try:
        from astrbot.core.message.components import Image

        result_obj = event.get_result()
        if result_obj is None:
            logger.warning("[sign_meme][integrated] 无可附加的结果链,跳过发图")
            await _discard_rendered(plugin, event, path)
            return
        image = Image.fromFileSystem(path)
        chain = getattr(result_obj, "chain", None)
        if isinstance(chain, list):
            result_obj.chain = chain + [image]
        else:
            result_obj.chain = [image]
    except Exception as exc:
        logger.error(
            "[sign_meme][integrated] 附加图片失败 error=%s", exc, exc_info=True
        )
        await _discard_rendered(plugin, event, path)
        return
    # H1 回归(2026-09-11): 追加成功后必须保留 EXTRA_RENDERED_PATH——
    # after_message_sent 靠它找到临时文件来清理(消费时自行置空)。
    # 此前 decorating 在追加前置空 → 清理永远读空 → 每轮泄漏 ~1.1MB
    # (generated/ 实测堆积 5 个孤儿 PNG 共 5.7MB)。
    logger.info(
        "[sign_meme][integrated] 举牌成品图已追加 request_id=%s",
        event.get_extra(EXTRA_RENDERED_REQUEST_ID) or "",
    )


async def _discard_rendered(plugin, event: AstrMessageEvent, path: str) -> None:
    """追加失败的兜底: 立即清理临时文件并清掉待清理标记,不留泄漏。"""
    event.set_extra(EXTRA_RENDERED_PATH, None)
    event.set_extra(EXTRA_RENDERED_REQUEST_ID, None)
    try:
        await plugin.cleanup_generated(path)
    except Exception as exc:
        logger.error(
            "[sign_meme][integrated] 失败路径临时图清理失败 path=%s error=%s", path, exc
        )


async def after_message_sent(plugin, event: AstrMessageEvent) -> None:
    """清理本轮渲染临时文件。"""
    path = str(event.get_extra(EXTRA_RENDERED_PATH) or "").strip()
    if not path:
        return
    event.set_extra(EXTRA_RENDERED_PATH, None)
    request_id = event.get_extra(EXTRA_RENDERED_REQUEST_ID)
    try:
        await plugin.cleanup_generated(path)
        logger.info(
            "[sign_meme][integrated] 临时成品图已清理 request_id=%s",
            request_id or "",
        )
    except Exception as exc:
        logger.error(
            "[sign_meme][integrated] 临时图清理失败 path=%s error=%s", path, exc
        )
