"""One Telegram forum topic per external phone number."""
import asyncio
import logging
from typing import Awaitable, Callable, TypeVar

from telegram import Bot
from telegram.error import BadRequest, TelegramError

from .numbers import pretty
from .store import Contact, Store

log = logging.getLogger(__name__)
T = TypeVar("T")

TOPIC_NAME_MAX = 128


def _thread_gone(e: BadRequest) -> bool:
    text = str(e).lower()
    return "thread not found" in text or "topic_deleted" in text or "topic deleted" in text or "topic_id_invalid" in text


def topic_title(contact: Contact) -> str:
    title = f"{contact.name} · {pretty(contact.number)}" if contact.name else pretty(contact.number)
    return title[:TOPIC_NAME_MAX]


def topic_link(chat_id: int, thread_id: int) -> str:
    internal = str(chat_id).removeprefix("-100")
    return f"https://t.me/c/{internal}/{thread_id}"


class Topics:
    def __init__(self, bot: Bot, chat_id: int, store: Store):
        self.bot = bot
        self.chat_id = chat_id
        self.store = store
        self._lock = asyncio.Lock()

    async def ensure(self, number: str) -> tuple[Contact, bool]:
        """Return the contact with a live, open topic, creating one if needed. Second value: created now."""
        async with self._lock:
            contact = await self.store.contact(number)
            if contact and contact.thread_id:
                if not contact.closed:
                    return contact, False
                try:
                    await self.bot.reopen_forum_topic(self.chat_id, contact.thread_id)
                    await self.store.set_closed(number, False)
                    contact.closed = False
                    return contact, False
                except BadRequest as e:
                    if "not_modified" in str(e).lower():
                        await self.store.set_closed(number, False)
                        contact.closed = False
                        return contact, False
                    if not _thread_gone(e):
                        raise
                    log.info("topic for %s was deleted; recreating", number)
            contact = contact or Contact(number, None, None, False)
            topic = await self.bot.create_forum_topic(self.chat_id, topic_title(contact))
            await self.store.set_thread(number, topic.message_thread_id)
            contact.thread_id, contact.closed = topic.message_thread_id, False
            log.info("created topic %s for %s", topic.message_thread_id, number)
            return contact, True

    async def post(self, number: str, send: Callable[[int], Awaitable[T]]) -> T:
        """Run send(thread_id) in the number's topic, recreating the topic once if it was deleted."""
        contact, _ = await self.ensure(number)
        try:
            return await send(contact.thread_id)
        except BadRequest as e:
            if not _thread_gone(e):
                raise
            log.info("topic %s for %s is gone; recreating", contact.thread_id, number)
            await self.store.set_thread(number, None)
            contact, _ = await self.ensure(number)
            return await send(contact.thread_id)

    async def notify_general(self, text: str) -> None:
        try:
            await self.bot.send_message(self.chat_id, text)
        except TelegramError:
            log.exception("failed to post notice to General")

    async def rename(self, contact: Contact) -> None:
        if contact.thread_id:
            await self.bot.edit_forum_topic(self.chat_id, contact.thread_id, name=topic_title(contact))

    async def set_closed(self, contact: Contact, closed: bool) -> None:
        if contact.thread_id:
            try:
                if closed:
                    await self.bot.close_forum_topic(self.chat_id, contact.thread_id)
                else:
                    await self.bot.reopen_forum_topic(self.chat_id, contact.thread_id)
            except BadRequest as e:
                if "not_modified" not in str(e).lower():
                    raise
        await self.store.set_closed(contact.number, closed)
