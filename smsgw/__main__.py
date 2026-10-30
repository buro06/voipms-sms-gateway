import asyncio
import logging
import signal

from aiohttp import web
from telegram import Update
from telegram.error import TelegramError
from telegram.ext import AIORateLimiter, Application

from .bot import Gateway, publish_commands
from .config import Settings
from .inbound import Inbound
from .outbound import Outbound
from .reconcile import Reconciler
from .store import Store
from .topics import Topics, check_chat
from .voipms import VoipMsClient
from .webhook import make_app

log = logging.getLogger("smsgw")


async def main() -> int:
    settings = Settings()
    logging.basicConfig(level=settings.log_level, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    # httpx logs full request URLs, which contain the voip.ms API password and the bot token.
    logging.getLogger("httpx").setLevel(logging.WARNING)

    store = await Store.open(settings.db_path)
    voipms = VoipMsClient(settings.voipms_api_username, settings.voipms_api_password, settings.voipms_api_url)
    app = Application.builder().token(settings.telegram_bot_token).rate_limiter(AIORateLimiter()).build()

    chat_id = settings.telegram_chat_id
    topics = Topics(app.bot, chat_id, store)
    inbound = Inbound(app.bot, chat_id, store, topics, voipms)
    outbound = Outbound(settings, store, voipms, notify=topics.notify_general)
    Gateway(settings, store, topics, outbound).register(app)

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)

    # access_log=None: callback URLs carry the webhook secret and message text.
    runner = web.AppRunner(make_app(settings, voipms, inbound), access_log=None)
    await runner.setup()
    reconcile_task = None
    problems: list[str] = []
    async with app:
        if chat_id != 0 and (problems := await check_chat(app.bot, chat_id)):
            for problem in problems:
                log.error(problem)
            log.error("Fix the above in Telegram and restart the gateway.")
            stop.set()
        else:
            await app.updater.start_polling(allowed_updates=Update.ALL_TYPES)
            log.info("polling Telegram as @%s", app.bot.username)
            if chat_id != 0:
                try:
                    await publish_commands(app.bot, chat_id)
                except TelegramError:
                    log.exception("could not publish the command menu")
            if chat_id == 0:
                log.warning("TELEGRAM_CHAT_ID not set: discovery mode. Message the bot in your group to get its id.")
            else:
                await web.TCPSite(runner, settings.webhook_host, settings.webhook_port).start()
                log.info("webhook listening on %s:%d", settings.webhook_host, settings.webhook_port)
                reconciler = Reconciler(voipms, store, inbound, settings.voipms_did, settings.reconcile_interval)
                reconcile_task = asyncio.create_task(reconciler.run(stop))
            log.info("gateway running")
            await stop.wait()
            log.info("shutting down")
            await app.updater.stop()
            await app.stop()
    if reconcile_task:
        await reconcile_task
    await runner.cleanup()
    await voipms.aclose()
    await store.close()
    return 1 if problems else 0


def run() -> None:
    raise SystemExit(asyncio.run(main()))


if __name__ == "__main__":
    run()
