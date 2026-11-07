"""章节图集：上传、读取、缩略图、删除、图片记忆与发送给猫娘。"""

from __future__ import annotations

import asyncio
import time
import uuid
from typing import Any

from plugin.sdk.plugin import Err, Ok, SdkError, plugin_entry, tr
from plugin.sdk.shared.core.router import PluginRouter

from .. import (
    IMAGE_DIR_NAME,
    IMAGE_LIST_LIMIT,
    IMAGE_MAX_BYTES,
    IMAGE_MEMORY_MAX_CHARS,
    PLUGIN_ID,
)
from ..chronicle.models import local_day_key


class ImagesRouter(PluginRouter):
    """章节图集：上传、读取、缩略图、删除、图片记忆与发送给猫娘。"""

    def __init__(self):
        super().__init__(name="images")

    @plugin_entry(
        id="list_images",
        name=tr("entries.listImages.name", default="列出章节图片"),
        description=tr(
            "entries.listImages.description",
            default="列出某一天（日期留空则全部日期）章节图集里保存的图片及地址，"
            "用于浏览页的图片入口与「相册合集」。只返回元信息，不返回图片本体。",
        ),
        input_schema={
            "type": "object",
            "properties": {
                "date": {"type": "string", "description": tr("fields.date", default="日期（YYYY-MM-DD）")},
                "limit": {
                    "type": "integer",
                    "description": tr("fields.imageLimit", default="最多返回多少张"),
                    "default": IMAGE_LIST_LIMIT,
                },
            },
        },
        llm_result_fields=["date", "count"],
    )
    async def entry_list_images(self, date: str = "", limit: int = IMAGE_LIST_LIMIT, **_):
        if self.main_plugin.store is None:
            return Err(SdkError("插件尚未初始化完成"))
        day = self.main_plugin._normalize_day(date)
        items = await asyncio.to_thread(self.main_plugin._image_items, day, limit=int(limit or 0))
        return Ok({"date": day, "items": items, "count": len(items)})

    @plugin_entry(
        id="get_image_data",
        name=tr("entries.getImageData.name", default="读取单张图片原图"),
        description=tr(
            "entries.getImageData.description",
            default="按日期+文件名读取某张图片的原图字节（base64 data URL），"
            "用于浏览页点开大图 / 新标签页打开。列表接口只返回缩略图，原图按需调用本接口。",
        ),
        input_schema={
            "type": "object",
            "properties": {
                "date": {"type": "string", "description": tr("fields.date", default="日期（YYYY-MM-DD）")},
                "filename": {"type": "string", "description": tr("fields.imageFilename", default="图片文件名")},
            },
            "required": ["date", "filename"],
        },
    )
    async def entry_get_image_data(self, date: str = "", filename: str = "", **_):
        if self.main_plugin.store is None:
            return Err(SdkError("插件尚未初始化完成"))
        if not date or not filename:
            return Err(SdkError("需要 date 和 filename"))
        path = self.main_plugin._image_file(date, filename)
        if path is None:
            return Err(SdkError("图片不存在或文件名非法"))
        # 大图（>1.5MB）用 Pillow 转成 JPEG（最长边 1920px，质量 85），
        # 避免 base64 后 ~9MB 撑爆 /runs/export 接口；小图直接返回原字节。
        try:
            size = path.stat().st_size
        except OSError:
            size = 0
        if size > 1_500_000:
            data_url = self.main_plugin._compress_to_jpeg_data_url(path, max_side=1920, quality=85)
            if not data_url:
                data_url = self.main_plugin._path_to_data_url(path)  # 压缩失败退回原图
        else:
            data_url = self.main_plugin._path_to_data_url(path)
        if not data_url:
            return Err(SdkError("读取图片字节失败"))
        return Ok({"date": date, "filename": filename, "data": data_url})

    @plugin_entry(
        id="get_image_thumb",
        name=tr("entries.getImageThumb.name", default="读取单张图片缩略图"),
        description=tr(
            "entries.getImageThumb.description",
            default="按日期+文件名读取某张图片的缩略图（base64 data URL）；"
            "浏览页相册按需懒加载缩略图时调用，避免列表接口一次返回大量图片数据。",
        ),
        input_schema={
            "type": "object",
            "properties": {
                "date": {"type": "string", "description": tr("fields.date", default="日期（YYYY-MM-DD）")},
                "filename": {"type": "string", "description": tr("fields.imageFilename", default="图片文件名")},
            },
            "required": ["date", "filename"],
        },
        llm_result_fields=["date", "filename"],
    )
    async def entry_get_image_thumb(self, date: str = "", filename: str = "", **_):
        if self.main_plugin.store is None:
            return Err(SdkError("插件尚未初始化完成"))
        if not date or not filename:
            return Err(SdkError("需要 date 和 filename"))
        path = self.main_plugin._image_file(date, filename)
        if path is None:
            return Err(SdkError("图片不存在或文件名非法"))

        def _build() -> str:
            thumb = self.main_plugin._ensure_thumb(path)
            if thumb is None:
                return ""
            return self.main_plugin._path_to_data_url(thumb)

        data_url = await asyncio.to_thread(_build)
        if not data_url:
            return Err(SdkError("缩略图不可用（未安装 Pillow 或图片损坏）"))
        return Ok({"date": date, "filename": filename, "data": data_url})

    @plugin_entry(
        id="upload_image",
        name=tr("entries.uploadImage.name", default="上传章节图片"),
        description=tr(
            "entries.uploadImage.description",
            default="把一张图片保存进某一天章节的图集（浏览页点图片入口即可上传）。"
            "data 传 data URL 或纯 base64；支持 png/jpg/gif/webp/bmp，单张最多 25 MB。",
        ),
        input_schema={
            "type": "object",
            "properties": {
                "date": {"type": "string", "description": tr("fields.date", default="日期（YYYY-MM-DD，留空为今天）")},
                "filename": {"type": "string", "description": tr("fields.imageFilename", default="原始文件名（用于识别格式）")},
                "data": {"type": "string", "description": tr("fields.imageData", default="图片内容：data URL 或 base64")},
            },
            "required": ["data"],
        },
        llm_result_fields=["date", "count"],
    )
    async def entry_upload_image(
        self, date: str = "", filename: str = "", data: str = "", **_
    ):
        if self.main_plugin.store is None:
            return Err(SdkError("插件尚未初始化完成"))
        day = self.main_plugin._normalize_day(date) or self.main_plugin._panel_day or local_day_key()
        if not day:
            return Err(SdkError("日期格式应为 YYYY-MM-DD"))
        root = self.main_plugin._ensure_images_dir()
        if root is None:
            return Err(SdkError("图片目录不可用，请重启插件后再试"))
        binary, mime = self.main_plugin._decode_image_payload(data)
        if not binary:
            return Err(SdkError("图片内容为空或不是合法的 base64"))
        if len(binary) > IMAGE_MAX_BYTES:
            return Err(
                SdkError(
                    f"图片太大（{len(binary) / 1024 / 1024:.1f} MB），单张最多 "
                    f"{IMAGE_MAX_BYTES // 1024 // 1024} MB"
                )
            )
        ext = self.main_plugin._image_ext(filename, mime)
        if not ext:
            return Err(SdkError("不支持的图片格式，请上传 png/jpg/gif/webp/bmp"))
        stored = f"{int(time.time() * 1000)}_{uuid.uuid4().hex[:8]}{ext}"
        path = root / day / stored
        try:
            await asyncio.to_thread(self.main_plugin._atomic_write_bytes, path, binary)
        except OSError as exc:
            return Err(SdkError(f"保存图片失败：{exc}"))
        thumb = await asyncio.to_thread(self.main_plugin._ensure_thumb, path)
        self.main_plugin.logger.info("章节图片已保存：{}（{} 字节）", path, len(binary))
        return Ok(
            {
                "date": day,
                "count": self.main_plugin._image_count(day),
                "item": {
                    "date": day,
                    "filename": stored,
                    "url": f"/plugin/{PLUGIN_ID}/ui/{IMAGE_DIR_NAME}/{day}/{stored}",
                    "thumb_url": (
                        self.main_plugin._thumb_url(day, thumb)
                        if thumb is not None
                        else f"/plugin/{PLUGIN_ID}/ui/{IMAGE_DIR_NAME}/{day}/{stored}"
                    ),
                    "size": len(binary),
                    "original_name": str(filename or ""),
                },
            }
        )

    @plugin_entry(
        id="delete_image",
        name=tr("entries.deleteImage.name", default="删除章节图片"),
        description=tr(
            "entries.deleteImage.description",
            default="删除某一天章节图集里的一张图片（连同缩略图和图片记忆），删除后不可恢复。",
        ),
        input_schema={
            "type": "object",
            "properties": {
                "date": {"type": "string", "description": tr("fields.date", default="日期（YYYY-MM-DD）")},
                "filename": {
                    "type": "string",
                    "description": tr("fields.imageStoredName", default="图片在章节里的文件名"),
                },
            },
            "required": ["filename"],
        },
        llm_result_fields=["date", "filename", "count"],
    )
    async def entry_delete_image(self, date: str = "", filename: str = "", **_):
        if self.main_plugin.store is None:
            return Err(SdkError("插件尚未初始化完成"))
        day = self.main_plugin._normalize_day(date) or self.main_plugin._panel_day or local_day_key()
        path = self.main_plugin._image_file(day, filename)
        if path is None:
            return Err(SdkError("图片不存在，可能已经被删除了"))
        await asyncio.to_thread(self.main_plugin._delete_image_files, path)
        self.main_plugin.logger.info("章节图片已删除：{} / {}", day, path.name)
        return Ok(
            {
                "date": day,
                "filename": path.name,
                "count": self.main_plugin._image_count(day),
            }
        )

    @plugin_entry(
        id="save_image_memory",
        name=tr("entries.saveImageMemory.name", default="保存图片记忆"),
        description=tr(
            "entries.saveImageMemory.description",
            default="给某一天的某张图片写一段记忆文字；传空文本则清除。"
            "发送给猫娘时可以直接带上这段记忆。",
        ),
        input_schema={
            "type": "object",
            "properties": {
                "date": {"type": "string", "description": tr("fields.date", default="日期（YYYY-MM-DD）")},
                "filename": {
                    "type": "string",
                    "description": tr("fields.imageStoredName", default="图片在章节里的文件名"),
                },
                "text": {
                    "type": "string",
                    "description": tr("fields.imageMemoryText", default="图片记忆文字（留空则清除）"),
                },
            },
            "required": ["filename"],
        },
        llm_result_fields=["date", "filename", "saved"],
    )
    async def entry_save_image_memory(
        self, date: str = "", filename: str = "", text: str = "", **_
    ):
        if self.main_plugin.store is None:
            return Err(SdkError("插件尚未初始化完成"))
        day = self.main_plugin._normalize_day(date) or self.main_plugin._panel_day or local_day_key()
        path = self.main_plugin._image_file(day, filename)
        if path is None:
            return Err(SdkError("图片不存在，可能已经被删除了"))
        memory = str(text or "").strip()
        if len(memory) > IMAGE_MEMORY_MAX_CHARS:
            return Err(
                SdkError(f"图片记忆最多 {IMAGE_MEMORY_MAX_CHARS} 字，当前 {len(memory)} 字")
            )
        await asyncio.to_thread(self.main_plugin._write_image_memory, path, memory)
        return Ok(
            {
                "date": day,
                "filename": path.name,
                "memory": memory,
                "saved": bool(memory),
            }
        )

    @plugin_entry(
        id="send_image_to_catgirl",
        name=tr("entries.sendImageToCatgirl.name", default="把图片发送给猫娘"),
        description=tr(
            "entries.sendImageToCatgirl.description",
            default="把某一天的某张图片发到聊天里给猫娘看，并可选附带一段文字；"
            "text 留空时自动带上这张图片已保存的图片记忆。",
        ),
        input_schema={
            "type": "object",
            "properties": {
                "date": {"type": "string", "description": tr("fields.date", default="日期（YYYY-MM-DD）")},
                "filename": {
                    "type": "string",
                    "description": tr("fields.imageStoredName", default="图片在章节里的文件名"),
                },
                "text": {
                    "type": "string",
                    "description": tr(
                        "fields.imageSendText",
                        default="附带文字（留空则用图片记忆；都没有则只发图片）",
                    ),
                },
            },
            "required": ["filename"],
        },
        llm_result_fields=["date", "filename", "image_sent", "submitted"],
    )
    async def entry_send_image_to_catgirl(
        self, date: str = "", filename: str = "", text: str = "", **_
    ):
        if self.main_plugin.store is None:
            return Err(SdkError("插件尚未初始化完成"))
        day = self.main_plugin._normalize_day(date) or self.main_plugin._panel_day or local_day_key()
        path = self.main_plugin._image_file(day, filename)
        if path is None:
            return Err(SdkError("图片不存在，可能已经被删除了"))
        note = str(text or "").strip() or self.main_plugin._read_image_memory(path)
        note = note[:IMAGE_MEMORY_MAX_CHARS]
        image_part = await asyncio.to_thread(self.main_plugin._image_push_part, path)
        parts: list[dict] = []
        if image_part is not None:
            parts.append(image_part)
        if note:
            parts.append({"type": "text", "text": note})
        if not parts:
            return Err(SdkError("图片读取失败，没能发送出去"))

        def _push() -> Any:
            return self.main_plugin.push_message(
                source=PLUGIN_ID,
                visibility=["chat"],
                ai_behavior="respond",
                parts=parts,
                priority=5,
            )

        try:
            result = await asyncio.to_thread(_push)
        except Exception as exc:  # noqa: BLE001 - 推送失败要告诉界面
            self.main_plugin.logger.warning("发送图片给猫娘失败：{}", exc)
            return Err(SdkError(f"发送失败：{exc}"))
        if isinstance(result, dict) and not result.get("submitted", True):
            return Err(SdkError(f"发送失败：{result.get('reason') or '宿主未接收'}"))
        self.main_plugin.logger.info(
            "图片已发送给猫娘：{} / {}（图片：{}）",
            day,
            path.name,
            "有" if image_part is not None else "无",
        )
        return Ok(
            {
                "date": day,
                "filename": path.name,
                "text": note,
                "image_sent": image_part is not None,
                "submitted": True,
            }
        )
