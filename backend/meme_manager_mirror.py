from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import tempfile
from pathlib import Path
from typing import Any


PACK_ID = "sign-meme-templates"
PACK_NAME = "举牌模板"
PACK_VERSION = "1.0.0"
CATEGORY_NAME = "举牌模板"
CATEGORY_DESCRIPTION = "仅用于管理举牌渲染底图；不参与普通表情包检索或直接发送。"
_TEMPLATE_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z")


class MemeManagerMirror:
    """Transactional derived mirror of sign_meme templates in a manager pack."""

    def __init__(self, meme_manager_dir: str | Path):
        self.root = Path(meme_manager_dir).resolve()
        self.packs_dir = self.root / "packs"
        self.pack_dir = self.packs_dir / PACK_ID
        self.image_dir = self.pack_dir / "memes" / CATEGORY_NAME
        self.registry_path = self.root / "registry.json"
        self.manifest_path = self.pack_dir / "manifest.json"
        self.descriptions_path = self.pack_dir / "memes_data.json"
        self.metadata_path = self.pack_dir / "sign_meme_mirror.json"

    @staticmethod
    def _read_json(path: Path, default: dict[str, Any]) -> dict[str, Any]:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else default
        except (OSError, ValueError, json.JSONDecodeError):
            return default

    @staticmethod
    def _atomic_json(path: Path, value: dict[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(value, handle, ensure_ascii=False, indent=2)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp_name, path)
        finally:
            if os.path.exists(tmp_name):
                os.unlink(tmp_name)

    @staticmethod
    def _snapshot(path: Path) -> bytes | None:
        return path.read_bytes() if path.is_file() else None

    @staticmethod
    def _restore(path: Path, snapshot: bytes | None) -> None:
        if snapshot is None:
            path.unlink(missing_ok=True)
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".rollback", dir=path.parent)
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(snapshot)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp_name, path)
        finally:
            if os.path.exists(tmp_name):
                os.unlink(tmp_name)

    @staticmethod
    def _template_id(template_id: Any) -> str:
        value = str(template_id or "").strip()
        if not _TEMPLATE_ID_RE.fullmatch(value):
            raise ValueError("template_id 非法")
        return value

    def _image_path(self, template_id: str) -> Path:
        image = (self.image_dir / f"{template_id}.png").resolve()
        try:
            image.relative_to(self.image_dir.resolve())
        except ValueError as exc:
            raise ValueError("镜像图片路径越界") from exc
        return image

    def _ensure_pack(self) -> None:
        self.image_dir.mkdir(parents=True, exist_ok=True)
        registry = self._read_json(self.registry_path, {"schema_version": 1, "installed_packs": []})
        installed = registry.get("installed_packs")
        if not isinstance(installed, list):
            installed = []
        entry = {"id": PACK_ID, "name": PACK_NAME, "version": PACK_VERSION, "enabled": True}
        registry["schema_version"] = 1
        registry["installed_packs"] = [
            entry if isinstance(item, dict) and item.get("id") == PACK_ID else item
            for item in installed
        ]
        if not any(isinstance(item, dict) and item.get("id") == PACK_ID for item in installed):
            registry["installed_packs"].append(entry)
        self._atomic_json(self.registry_path, registry)
        self._atomic_json(
            self.manifest_path,
            {
                "schema_version": 1,
                "id": PACK_ID,
                "name": PACK_NAME,
                "version": PACK_VERSION,
                "description": CATEGORY_DESCRIPTION,
                "tags": ["runtime", "sign-template", "management-only"],
                "categories": {CATEGORY_NAME: {"description": CATEGORY_DESCRIPTION}},
            },
        )
        self._atomic_json(self.descriptions_path, {CATEGORY_NAME: CATEGORY_DESCRIPTION})

    def sync(self, template: dict[str, Any], source_image: str | Path) -> dict[str, Any]:
        template_id = self._template_id(template.get("id"))
        source = Path(source_image).resolve()
        if not source.is_file():
            raise FileNotFoundError("举牌模板源图片不存在")
        target = self._image_path(template_id)
        tracked = [self.registry_path, self.manifest_path, self.descriptions_path, self.metadata_path, target]
        snapshots = {path: self._snapshot(path) for path in tracked}
        try:
            self._ensure_pack()
            target.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp_name = tempfile.mkstemp(prefix=f".{template_id}.", suffix=".png", dir=target.parent)
            os.close(fd)
            try:
                shutil.copy2(source, tmp_name)
                os.replace(tmp_name, target)
            finally:
                if os.path.exists(tmp_name):
                    os.unlink(tmp_name)
            metadata = self._read_json(self.metadata_path, {"schema_version": 1, "templates": {}})
            templates = metadata.get("templates")
            if not isinstance(templates, dict):
                templates = {}
            data = target.read_bytes()
            templates[template_id] = {
                "template_id": template_id,
                "name": str(template.get("name") or "").strip(),
                "caption": str(template.get("caption") or "").strip(),
                "tags": [str(tag).strip() for tag in template.get("tags", []) if str(tag).strip()],
                "active": bool(template.get("active")),
                "rect": template.get("rect") if isinstance(template.get("rect"), dict) else {},
                "source_updated_at": template.get("updated_at"),
                "relative_path": f"memes/{CATEGORY_NAME}/{template_id}.png",
                "sha256": hashlib.sha256(data).hexdigest(),
            }
            metadata["schema_version"] = 1
            metadata["templates"] = templates
            self._atomic_json(self.metadata_path, metadata)
            return dict(templates[template_id])
        except Exception:
            for path in reversed(tracked):
                self._restore(path, snapshots[path])
            raise

    def remove(self, template_id: str) -> bool:
        template_id = self._template_id(template_id)
        target = self._image_path(template_id)
        tracked = [self.metadata_path, target]
        snapshots = {path: self._snapshot(path) for path in tracked}
        try:
            metadata = self._read_json(self.metadata_path, {"schema_version": 1, "templates": {}})
            templates = metadata.get("templates")
            if not isinstance(templates, dict) or template_id not in templates:
                return False
            del templates[template_id]
            metadata["schema_version"] = 1
            metadata["templates"] = templates
            self._atomic_json(self.metadata_path, metadata)
            target.unlink(missing_ok=True)
            return True
        except Exception:
            for path in reversed(tracked):
                self._restore(path, snapshots[path])
            raise
