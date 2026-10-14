"""One Telegram updates consumer for Gmail commands and MAX replies."""
import asyncio
import json
import logging
import os
from pathlib import Path
import signal
import sys
import threading

from telegram.ext import Application, CallbackQueryHandler, CommandHandler, MessageHandler, filters

import gmail_bot
from max_native_media import NativeTransport
from max_replies import ReplyController
from max_reply_store import ReplyStore


async def main():
    os.umask(0o077)
    # Upstream INFO/WARNING messages can contain private previews or upload URLs.
    logging.basicConfig(level=logging.CRITICAL)
    logging.getLogger("dragon-replies").setLevel(logging.WARNING)
    logging.getLogger("vk-notify").setLevel(logging.WARNING)
    log = logging.getLogger("dragon-bridge")
    log.setLevel(logging.INFO)
    sys.path.insert(0, "/opt/max2tg")
    from app import dragon_notify_format, max_listener
    from app.tg_sender import TelegramSender

    store = ReplyStore(Path(os.getenv("DATA_DIR", "/app/data")) / "max_reply_state.sqlite3")
    dragon_notify_format.configure_replies(store.add_route)
    sender = TelegramSender(gmail_bot.TG_TOKEN, gmail_bot.CHAT_ID)
    client = max_listener.create_max_client(os.environ["MAX_TOKEN"], os.environ["MAX_DEVICE_ID"],
                                           sender, reply_enabled=False)
    transport = NativeTransport(client)
    state = {"connected": False}
    health_path = Path("/tmp/dragon-notify-health.json")

    def write_health():
        temp = health_path.with_suffix(".tmp")
        temp.write_text(json.dumps({"max_connected": state["connected"],
                                   "telegram_consumers": 1, "replies_enabled": True,
                                   "native_media": ["PHOTO", "VIDEO", "AUDIO"]}))
        temp.replace(health_path)

    write_health()
    original_ready, original_disconnect = client._on_ready_cb, client._on_disconnect_cb

    async def ready(snapshot):
        state["connected"] = True
        write_health()
        log.info("MAX connected; replies enabled")
        await original_ready(snapshot)

    async def disconnected():
        state["connected"] = False
        write_health()
        log.warning("MAX disconnected; waiting for reconnect")
        await original_disconnect()

    client._on_ready_cb, client._on_disconnect_cb = ready, disconnected
    controller = ReplyController(gmail_bot.CHAT_ID, store, transport, lambda: state["connected"])
    owner_filter = filters.Chat(chat_id=int(gmail_bot.CHAT_ID)) & filters.User(user_id=int(gmail_bot.CHAT_ID)) & filters.ChatType.PRIVATE
    application = Application.builder().token(gmail_bot.TG_TOKEN).concurrent_updates(False).build()

    async def commands(update, context):
        cmd = (update.message.text or "").split()[0].split("@")[0].lower()
        if cmd == "/status":
            info = gmail_bot.STATE
            await update.message.reply_text(
                "MAX: " + ("подключён" if state["connected"] else "переподключается")
                + "\nGmail: " + ("ошибка " + info["error"] if info["error"] else "проверяется")
                + "\nПоследняя проверка Gmail: " + info["check"])
        elif cmd == "/test":
            await update.message.reply_text("Telegram работает. /status — Gmail и MAX.")
        else:
            await update.message.reply_text(
                "Dragon Notify | Уведомления\n\nУведомления ВК и MAX приходят сюда.\n"
                "Для ответа в MAX нажми кнопку под уведомлением, затем ответь на подсказку "
                "текстом, ГС, фото или видео. Файлы — до 20 МБ, по одному.\n"
                "/cancel — отменить ответы\n/status — подключения\n/test — тест")

    async def error_handler(update, context):
        logging.getLogger("dragon-replies").warning("Telegram handler error (%s)", type(context.error).__name__)

    application.add_handler(CallbackQueryHandler(controller.button, pattern=r"^maxreply:"))
    application.add_handler(CommandHandler("cancel", controller.cancel, filters=owner_filter))
    application.add_handler(CommandHandler(["start", "help", "status", "test"], commands, filters=owner_filter))
    application.add_handler(MessageHandler(owner_filter & ~filters.COMMAND, controller.message))
    application.add_error_handler(error_handler)
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)
    task = None
    try:
        async with application:
            await sender.start()
            await application.start()
            # Keep pending updates: the persistent attempts table blocks re-sends.
            await application.updater.start_polling(allowed_updates=["message", "callback_query"], drop_pending_updates=False)
            log.info("Bridge started with one Telegram updates consumer")
            threading.Thread(target=gmail_bot.worker, daemon=True).start()
            task = asyncio.create_task(client.run())
            stop_task = asyncio.create_task(stop.wait())
            await sender.send("Dragon Notify запущен. Gmail и MAX включены. Ответы доступны по кнопке под новыми уведомлениями MAX.")
            done, _ = await asyncio.wait([task, stop_task], return_when=asyncio.FIRST_COMPLETED)
            if task in done:
                task.result()
            stop_task.cancel()
            await application.updater.stop()
            await application.stop()
    finally:
        if task:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        await sender.stop()
        store.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except Exception as exc:
        print("Dragon bridge stopped:", type(exc).__name__, flush=True)
        raise SystemExit(1)
