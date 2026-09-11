from __future__ import annotations

import hashlib
import json
import logging
import math
import os
import secrets
import shutil
import threading
import time
import uuid
from pathlib import Path
from typing import Any

from PIL import Image, ImageDraw, ImageFont

try:
    from .meme_manager_mirror import MemeManagerMirror
except ImportError:  # 直接以文件方式运行(非包上下文)
    from backend.meme_manager_mirror import MemeManagerMirror


class SignMemeError(ValueError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


class SignMemeService:
    """Independent sign-template service used by meme_manager."""

    MAX_TEXT_LENGTH = 40
    MAX_TEMPLATES = 100
    ALLOWED_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp"}
    # H3(2026-09-11): staging 上传残留 / generated 临时成品图的 TTL 兜底。
    # 正常路径各自消费(consume_upload/cleanup),失败路径(放弃保存、进程中断、
    # 钩子未触发)曾永久残留——实测 staging 2 个陈旧文件、generated 5 个孤儿。
    TTL_SECONDS = 24 * 3600

    def __init__(self, data_dir: str | Path, logger_: logging.Logger | None = None):
        self.data_dir = Path(data_dir).resolve()
        self.template_dir = self.data_dir / "templates"
        self.permanent_dir = self.data_dir / "saved"
        self.temp_dir = self.data_dir / "generated"
        self.staging_dir = self.data_dir / "staging"
        self.db_path = self.data_dir / "templates.json"
        self.font_path = self.data_dir / "fonts" / "NotoSansSC-Regular.ttf"
        self.meme_manager_mirror = MemeManagerMirror(self.data_dir.parent / "meme_manager")
        try:
            from .semantic_pool import SemanticPoolSync
        except ImportError:
            from backend.semantic_pool import SemanticPoolSync
        # 对接模式语义池同步;standalone 模式下所有方法为 no-op
        self.semantic_pool = SemanticPoolSync(self.data_dir.parent / "meme_manager", logger=None)
        self.sign_mode_provider = None  # main.py 注入: 返回当前 sign_mode 的 callable
        self.logger = logger_ or logging.getLogger("sign_meme")
        # 语义池向量待重建队列: upsert 返回 needs_vector 时登记 entry_id,
        # 由 main.py 异步消费(增量重建 FAISS)。模板语义改动立即生效的关键。
        self._pending_vector_ids: list[str] = []
        self._pending_vector_lock = threading.Lock()
        for path in (self.template_dir, self.permanent_dir, self.temp_dir, self.staging_dir):
            path.mkdir(parents=True, exist_ok=True)
        self._migrate()

    def _migrate(self) -> None:
        if not self.db_path.exists():
            self._write_db({"version": 1, "templates": []})
            return
        try:
            data = self._read_db()
            if not isinstance(data.get("templates"), list):
                raise ValueError("templates 必须为数组")
        except Exception:
            backup = self.db_path.with_suffix(f".corrupt-{int(time.time())}.json")
            shutil.copy2(self.db_path, backup)
            self._write_db({"version": 1, "templates": []})
            self.logger.error("模板数据库损坏，已备份到 %s", backup, exc_info=True)

        # v1 模板已经有 description；将其兼容迁移为 meme_manager 语义模型中的
        # caption，并为后续语义索引补齐 tags/visible_text 字段。
        data = self._read_db()
        changed = False
        for item in data.get("templates", []):
            if "caption" not in item:
                item["caption"] = str(item.get("description") or "").strip()
                changed = True
            if "tags" not in item or not isinstance(item.get("tags"), list):
                item["tags"] = []
                changed = True
            if "visible_text" not in item:
                item["visible_text"] = ""
                changed = True
        if changed:
            self._write_db(data)
            self.logger.info("event=semantic_metadata_migrated schema=sign_meme_v2")

    def _read_db(self) -> dict[str, Any]:
        return json.loads(self.db_path.read_text(encoding="utf-8"))

    def _write_db(self, data: dict[str, Any]) -> None:
        tmp = self.db_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, self.db_path)

    def list_templates(self) -> list[dict[str, Any]]:
        data = self._read_db()
        result = []
        for item in data.get("templates", []):
            copied = dict(item)
            # Plugin Page 位于 Dashboard iframe 中，图片也必须走 Dashboard 的
            # 受控扩展路由；直接返回 /sign_meme/... 会落到 WebUI 根路由，导致
            # 已有模板列表能读到但缩略图请求失败。
            copied["image_url"] = (
                "/api/v1/plugins/extensions/sign_meme"
                f"/image/{copied['id']}"
            )
            result.append(copied)
        return result

    def get_template(self, template_id: str) -> dict[str, Any] | None:
        return next((x for x in self.list_templates() if x.get("id") == template_id), None)

    @staticmethod
    def _validate_name(name: Any) -> str:
        value = str(name or "").strip()
        if not value or len(value) > 80:
            raise SignMemeError("invalid_name", "模板名称不能为空且不能超过80个字符")
        return value

    @staticmethod
    def _validate_quad(quad: Any, width: int, height: int) -> list[list[float]]:
        if not isinstance(quad, list) or len(quad) != 4:
            raise SignMemeError("invalid_quad", "牌面必须包含四个角点")
        points: list[list[float]] = []
        for point in quad:
            if not isinstance(point, (list, tuple)) or len(point) != 2:
                raise SignMemeError("invalid_quad", "每个角点必须是[x,y]")
            x, y = float(point[0]), float(point[1])
            if not math.isfinite(x) or not math.isfinite(y):
                raise SignMemeError("invalid_quad", "角点坐标必须是有限数字")
            if x < 0 or y < 0 or x >= width or y >= height:
                raise SignMemeError("invalid_quad", "角点必须位于图片范围内")
            points.append([round(x, 3), round(y, 3)])
        area = abs(sum(points[i][0] * points[(i + 1) % 4][1] - points[(i + 1) % 4][0] * points[i][1] for i in range(4))) / 2
        if area < 25:
            raise SignMemeError("invalid_quad", "牌面区域过小")
        return points

    @staticmethod
    def _validate_rect(rect: Any, width: int, height: int) -> dict[str, int]:
        if not isinstance(rect, dict):
            raise SignMemeError("invalid_rect", "牌面必须是矩形参数")
        try:
            x = int(round(float(rect.get("x"))))
            y = int(round(float(rect.get("y"))))
            rect_width = int(round(float(rect.get("width"))))
            rect_height = int(round(float(rect.get("height"))))
        except (TypeError, ValueError):
            raise SignMemeError("invalid_rect", "矩形坐标和尺寸必须是数字")
        if rect_width < 16 or rect_height < 16:
            raise SignMemeError("invalid_rect", "牌面宽度和高度不能小于16像素")
        if x < 0 or y < 0 or x + rect_width > width or y + rect_height > height:
            raise SignMemeError("invalid_rect", "牌面矩形必须位于图片范围内")
        return {"x": x, "y": y, "width": rect_width, "height": rect_height}

    @classmethod
    def _template_rect(cls, template: dict[str, Any]) -> dict[str, int]:
        if isinstance(template.get("rect"), dict):
            return cls._validate_rect(template["rect"], int(template["width"]), int(template["height"]))
        quad = template.get("quad")
        if isinstance(quad, list) and len(quad) == 4:
            xs = [float(point[0]) for point in quad]
            ys = [float(point[1]) for point in quad]
            return cls._validate_rect(
                {"x": min(xs), "y": min(ys), "width": max(xs) - min(xs), "height": max(ys) - min(ys)},
                int(template["width"]),
                int(template["height"]),
            )
        raise SignMemeError("invalid_rect", "模板缺少牌面矩形")

    def _resolve_path(self, relative_path: str) -> Path:
        path = (self.data_dir / relative_path).resolve()
        try:
            path.relative_to(self.data_dir)
        except ValueError as exc:
            raise SignMemeError("unsafe_path", "文件路径超出插件数据目录") from exc
        return path

    def stage_upload(self, image_bytes: bytes, original_name: str) -> dict[str, str]:
        if len(image_bytes) > 32 * 1024 * 1024:
            raise SignMemeError("image_too_large", "模板图片不能超过32MB")
        suffix = Path(original_name or "template.png").suffix.lower()
        if suffix not in self.ALLOWED_EXTENSIONS:
            raise SignMemeError("unsupported_image", "只支持 PNG/JPG/JPEG/WEBP")
        token = uuid.uuid4().hex
        (self.staging_dir / f"{token}{suffix}").write_bytes(image_bytes)
        self.logger.info("event=template_upload_staged token=%s", token)
        return {"token": token, "filename": str(original_name or "template.png")[:255]}

    def consume_upload(self, token: str) -> tuple[bytes, str]:
        safe_token = Path(str(token or "")).name
        if safe_token != str(token or "") or not safe_token:
            raise SignMemeError("invalid_upload", "上传令牌非法")
        candidates = list(self.staging_dir.glob(f"{safe_token}.*"))
        if len(candidates) != 1 or not candidates[0].is_file():
            raise SignMemeError("upload_not_found", "上传文件不存在或已过期")
        path = candidates[0]
        data = path.read_bytes()
        path.unlink(missing_ok=True)
        return data, path.name

    @staticmethod
    def _normalize_tags(tags: Any) -> list[str]:
        if isinstance(tags, str):
            tags = tags.replace("，", ",").split(",")
        if not isinstance(tags, (list, tuple, set)):
            return []
        result: list[str] = []
        seen: set[str] = set()
        for value in tags:
            tag = str(value or "").strip()
            if tag and tag not in seen and len(tag) <= 50:
                seen.add(tag)
                result.append(tag)
        return result[:30]

    def create_template(self, *, name: Any, description: Any, caption: Any = None, tags: Any = None, visible_text: Any = "", image_bytes: bytes, original_name: str, rect: Any = None, quad: Any = None) -> dict[str, Any]:
        if len(image_bytes) > 32 * 1024 * 1024:
            raise SignMemeError("image_too_large", "模板图片不能超过32MB")
        suffix = Path(original_name or "template.png").suffix.lower()
        if suffix not in self.ALLOWED_EXTENSIONS:
            raise SignMemeError("unsupported_image", "只支持 PNG/JPG/JPEG/WEBP")
        try:
            from io import BytesIO
            with Image.open(BytesIO(image_bytes)) as source:
                image = source.convert("RGBA")
                width, height = image.size
                image.verify()
        except SignMemeError:
            raise
        except Exception as exc:
            raise SignMemeError("invalid_image", "模板图片无法读取") from exc
        data = self._read_db()
        templates = data["templates"]
        if len(templates) >= self.MAX_TEMPLATES:
            raise SignMemeError("too_many_templates", "模板数量已达到上限")
        if rect is None and quad is not None:
            rect = {"x": min(point[0] for point in quad), "y": min(point[1] for point in quad), "width": max(point[0] for point in quad) - min(point[0] for point in quad), "height": max(point[1] for point in quad) - min(point[1] for point in quad)}
        if rect is None:
            rect = {"x": width * 0.2, "y": height * 0.3, "width": width * 0.6, "height": height * 0.4}
        validated_rect = self._validate_rect(rect, width, height)
        template_id = uuid.uuid4().hex
        filename = f"{template_id}.png"
        relative = f"templates/{filename}"
        image.save(self.data_dir / relative, "PNG")
        now = int(time.time())
        item = {
            "id": template_id,
            "name": self._validate_name(name),
            "description": str(description or "").strip()[:500],
            "caption": str(caption if caption is not None else description or "").strip()[:1000],
            "tags": self._normalize_tags(tags),
            "visible_text": str(visible_text or "").strip()[:500],
            "relative_path": relative,
            "original_name": str(original_name or "template.png")[:255],
            "width": width,
            "height": height,
            "rect": validated_rect,
            "active": not any(bool(x.get("active")) for x in templates),
            "created_at": now,
            "updated_at": now,
            "version": 1,
            "sha256": hashlib.sha256(image_bytes).hexdigest(),
        }
        templates.append(item)
        self._write_db(data)
        result = self.get_template(template_id)  # type: ignore[assignment]
        try:
            self.meme_manager_mirror.sync(item, self._resolve_path(relative))
        except Exception as exc:
            data["templates"] = [candidate for candidate in data["templates"] if candidate.get("id") != template_id]
            self._write_db(data)
            (self.data_dir / relative).unlink(missing_ok=True)
            self.logger.error("event=template_mirror_failed template_id=%s error=%s", template_id, type(exc).__name__, exc_info=True)
            raise SignMemeError("mirror_failed", "模板目录同步失败，创建已回滚") from exc
        self.logger.info("event=template_mirror_succeeded template_id=%s pack_id=sign-meme-templates", template_id)
        self._pool_sync_template(item)
        self.logger.info("event=template_created template_id=%s active=%s", template_id, item["active"])
        return result  # type: ignore[return-value]

    def update_template(self, template_id: str, *, name: Any, description: Any, caption: Any = None, tags: Any = None, visible_text: Any = "", rect: Any = None, quad: Any = None) -> dict[str, Any]:
        data = self._read_db()
        item = next((x for x in data["templates"] if x.get("id") == template_id), None)
        if item is None:
            raise SignMemeError("not_found", "模板不存在")
        self._validate_name(name)
        if rect is None:
            rect = quad or item.get("rect") or self._template_rect(item)
        validated_rect = self._validate_rect(rect, int(item["width"]), int(item["height"]))
        item.update({
            "name": str(name).strip(),
            "description": str(description or "").strip()[:500],
            "caption": str(caption if caption is not None else description or "").strip()[:1000],
            "tags": self._normalize_tags(tags),
            "visible_text": str(visible_text or "").strip()[:500],
            "rect": validated_rect,
            "updated_at": int(time.time()),
            "version": int(item.get("version", 1)) + 1,
        })
        # M2(2026-09-11): 镜像同步成功后才落 DB。此前先写 DB 再 sync,
        # sync 失败时 DB 已是新值而镜像/语义池停在旧值,直到下次成功保存才收敛。
        try:
            self.meme_manager_mirror.sync(item, self._resolve_path(str(item["relative_path"])))
        except Exception as exc:
            self.logger.error("event=template_mirror_failed template_id=%s operation=update error=%s", template_id, type(exc).__name__, exc_info=True)
            raise SignMemeError("mirror_failed", "模板目录同步失败") from exc
        self._write_db(data)
        self.logger.info("event=template_mirror_succeeded template_id=%s pack_id=sign-meme-templates operation=update", template_id)
        updated = self.get_template(template_id)  # type: ignore[assignment]
        self._pool_sync_template(item)
        self.logger.info("event=template_updated template_id=%s version=%s", template_id, item["version"])
        return updated  # type: ignore[return-value]

    def set_active(self, template_id: str) -> dict[str, Any]:
        data = self._read_db()
        found = False
        for item in data["templates"]:
            active = item.get("id") == template_id
            if active:
                found = True
            item["active"] = active
            if active:
                item["updated_at"] = int(time.time())
        if not found:
            raise SignMemeError("not_found", "模板不存在")
        # M2(2026-09-11): 全部镜像同步成功后才落 DB,避免部分同步后 DB 独走。
        # (mirror.sync 自带 snapshot/rollback,单条失败会还原该条镜像。)
        try:
            for item in data["templates"]:
                self.meme_manager_mirror.sync(item, self._resolve_path(str(item["relative_path"])))
        except Exception as exc:
            self.logger.error("event=template_mirror_failed template_id=%s operation=activate error=%s", template_id, type(exc).__name__, exc_info=True)
            raise SignMemeError("mirror_failed", "模板目录同步失败") from exc
        self._write_db(data)
        self.logger.info("event=template_mirror_succeeded template_id=%s pack_id=sign-meme-templates operation=activate", template_id)
        self.logger.info("event=template_activated template_id=%s", template_id)
        return self.get_template(template_id)  # type: ignore[return-value]

    def delete_template(self, template_id: str) -> None:
        data = self._read_db()
        item = next((x for x in data["templates"] if x.get("id") == template_id), None)
        if item is None:
            raise SignMemeError("not_found", "模板不存在")
        if item.get("active"):
            raise SignMemeError("active_template", "不能直接删除激活模板，请先激活其他模板")
        data["templates"] = [x for x in data["templates"] if x.get("id") != template_id]
        try:
            self.meme_manager_mirror.remove(template_id)
        except Exception as exc:
            self.logger.error("event=template_mirror_failed template_id=%s operation=delete error=%s", template_id, type(exc).__name__, exc_info=True)
            raise SignMemeError("mirror_failed", "模板目录同步失败，删除已取消") from exc
        self._write_db(data)
        path = self._resolve_path(str(item["relative_path"]))
        if path.exists():
            path.unlink()
        self.logger.info("event=template_mirror_succeeded template_id=%s pack_id=sign-meme-templates operation=delete", template_id)
        self._pool_remove_template(template_id)
        self.logger.info("event=template_deleted template_id=%s", template_id)

    def active_template(self) -> dict[str, Any] | None:
        return next((x for x in self._read_db().get("templates", []) if x.get("active")), None)

    def active_semantic_context(self) -> dict[str, Any] | None:
        item = self.active_template()
        if not item:
            return None
        return {
            "template_id": item.get("id", ""),
            "name": item.get("name", ""),
            "caption": item.get("caption") or item.get("description", ""),
            "tags": self._normalize_tags(item.get("tags")),
            "visible_text": "",
            "caption_status": "done" if str(item.get("caption") or item.get("description") or "").strip() else "pending",
            "embedding_status": "not_indexed",
        }

    def _font(self, size: int) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
        # L4(2026-09-11): 字体对象缓存。此前每次渲染每个字号都重新
        # truetype() 加载(最多 ~40 次/渲染),全启发式循环里纯属重复 IO。
        cache = getattr(self, "_font_cache", None)
        if cache is None:
            cache = self._font_cache = {}
        if size in cache:
            return cache[size]
        candidates = [
            self.font_path,
            Path(__file__).resolve().parent.parent / "fonts" / "NotoSansSC-Regular.ttf",
        ]
        font: ImageFont.FreeTypeFont | ImageFont.ImageFont
        for path in candidates:
            if path.exists():
                font = ImageFont.truetype(str(path), size=size)
                cache[size] = font
                return font
        font = ImageFont.load_default()
        cache[size] = font
        return font

    @staticmethod
    def _homography(src: list[tuple[float, float]], dst: list[tuple[float, float]]) -> tuple[float, ...]:
        # Solve the 8-parameter projective transform mapping src -> dst.
        a: list[list[float]] = []
        b: list[float] = []
        for (x, y), (u, v) in zip(src, dst):
            a.append([x, y, 1, 0, 0, 0, -u * x, -u * y]); b.append(u)
            a.append([0, 0, 0, x, y, 1, -v * x, -v * y]); b.append(v)
        for col in range(8):
            pivot = max(range(col, 8), key=lambda r: abs(a[r][col]))
            if abs(a[pivot][col]) < 1e-10:
                raise SignMemeError("invalid_quad", "牌面透视变换不可计算")
            a[col], a[pivot] = a[pivot], a[col]; b[col], b[pivot] = b[pivot], b[col]
            divisor = a[col][col]
            a[col] = [v / divisor for v in a[col]]; b[col] /= divisor
            for row in range(8):
                if row == col: continue
                factor = a[row][col]
                a[row] = [a[row][j] - factor * a[col][j] for j in range(8)]; b[row] -= factor * b[col]
        return (*b, 1.0)

    MIN_FONT_SIZE = 12  # 牌面文字最小字号；再小不可读，宁可换行

    def _measure(self, draw: ImageDraw.ImageDraw, text: str, font, stroke: int) -> tuple[int, int]:
        bbox = draw.textbbox((0, 0), text, font=font, stroke_width=stroke)
        return bbox[2] - bbox[0], bbox[3] - bbox[1]

    def _stroke_for(self, font_size: int) -> int:
        return max(1, font_size // 18)

    def _render_text_layer(self, text: str, width: int, height: int) -> Image.Image:
        """牌面排版规则(2026-09-10 新增):
        1. 字号自适应: 优先把整句放进单行,从大到小尝试;
        2. 尽量不换行: 只有单行在 MIN_FONT_SIZE 仍放不下时,才退回多行折行;
        3. 居中 + 深色字/白描边策略不变。
        """
        margin = max(8, min(width, height) // 18)
        max_width = max(1, width - margin * 2)
        max_height = max(1, height - margin * 2)
        probe = ImageDraw.Draw(Image.new("RGBA", (1, 1)))

        # 阶段一: 单行自适应。找到能单行放下的最大字号。
        for font_size in range(max(12, min(width, height) // 2), self.MIN_FONT_SIZE - 1, -2):
            font = self._font(font_size)
            stroke = self._stroke_for(font_size)
            text_w, text_h = self._measure(probe, text, font, stroke)
            if text_w <= max_width and text_h <= max_height:
                layer = Image.new("RGBA", (width, height), (0, 0, 0, 0))
                draw = ImageDraw.Draw(layer)
                bbox = draw.textbbox((0, 0), text, font=font, stroke_width=stroke)
                draw.text(((width - text_w) // 2 - bbox[0], (height - text_h) // 2 - bbox[1]),
                          text, font=font, fill=(20, 20, 20, 255),
                          stroke_width=stroke, stroke_fill=(255, 255, 255, 255))
                return layer

        # 阶段二: 换行兜底(与旧行为一致,但字号下限受 MIN_FONT_SIZE 保护)
        for font_size in range(max(12, min(width, height) // 2), self.MIN_FONT_SIZE - 1, -2):
            font = self._font(font_size)
            lines: list[str] = []
            current = ""
            for char in text:
                candidate = current + char
                bbox = font.getbbox(candidate)
                if current and bbox[2] - bbox[0] > max_width:
                    lines.append(current); current = char
                else:
                    current = candidate
            if current: lines.append(current)
            line_height = max(font_size + 4, font.getbbox("国")[3] - font.getbbox("国")[1] + 4)
            total_height = line_height * len(lines)
            if total_height <= height - margin * 2:
                layer = Image.new("RGBA", (width, height), (0, 0, 0, 0))
                draw = ImageDraw.Draw(layer)
                y = (height - total_height) // 2
                for line in lines:
                    bbox = draw.textbbox((0, 0), line, font=font, stroke_width=max(1, font_size // 18))
                    x = (width - (bbox[2] - bbox[0])) // 2
                    draw.text((x, y), line, font=font, fill=(20, 20, 20, 255), stroke_width=max(1, font_size // 18), stroke_fill=(255, 255, 255, 255))
                    y += line_height
                return layer
        raise SignMemeError("text_overflow", "牌面文字超过渲染限制")

    # ---- 对接模式语义池同步(模式感知) ----

    def _sign_mode(self) -> str:
        if callable(self.sign_mode_provider):
            try:
                return str(self.sign_mode_provider() or "standalone")
            except Exception:
                pass
        return "standalone"

    def _pool_sync_template(self, item: dict[str, Any]) -> None:
        """integrated 模式下把模板写入主 pack 语义池(失败只记日志,不阻断)。"""
        if self._sign_mode() != "integrated":
            return
        try:
            result = self.semantic_pool.upsert(item, self._resolve_path(str(item["relative_path"])))
            if result.get("ok"):
                entry_id = str(result.get("entry_id") or "")
                if result.get("needs_vector") and entry_id:
                    self._queue_vector_rebuild(entry_id)
                self.logger.info(
                    "event=sign_pool_synced template_id=%s entry_id=%s needs_vector=%s",
                    item.get("id"), entry_id[:12], bool(result.get("needs_vector")),
                )
            else:
                self.logger.warning("event=sign_pool_sync_skipped template_id=%s reason=%s", item.get("id"), result.get("reason"))
        except Exception as exc:
            self.logger.error("event=sign_pool_sync_failed template_id=%s error=%s", item.get("id"), type(exc).__name__, exc_info=True)

    def _queue_vector_rebuild(self, entry_id: str) -> None:
        """登记待重建向量的 entry(去重,保持插入序;供 main.py 异步消费)。"""
        with self._pending_vector_lock:
            if entry_id not in self._pending_vector_ids:
                self._pending_vector_ids.append(entry_id)

    def drain_pending_vector_ids(self) -> list[str]:
        """原子取出全部待重建 entry_id(空队列返回 [],不触发任何 IO)。"""
        with self._pending_vector_lock:
            pending, self._pending_vector_ids = self._pending_vector_ids, []
            return pending

    def _pool_remove_template(self, template_id: str) -> None:
        if self._sign_mode() != "integrated":
            return
        try:
            self.semantic_pool.remove(template_id)
        except Exception as exc:
            self.logger.error("event=sign_pool_remove_failed template_id=%s error=%s", template_id, type(exc).__name__, exc_info=True)

    def reconcile_semantic_pool(self) -> dict:
        """模式切换/启动时对齐语义池。返回操作摘要。"""
        mode = self._sign_mode()
        templates = self._read_db().get("templates", [])
        try:
            def resolver(t: dict) -> Path | None:
                try:
                    return self._resolve_path(str(t.get("relative_path")))
                except Exception:
                    return None
            result = self.semantic_pool.reconcile(mode, templates, resolver)
            self.logger.info("event=sign_pool_reconciled mode=%s ops=%s", mode, result)
            return result
        except Exception as exc:
            self.logger.error("event=sign_pool_reconcile_failed mode=%s error=%s", mode, type(exc).__name__, exc_info=True)
            return {"ok": False, "reason": str(exc)}

    def _render_with_template(self, template: dict[str, Any], text: str, *, request_id: str) -> dict[str, Any]:
        """按给定模板渲染牌面(公共核心,generate/generate_with_template 共用)。"""
        started = time.monotonic()
        try:
            image_path = self._resolve_path(str(template["relative_path"]))
            with Image.open(image_path) as source:
                base = source.convert("RGBA")
            rect = self._template_rect(template)
            target_w = rect["width"]; target_h = rect["height"]
            layer = self._render_text_layer(text, target_w, target_h)
            base.alpha_composite(layer, (rect["x"], rect["y"]))
            filename = f"{int(time.time())}-{request_id}-{uuid.uuid4().hex}.png"
            output = self.temp_dir / filename
            base.save(output, "PNG")
            elapsed = int((time.monotonic() - started) * 1000)
            self.logger.info("event=render_succeeded request_id=%s template_id=%s duration_ms=%d", request_id, template["id"], elapsed)
            return {"ok": True, "request_id": request_id, "template_id": template["id"], "temporary_path": str(output), "cleanup_token": filename, "duration_ms": elapsed}
        except SignMemeError:
            raise
        except Exception as exc:
            self.logger.error("event=render_failed request_id=%s template_id=%s error=%s", request_id, template.get("id"), exc, exc_info=True)
            return {"ok": False, "request_id": request_id, "error_code": "render_failed", "error": str(exc)}

    def generate(self, sign_text: Any, *, request_id: str | None = None) -> dict[str, Any]:
        """用当前激活模板渲染(独立模式/管理页预览用)。"""
        request_id = request_id or secrets.token_hex(8)
        text = str(sign_text or "").strip()
        if not text:
            return {"ok": False, "skipped": True, "error_code": "empty_text", "request_id": request_id}
        if len(text) > self.MAX_TEXT_LENGTH:
            self.logger.warning("event=render_skipped request_id=%s error_code=text_overflow length=%d", request_id, len(text))
            return {"ok": False, "skipped": True, "error_code": "text_overflow", "request_id": request_id}
        template = self.active_template()
        if not template:
            self.logger.warning("event=render_skipped request_id=%s error_code=no_active_template", request_id)
            return {"ok": False, "skipped": True, "error_code": "no_active_template", "request_id": request_id}
        return self._render_with_template(template, text, request_id=request_id)

    def generate_with_template(self, template_id: str, sign_text: Any, *, request_id: str | None = None) -> dict[str, Any]:
        """按指定模板 ID 渲染,不依赖激活状态(对接模式检索命中后调用)。"""
        request_id = request_id or secrets.token_hex(8)
        text = str(sign_text or "").strip()
        if not text:
            return {"ok": False, "skipped": True, "error_code": "empty_text", "request_id": request_id}
        if len(text) > self.MAX_TEXT_LENGTH:
            self.logger.warning("event=render_skipped request_id=%s error_code=text_overflow length=%d", request_id, len(text))
            return {"ok": False, "skipped": True, "error_code": "text_overflow", "request_id": request_id}
        template = self.get_template(str(template_id or "").strip())
        if not template:
            self.logger.warning("event=render_skipped request_id=%s error_code=template_not_found template_id=%s", request_id, template_id)
            return {"ok": False, "skipped": True, "error_code": "template_not_found", "request_id": request_id}
        return self._render_with_template(template, text, request_id=request_id)

    def cleanup(self, path: str) -> bool:
        try:
            resolved = Path(path).resolve()
            resolved.relative_to(self.temp_dir)
            if resolved.exists(): resolved.unlink()
            self.logger.info("event=temporary_cleanup_succeeded path=%s", resolved.name)
            return True
        except Exception as exc:
            self.logger.error("event=temporary_cleanup_failed path=%s error=%s", path, exc, exc_info=True)
            return False

    def sweep_expired(self, *, max_age_seconds: int | None = None) -> dict[str, int]:
        """H3 TTL 兜底:清扫 staging/ 与 generated/ 中超过时限的残留文件。

        只动这两个临时目录,templates/saved 永不触碰。启动时与每次
        after_message_sent 顺带调用(见 main.py),失败只记日志不抛。
        """
        max_age = self.TTL_SECONDS if max_age_seconds is None else max_age_seconds
        now = time.time()
        removed = {"staging": 0, "generated": 0}
        for name in ("staging", "generated"):
            directory = self.staging_dir if name == "staging" else self.temp_dir
            try:
                for f in directory.iterdir():
                    if not f.is_file():
                        continue
                    try:
                        if now - f.stat().st_mtime > max_age:
                            f.unlink()
                            removed[name] += 1
                    except OSError:
                        continue
            except OSError:
                continue
        if any(removed.values()):
            self.logger.info(
                "event=expired_temp_swept staging=%d generated=%d max_age_seconds=%d",
                removed["staging"], removed["generated"], max_age,
            )
        return removed

    def get_image_path(self, template_id: str) -> Path:
        item = self.get_template(template_id)
        if not item:
            raise SignMemeError("not_found", "模板不存在")
        path = self._resolve_path(str(item["relative_path"]))
        if not path.is_file():
            raise SignMemeError("missing_file", "模板图片不存在")
        return path
