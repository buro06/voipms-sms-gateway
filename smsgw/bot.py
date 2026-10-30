"""Telegram side: access control, commands, and topic messages -> SMS/MMS."""
import asyncio
import html
import logging

from telegram import Message, Update
from telegram.constants import ChatType, ParseMode
from telegram.error import TelegramError
from telegram.ext import (
    Application,
    ApplicationHandlerStop,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    TypeHandler,
    filters,
)

from .config import Settings
from .numbers import InvalidNumber, normalize, pretty
from .outbound import Outbound, QuotaExceeded
from .store import Store
from .topics import Topics, topic_link, topic_title
from .voipms import VoipMsError

log = logging.getLogger(__name__)

ALBUM_WAIT = 2.0  # seconds to collect the photos of one Telegram album
OK_REACTION = "👍"
FAIL_REACTION = "👎"

HELP = """<b>SMS gateway</b>
Each phone number has its own topic. Anything posted in a number's topic is texted to that number; replies show up there too.

/new <i>number</i> [name] – start or open a conversation
/name <i>name</i> – (in a topic) set the contact name
/close, /reopen – (in a topic) archive or reopen it
/contacts – list conversations
/quota – sends used today

Text up to 160 chars goes as SMS; longer text and photos (up to 3 per message) go as MMS."""


class Gateway:
    def __init__(self, settings: Settings, store: Store, topics: Topics, outbound: Outbound):
        self.settings = settings
        self.store = store
        self.topics = topics
        self.outbound = outbound
        self._albums: dict[str, list[Message]] = {}

    def register(self, app: Application) -> None:
        app.add_error_handler(self._error)
        app.add_handler(TypeHandler(Update, self._gate), group=-1)
        app.add_handler(CommandHandler(["start", "help"], self._help))
        app.add_handler(CommandHandler("new", self._new))
        app.add_handler(CommandHandler("name", self._name))
        app.add_handler(CommandHandler("close", self._close))
        app.add_handler(CommandHandler("reopen", self._reopen))
        app.add_handler(CommandHandler("contacts", self._contacts))
        app.add_handler(CommandHandler("quota", self._quota))
        app.add_handler(
            MessageHandler(
                filters.ChatType.SUPERGROUP & filters.IS_TOPIC_MESSAGE & ~filters.COMMAND & ~filters.StatusUpdate.ALL,
                self._topic_message,
            )
        )

    # --- access control -----------------------------------------------------------

    async def _gate(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        chat, user, msg = update.effective_chat, update.effective_user, update.effective_message
        log.info("update %s from chat %s user %s", update.update_id, chat and chat.id, user and user.id)
        if self.settings.telegram_chat_id == 0:
            log.info("discovery mode: update from chat %s (%s), user %s", chat and chat.id, chat and chat.type, user and user.id)
            if msg and chat:
                await msg.reply_text(f"chat id: {chat.id}\nyour user id: {user.id if user else '?'}")
            raise ApplicationHandlerStop
        if chat is None or chat.id != self.settings.telegram_chat_id:
            log.warning("ignoring update from foreign chat %s (%s)", chat and chat.id, chat and chat.type)
            if chat and chat.type in (ChatType.GROUP, ChatType.SUPERGROUP, ChatType.CHANNEL):
                try:
                    await ctx.bot.leave_chat(chat.id)
                except TelegramError:
                    pass
            elif chat and chat.type == ChatType.PRIVATE and msg and user:
                await msg.reply_text(f"This is a private SMS gateway. Your Telegram user id is {user.id}.")
            raise ApplicationHandlerStop
        allowed = self.settings.allowed_users
        if allowed and (user is None or user.id not in allowed):
            log.warning("ignoring message from user %s: not in ALLOWED_USER_IDS", user and user.id)
            if msg and user and not user.is_bot:
                await msg.reply_text(f"Not sent: your user id {user.id} is not in ALLOWED_USER_IDS.")
            raise ApplicationHandlerStop

    async def _error(self, update: object, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        log.error("error handling update %s", getattr(update, "update_id", update), exc_info=ctx.error)

    # --- commands -----------------------------------------------------------------

    async def _help(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        await update.effective_message.reply_text(HELP, parse_mode=ParseMode.HTML)

    async def _contact_here(self, msg: Message):
        if not msg.is_topic_message or msg.message_thread_id is None:
            await msg.reply_text("Use this inside a conversation topic.")
            return None
        contact = await self.store.contact_by_thread(msg.message_thread_id)
        if contact is None:
            await msg.reply_text("This topic isn't linked to a phone number.")
        return contact

    async def _new(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        msg = update.effective_message
        if not ctx.args:
            await msg.reply_text("Usage: /new 5551234567 [name]")
            return
        try:
            number = normalize(ctx.args[0])
        except InvalidNumber as e:
            await msg.reply_text(str(e))
            return
        name = " ".join(ctx.args[1:]).strip() or None
        if name:
            await self.store.set_name(number, name)
        contact, created = await self.topics.ensure(number)
        if name and not created:
            contact.name = name
            await self.topics.rename(contact)
        if created:
            await self.topics.bot.send_message(
                self.settings.telegram_chat_id,
                f"📱 Conversation with {topic_title(contact)}. Messages posted here are texted to this number.",
                message_thread_id=contact.thread_id,
            )
        link = topic_link(self.settings.telegram_chat_id, contact.thread_id)
        await msg.reply_text(
            f'{"Created" if created else "Opened"} <a href="{link}">{html.escape(topic_title(contact))}</a>',
            parse_mode=ParseMode.HTML,
        )

    async def _name(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        msg = update.effective_message
        contact = await self._contact_here(msg)
        if contact is None:
            return
        contact.name = " ".join(ctx.args).strip() or None
        await self.store.set_name(contact.number, contact.name)
        await self.topics.rename(contact)
        await msg.reply_text(f"Renamed to {topic_title(contact)}")

    async def _close(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        contact = await self._contact_here(update.effective_message)
        if contact:
            await update.effective_message.reply_text("Closed. A new text from this number will reopen it.")
            await self.topics.set_closed(contact, True)

    async def _reopen(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        contact = await self._contact_here(update.effective_message)
        if contact:
            await self.topics.set_closed(contact, False)
            await update.effective_message.reply_text("Reopened.")

    async def _contacts(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        contacts = [c for c in await self.store.contacts() if c.thread_id]
        if not contacts:
            await update.effective_message.reply_text("No conversations yet. Start one with /new 5551234567")
            return
        lines = [
            f'{"🗄" if c.closed else "💬"} <a href="{topic_link(self.settings.telegram_chat_id, c.thread_id)}">'
            f"{html.escape(topic_title(c))}</a>"
            for c in contacts
        ]
        await update.effective_message.reply_text("\n".join(lines), parse_mode=ParseMode.HTML)

    async def _quota(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        count, exhausted = await self.store.sends_today()
        suffix = " (voip.ms reported the limit reached)" if exhausted else ""
        await update.effective_message.reply_text(f"Sends today: {count}/{self.settings.daily_send_limit}{suffix}")

    # --- topic messages -> SMS/MMS ------------------------------------------------

    async def _topic_message(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        msg = update.effective_message
        log.info("topic message %s in thread %s from user %s", msg.message_id, msg.message_thread_id,
                 msg.from_user and msg.from_user.id)
        if msg.media_group_id:
            album = self._albums.setdefault(msg.media_group_id, [])
            album.append(msg)
            if len(album) == 1:
                ctx.application.create_task(self._flush_album(msg.media_group_id))
            return
        await self._send([msg])

    async def _flush_album(self, group_id: str) -> None:
        await asyncio.sleep(ALBUM_WAIT)
        msgs = self._albums.pop(group_id, [])
        if msgs:
            await self._send(sorted(msgs, key=lambda m: m.message_id))

    async def _send(self, msgs: list[Message]) -> None:
        first = msgs[0]
        contact = await self.store.contact_by_thread(first.message_thread_id)
        if contact is None:
            await first.reply_text("This topic isn't linked to a phone number. Use /new <number> in General.")
            return

        text = "\n".join(t for m in msgs if (t := m.text or m.caption))
        attachments = []
        unsupported = False
        for m in msgs:
            if m.photo:
                attachments.append(m.photo[-1])
            elif m.document and (m.document.mime_type or "").startswith("image/"):
                attachments.append(m.document)
            elif m.effective_attachment is not None:
                unsupported = True

        if unsupported and not attachments and not text:
            await first.reply_text("Only text and photos can be sent as SMS/MMS.")
            await self._react(msgs, FAIL_REACTION)
            return
        try:
            images = [bytes(await (await a.get_file()).download_as_bytearray()) for a in attachments]
            await self.outbound.send(contact.number, text, images, first.from_user and first.from_user.id)
        except QuotaExceeded as e:
            await self._react(msgs, FAIL_REACTION)
            await first.reply_text(f"⛔ Not sent – daily send limit: {e}")
            return
        except VoipMsError as e:
            await self._react(msgs, FAIL_REACTION)
            await first.reply_text(f"❌ Not sent – voip.ms error: {e.status}")
            return
        except Exception:
            log.exception("send to %s failed", contact.number)
            await self._react(msgs, FAIL_REACTION)
            await first.reply_text("❌ Not sent – gateway error (see logs).")
            return
        if not await self._react(msgs, OK_REACTION):
            await first.reply_text("✅ Sent")
        if unsupported:
            await first.reply_text("Sent, but some attachments were skipped (only photos are supported).")

    async def _react(self, msgs: list[Message], emoji: str) -> bool:
        """React to msgs; False if Telegram refused (e.g. the group restricts reactions)."""
        for m in msgs:
            try:
                await m.set_reaction(emoji)
            except TelegramError as e:
                log.warning("could not react to message %s: %s", m.message_id, e)
                return False
        return True
