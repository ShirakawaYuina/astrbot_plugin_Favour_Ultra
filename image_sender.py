from __future__ import annotations

from typing import Any

from astrbot.core import astrbot_config, file_token_service

SEND_MODE_BASE64 = "base64"
SEND_MODE_URL = "url"


class FavourImageSender:
    """Send rendered favour images with either AstrBot image results or OneBot URLs."""

    def __init__(
        self,
        send_mode: str = SEND_MODE_BASE64,
        url_base: str = "",
        file_token_service: Any = file_token_service,
    ) -> None:
        self.send_mode = self._normalize_send_mode(send_mode)
        self.url_base = str(url_base or "").strip()
        self.file_token_service = file_token_service

    async def send_image(self, event: Any, image_path: str) -> None:
        """Send one rendered image according to the configured mode."""

        if self.send_mode == SEND_MODE_URL:
            await self._send_image_by_url(event, image_path)
            return

        await event.send(event.image_result(image_path))

    async def _send_image_by_url(self, event: Any, image_path: str) -> None:
        """Register a local image URL and send it directly through OneBot v11."""

        platform_name = str(event.get_platform_name() or "").strip()
        if platform_name != "aiocqhttp":
            raise RuntimeError("仅支持通过 OneBot v11 返回 QQ 图片消息")

        image_message = {
            "type": "image",
            "data": {
                "file": await self._register_image_url(image_path),
            },
        }

        bot = getattr(event, "bot", None)
        if bot is None:
            raise RuntimeError("URL 发送模式需要 aiocqhttp 事件暴露 bot 实例")

        group_id = str(event.get_group_id() or "").strip()
        if group_id.isdigit():
            await bot.send_group_msg(group_id=int(group_id), message=[image_message])
            return

        sender_id = str(event.get_sender_id() or "").strip()
        if sender_id.isdigit():
            await bot.send_private_msg(user_id=int(sender_id), message=[image_message])
            return

        raise RuntimeError("URL 发送模式缺少有效的群号或用户 ID")

    async def _register_image_url(self, image_path: str) -> str:
        """Register the rendered image in AstrBot file service."""

        callback_base = self._resolve_url_base()
        token = await self.file_token_service.register_file(str(image_path))
        return f"{callback_base}/api/file/{token}"

    def _resolve_url_base(self) -> str:
        """Use plugin URL base first, then fall back to AstrBot callback API base."""

        configured_base = (
            self.url_base
            or str(astrbot_config.get("callback_api_base", "") or "").strip()
        )
        clean_base = configured_base.rstrip("/")
        if not clean_base:
            raise RuntimeError(
                "URL 发送模式需要配置 image_send_url_base 或 AstrBot callback_api_base"
            )
        return clean_base

    @staticmethod
    def _normalize_send_mode(send_mode: str) -> str:
        """Normalize unknown send modes to base64 for backward compatibility."""

        clean_mode = str(send_mode or "").strip().lower()
        if clean_mode == SEND_MODE_URL:
            return SEND_MODE_URL
        return SEND_MODE_BASE64
