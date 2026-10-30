"""Deliver received SMS/MMS into the sender's Telegram topic. Shared by webhook and reconciler."""
import asyncio
import logging
import mimetypes

from telegram import Bot, InputMediaPhoto

from .numbers import normalize_lenient
from .store import Store
from .topics import Topics
from .voipms import Message, VoipMsClient

log = logging.getLogger(__name__)

CAPTION_MAX = 1024


class Inbound:
    def __init__(self, bot: Bot, chat_id: int, store: Store, topics: Topics, voipms: VoipMsClient):
        self.bot = bot
        self.chat_id = chat_id
        self.store = store
        self.topics = topics
        self.voipms = voipms
        self._lock = asyncio.Lock()

    async def deliver(self, msg: Message) -> bool:
        """Post msg to Telegram unless already delivered. Returns True if it was posted now."""
        async with self._lock:
            if await self.store.seen(msg.id):
                return False
            number = normalize_lenient(msg.number)
            files, failed = await self._download(msg.media)
            text = msg.text
            if failed:
                text = "\n".join([text, *failed]) if text else "\n".join(failed)
            await self.topics.post(number, lambda thread_id: self._post(thread_id, text, files))
            await self.store.log_in(msg.id, number, msg.text, len(msg.media))
            log.info("delivered inbound %s from %s (%d media)", msg.id, number, len(msg.media))
            return True

    async def _download(self, urls: list[str]) -> tuple[list[tuple[bytes, str, str]], list[str]]:
        files, failed = [], []
        for url in urls:
            try:
                blob, mime = await self.voipms.download(url)
                ext = mimetypes.guess_extension(mime) or ""
                files.append((blob, mime, f"mms{len(files) + 1}{ext}"))
            except Exception:
                log.exception("failed to download MMS media %s", url)
                failed.append(url)
        return files, failed

    async def _post(self, thread_id: int, text: str, files: list[tuple[bytes, str, str]]) -> None:
        send = dict(chat_id=self.chat_id, message_thread_id=thread_id)
        if not files:
            await self.bot.send_message(text=text or "(empty message)", **send)
            return

        caption = text or None
        if caption and len(caption) > CAPTION_MAX:
            await self.bot.send_message(text=caption, **send)
            caption = None

        photos = [f for f in files if f[1].startswith("image/") and f[1] != "image/gif"]
        others = [f for f in files if f not in photos]
        if len(photos) == 1:
            await self.bot.send_photo(photo=photos[0][0], caption=caption, **send)
            caption = None
        elif photos:
            group = [InputMediaPhoto(blob, caption=caption if i == 0 else None) for i, (blob, _, _) in enumerate(photos)]
            await self.bot.send_media_group(media=group, **send)
            caption = None
        for blob, _, filename in others:
            await self.bot.send_document(document=blob, filename=filename, caption=caption, **send)
            caption = None
