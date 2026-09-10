from __future__ import annotations

import asyncio
import base64
import json
import logging
import mimetypes
import secrets
import time
from pathlib import Path

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star
from astrbot.api.web import error_response, file_response, json_response, request
from astrbot.core.utils.astrbot_path import get_astrbot_data_path

from .service import SignMemeError, SignMemeService
from . import standalone_events

PLUGIN_NAME = "sign_meme"
ALT_PLUGIN_NAME = "astrbot_plugin_sign_meme"
DATA_DIR_NAME = "sign_meme"


class SignMemePlugin(Star):
    def __init__(self, context: Context, config=None):
        super().__init__(context)
        data_dir = Path(get_astrbot_data_path()) / "plugin_data" / DATA_DIR_NAME
        self.service = SignMemeService(data_dir, logger)
        self.context = context
        self.config = config
        self.service.sign_mode_provider = lambda: self.sign_mode
        self._register_routes()
        self.logger.info(
            "event=plugin_initialized data_dir=%s sign_mode=%s",
            data_dir,
            self.sign_mode,
        )
        # 启动时对齐语义池(幂等;integrated 补缺,standalone 清残留)
        try:
            summary = self.service.reconcile_semantic_pool()
            self.logger.info("event=sign_pool_startup_reconcile %s", summary)
        except Exception as exc:
            self.logger.warning("event=sign_pool_startup_reconcile_failed error=%s", exc)

    # ---- 模式与配置 ----

    @property
    def sign_mode(self) -> str:
        """standalone=独立命中链路(本插件自管);integrated=对接 meme_manager 语义检索。"""
        value = ""
        try:
            value = str(self.config.sign_mode if self.config is not None else "").strip()
        except Exception:
            value = ""
        return value if value in ("standalone", "integrated") else "standalone"

    @property
    def sign_text_llm_provider(self) -> str:
        try:
            return str(self.config.sign_text_llm_provider if self.config is not None else "").strip()
        except Exception:
            return ""

    @property
    def sign_text_llm_model(self) -> str:
        try:
            return str(self.config.sign_text_llm_model if self.config is not None else "").strip()
        except Exception:
            return ""

    @property
    def self_managed_sign_events(self) -> bool:
        """能力标志:meme_manager 旧链路据此让位(避免双注入/双发图)。"""
        return True

    async def _rebuild_pool_vectors(self, entry_ids: list[str] | None = None) -> dict:
        """对接模式:按 entry 增量重建主 pack FAISS 索引(需要 embedding provider)。"""
        try:
            import sys
            plugins_root = str(Path(get_astrbot_data_path()) / "plugins")
            if plugins_root not in sys.path:
                sys.path.insert(0, plugins_root)
            from astrbot_plugin_meme_manager.backend.semantic_index import (
                EmbeddingAdapter, build_index,
            )
            from astrbot_plugin_meme_manager.backend import semantic_storage
            from astrbot_plugin_meme_manager.backend.pack_resolver import resolve_pack_id
            pack_id = resolve_pack_id()
            if not pack_id:
                return {"ok": False, "reason": "no_default_pack"}
            pack_dir = Path(get_astrbot_data_path()) / "plugin_data" / "meme_manager" / "packs" / pack_id
            provider = None
            try:
                provider = self.context.get_using_provider()
            except Exception:
                pass
            if provider is None:
                return {"ok": False, "reason": "no_embedding_provider"}
            embedding = EmbeddingAdapter(provider)
            if not embedding.ready:
                return {"ok": False, "reason": "embedding_not_ready"}
            result = await build_index(
                pack_dir,
                Path(get_astrbot_data_path()) / "plugin_data" / "meme_manager",
                pack_id,
                embedding,
                target_entry_ids=set(entry_ids) if entry_ids else None,
            )
            return {"ok": True, "result": {k: result.get(k) for k in ("added", "reused", "total", "index_ready") if k in result}}
        except Exception as exc:
            self.logger.error("event=sign_pool_vector_rebuild_failed error=%s", type(exc).__name__)
            return {"ok": False, "reason": str(exc)}

    async def _after_mode_switch(self) -> None:
        """模式切换后:reconcile 语义池 + 触发向量增量。"""
        summary = self.service.reconcile_semantic_pool()
        self.logger.info("event=sign_mode_switched mode=%s reconcile=%s", self.sign_mode, summary)
        if self.sign_mode == "integrated" and summary.get("ok"):
            await self._rebuild_pool_vectors()

    def _register_routes(self) -> None:
        routes = [
            (f"/{PLUGIN_NAME}/templates", self.api_templates, ["GET"], "列出举牌模板"),
            (f"/{PLUGIN_NAME}/templates", self.api_create_template, ["POST"], "创建举牌模板"),
            (f"/{PLUGIN_NAME}/upload", self.api_upload, ["POST"], "上传举牌模板图片"),
            (f"/{PLUGIN_NAME}/templates/<template_id>", self.api_update_template, ["PUT", "POST"], "更新举牌模板"),
            (f"/{PLUGIN_NAME}/templates/<template_id>/activate", self.api_activate_template, ["POST"], "激活举牌模板"),
            (f"/{PLUGIN_NAME}/templates/<template_id>", self.api_delete_template, ["DELETE"], "删除举牌模板"),
            (f"/{PLUGIN_NAME}/templates/<template_id>/delete", self.api_delete_template, ["POST"], "删除举牌模板"),
            (f"/{PLUGIN_NAME}/image/<template_id>", self.api_template_image, ["GET"], "预览举牌模板"),
            (f"/{PLUGIN_NAME}/image/<template_id>/preview", self.api_template_image_preview, ["GET"], "读取举牌模板预览"),
            (f"/{PLUGIN_NAME}/generate", self.api_generate, ["POST"], "测试生成举牌图片"),
            (f"/{PLUGIN_NAME}/generated/save", self.api_save_generated, ["POST"], "收藏生成的举牌图片"),
            (f"/{PLUGIN_NAME}/mode", self.api_get_mode, ["GET"], "读取运行模式"),
            (f"/{PLUGIN_NAME}/mode/reconcile", self.api_reconcile_mode, ["POST"], "切换运行模式并同步语义池"),
        ]
        alias_routes = [
            (route.replace(f"/{PLUGIN_NAME}/", f"/{ALT_PLUGIN_NAME}/", 1), handler, methods, desc)
            for route, handler, methods, desc in routes
        ]
        for route, handler, methods, desc in routes + alias_routes:
            self.context.register_web_api(route, handler, methods, desc)

    @staticmethod
    def _admin_required() -> bool:
        return bool(getattr(request, "username", None))

    @staticmethod
    async def _json_object() -> dict | None:
        data = await request.json(default={})
        return data if isinstance(data, dict) else None

    def _error(self, message: str, status: int = 400):
        # AstrBot 4.27.x requires status_code as a keyword-only argument.
        # Passing it positionally turns any intended business error into a 500.
        return error_response(message, status_code=status)

    @staticmethod
    def _rid() -> str:
        return secrets.token_hex(8)

    def _log_rejected(self, event: str, request_id: str, status: int, reason: str) -> None:
        logger.warning(
            "event=%s request_id=%s status=%d reason=%s",
            event, request_id, status, reason,
        )

    async def api_templates(self):
        request_id = self._rid()
        if not self._admin_required():
            self._log_rejected("template_list_rejected", request_id, 403, "not_authenticated")
            return self._error("需要管理员登录", 403)
        templates = self.service.list_templates()
        logger.info("event=template_list_succeeded request_id=%s count=%d", request_id, len(templates))
        return json_response({"templates": templates})

    async def api_create_template(self):
        request_id = self._rid()
        if not self._admin_required():
            self._log_rejected("template_create_rejected", request_id, 403, "not_authenticated")
            return self._error("需要管理员登录", 403)
        json_data = await self._json_object()
        if json_data is not None and json_data.get("upload_token"):
            try:
                logger.info("event=template_create_started request_id=%s mode=staged", request_id)
                image_bytes, staged_name = self.service.consume_upload(json_data["upload_token"])
                result = self.service.create_template(
                    name=json_data.get("name"),
                    description=json_data.get("description", ""),
                    caption=json_data.get("caption"),
                    tags=json_data.get("tags", []),
                    visible_text="",
                    image_bytes=image_bytes,
                    original_name=json_data.get("original_name") or staged_name,
                    rect=json_data.get("rect"),
                )
                logger.info("event=template_create_succeeded request_id=%s template_id=%s", request_id, result["id"])
                return json_response({"template": result, "request_id": request_id}, status_code=201)
            except SignMemeError as exc:
                logger.warning("event=template_create_failed request_id=%s error_code=%s", request_id, exc.code)
                return self._error(exc.message, 400)
            except Exception:
                logger.exception("event=template_create_failed request_id=%s error_code=internal", request_id)
                return self._error("提交模板失败", 500)
        files = await request.files()
        upload = files.get("file") if files else None
        if not upload:
            self._log_rejected("template_create_rejected", request_id, 400, "missing_file")
            return self._error("缺少 file", 400)
        data = await request.form()
        try:
            image_bytes = await upload.read()
            rect = json.loads(str(data.get("rect") or ""))
            result = self.service.create_template(
                name=data.get("name"),
                description=data.get("description", ""),
                caption=data.get("caption"),
                tags=data.get("tags", []),
                visible_text="",
                image_bytes=image_bytes,
                original_name=getattr(upload, "filename", "template.png"),
                rect=rect,
            )
            logger.info("event=template_create_succeeded request_id=%s template_id=%s mode=direct", request_id, result["id"])
            return json_response({"template": result}, status_code=201)
        except SignMemeError as exc:
            logger.warning("event=template_create_failed request_id=%s error_code=%s", request_id, exc.code)
            return self._error(exc.message, 400)
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            logger.warning("event=template_create_failed request_id=%s error_code=invalid_request detail=%s", request_id, type(exc).__name__)
            return self._error(f"请求参数非法: {exc}", 400)
        except Exception:
            logger.exception("event=template_create_failed request_id=%s error_code=internal", request_id)
            return self._error("创建模板失败", 500)

    async def api_upload(self):
        request_id = self._rid()
        if not self._admin_required():
            self._log_rejected("template_upload_rejected", request_id, 403, "not_authenticated")
            return self._error("需要管理员登录", 403)
        try:
            logger.info("event=template_upload_started request_id=%s", request_id)
            files = await request.files()
            upload = files.get("file") if files else None
            if not upload:
                self._log_rejected("template_upload_rejected", request_id, 400, "missing_file")
                return self._error("缺少 file", 400)
            original_name = getattr(upload, "filename", "template.png")
            image_bytes = await upload.read()
            result = self.service.stage_upload(image_bytes, original_name)
            logger.info(
                "event=template_upload_succeeded request_id=%s bytes=%d suffix=%s token_prefix=%s",
                request_id, len(image_bytes), Path(original_name or "").suffix.lower(), result["token"][:8],
            )
            result["request_id"] = request_id
            return json_response(result, status_code=201)
        except SignMemeError as exc:
            logger.warning("event=template_upload_failed request_id=%s error_code=%s", request_id, exc.code)
            return self._error(exc.message, 400)
        except Exception:
            logger.exception("event=template_upload_failed request_id=%s error_code=internal", request_id)
            return self._error("上传模板图片失败", 500)

    async def api_update_template(self, template_id: str):
        if not self._admin_required():
            return self._error("需要管理员登录", 403)
        data = await self._json_object()
        if data is None:
            return self._error("请求体必须是 JSON 对象", 400)
        try:
            result = self.service.update_template(
                template_id,
                name=data.get("name"),
                description=data.get("description", ""),
                caption=data.get("caption"),
                tags=data.get("tags", []),
                visible_text="",
                rect=data.get("rect"),
            )
            return json_response({"template": result})
        except SignMemeError as exc:
            return self._error(exc.message, 404 if exc.code == "not_found" else 400)
        except Exception:
            logger.exception("[sign_meme] 更新模板失败 template_id=%s", template_id)
            return self._error("更新模板失败", 500)

    async def api_activate_template(self, template_id: str):
        if not self._admin_required():
            return self._error("需要管理员登录", 403)
        try:
            result = self.service.set_active(template_id)
            return json_response({"template": result})
        except SignMemeError as exc:
            return self._error(exc.message, 404 if exc.code == "not_found" else 400)
        except Exception:
            logger.exception("[sign_meme] 激活模板失败 template_id=%s", template_id)
            return self._error("激活模板失败", 500)

    async def api_delete_template(self, template_id: str):
        if not self._admin_required():
            return self._error("需要管理员登录", 403)
        try:
            self.service.delete_template(template_id)
            return json_response({"ok": True})
        except SignMemeError as exc:
            status = 404 if exc.code == "not_found" else 409 if exc.code == "active_template" else 400
            return self._error(exc.message, status)
        except Exception:
            logger.exception("[sign_meme] 删除模板失败 template_id=%s", template_id)
            return self._error("删除模板失败", 500)

    async def api_template_image(self, template_id: str):
        if not self._admin_required():
            return self._error("需要管理员登录", 403)
        try:
            return file_response(str(self.service.get_image_path(template_id)))
        except SignMemeError as exc:
            return self._error(exc.message, 404)

    async def api_template_image_preview(self, template_id: str):
        request_id = self._rid()
        if not self._admin_required():
            self._log_rejected("template_preview_rejected", request_id, 403, "not_authenticated")
            return self._error("需要管理员登录", 403)
        try:
            path = self.service.get_image_path(template_id)
            mime = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
            encoded = base64.b64encode(path.read_bytes()).decode("ascii")
            logger.info("event=template_preview_succeeded request_id=%s template_id=%s bytes=%d", request_id, template_id, path.stat().st_size)
            return json_response({"data_url": f"data:{mime};base64,{encoded}", "request_id": request_id})
        except SignMemeError as exc:
            logger.warning("event=template_preview_failed request_id=%s template_id=%s error_code=%s", request_id, template_id, exc.code)
            return self._error(exc.message, 404)
        except Exception:
            logger.exception("event=template_preview_failed request_id=%s template_id=%s error_code=internal", request_id, template_id)
            return self._error("读取模板预览失败", 500)

    async def api_get_mode(self):
        if not self._admin_required():
            return self._error("需要管理员登录", 403)
        return json_response({
            "sign_mode": self.sign_mode,
            "sign_text_llm_provider": self.sign_text_llm_provider,
            "sign_text_llm_model": self.sign_text_llm_model,
        })

    async def api_reconcile_mode(self):
        """切换模式(AstrBot 插件配置为准,此端点触发后处理)或强制重新对齐。"""
        if not self._admin_required():
            return self._error("需要管理员登录", 403)
        data = await self._json_object()
        if data is None:
            return self._error("请求体必须是 JSON 对象", 400)
        target = str(data.get("sign_mode") or "").strip()
        if target and target not in ("standalone", "integrated"):
            return self._error("sign_mode 必须是 standalone 或 integrated", 400)
        # 配置写入由 AstrBot 配置系统负责;这里执行 reconcile
        # (若请求模式与当前配置不一致,提示先在配置页切换)
        current = self.sign_mode
        if target and target != current:
            return self._error(
                f"配置当前为 {current};请先在插件配置中把 sign_mode 改为 {target} 再触发同步",
                409,
            )
        await self._after_mode_switch()
        return json_response({
            "ok": True,
            "sign_mode": current,
            "reconcile": self.service.reconcile_semantic_pool(),
        })

    async def api_generate(self):
        if not self._admin_required():
            return self._error("需要管理员登录", 403)
        data = await self._json_object()
        if data is None:
            return self._error("请求体必须是 JSON 对象", 400)
        result = self.service.generate(data.get("sign_text"), request_id=secrets.token_hex(8))
        if not result.get("ok"):
            return json_response(result, status_code=422 if not result.get("skipped") else 200)
        return json_response(result)

    async def api_save_generated(self):
        if not self._admin_required():
            return self._error("需要管理员登录", 403)
        data = await self._json_object()
        if data is None or not isinstance(data.get("temporary_path"), str):
            return self._error("temporary_path 必须是字符串", 400)
        result = await self.save_generated(
            data["temporary_path"],
            request_id=str(data.get("request_id") or secrets.token_hex(8)),
        )
        return json_response(result, status_code=201 if result.get("ok") else 400)

    async def generate_for_meme_manager(self, sign_text: str, *, request_id: str | None = None) -> dict:
        """公开给 meme_manager 的 Python 服务接口。"""
        return await asyncio.to_thread(self.service.generate, sign_text, request_id=request_id)

    async def render_for_meme_manager(self, template_id: str, sign_text: str, *, request_id: str | None = None) -> dict:
        """对接模式渲染接口:按检索命中的模板 ID 渲染,不依赖激活状态。"""
        return await asyncio.to_thread(
            self.service.generate_with_template, template_id, sign_text, request_id=request_id
        )

    async def cleanup_generated(self, path: str) -> bool:
        return await asyncio.to_thread(self.service.cleanup, path)

    def get_active_semantic_context(self) -> dict | None:
        """返回与 meme_manager 语义图片记录兼容的当前模板快照。"""
        return self.service.active_semantic_context()

    # ---- 独立模式事件链路(standalone 模式激活) ----

    @filter.on_llm_request()
    async def on_llm_request_sign(self, event: AstrMessageEvent, req: ProviderRequest):
        await standalone_events.handle_llm_request(self, event, req)

    @filter.on_llm_response()
    async def on_llm_response_sign(self, event: AstrMessageEvent, response):
        await standalone_events.handle_llm_response(self, event, response)

    @filter.on_decorating_result()
    async def on_decorating_result_sign(self, event: AstrMessageEvent):
        await standalone_events.handle_decorating_result(self, event)

    @filter.after_message_sent()
    async def after_message_sent_sign(self, event: AstrMessageEvent):
        await standalone_events.handle_after_message_sent(self, event)

    async def cleanup_generated(self, path: str) -> bool:
        return await asyncio.to_thread(self.service.cleanup, path)

    async def save_generated(self, path: str, *, request_id: str | None = None) -> dict:
        """第一版收藏接口：复制临时文件到 saved，并返回永久路径。"""
        request_id = request_id or secrets.token_hex(8)
        source = Path(path).resolve()
        try:
            source.relative_to(self.service.temp_dir)
            if not source.is_file():
                raise SignMemeError("missing_file", "临时图片不存在")
            destination = self.service.permanent_dir / f"{int(time.time())}-{request_id}-{source.name}"
            destination = destination.resolve()
            destination.relative_to(self.service.permanent_dir)
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(source.read_bytes())
            logger.info("event=permanent_save_succeeded request_id=%s filename=%s", request_id, destination.name)
            return {"ok": True, "permanent_path": str(destination), "request_id": request_id}
        except Exception as exc:
            logger.error("event=permanent_save_failed request_id=%s error=%s", request_id, exc, exc_info=True)
            return {"ok": False, "request_id": request_id, "error_code": "save_failed", "error": str(exc)}
