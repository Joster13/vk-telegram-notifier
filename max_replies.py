"""Owner-only Telegram reply handlers with explicit, persistent recipient binding."""
import logging

from telegram import ForceReply

LOG = logging.getLogger("dragon-replies")
MAX_DOWNLOAD_BYTES = 20 * 1024 * 1024


def media_from(message):
    if message.photo:
        return message.photo[-1], "photo", "photo.jpg"
    for field, kind, default in (("voice", "voice", "voice.ogg"),
                                  ("video", "video", "video.mp4"),
                                  ("video_note", "video", "video.mp4"),
                                  ("audio", "file", "audio.m4a"),
                                  ("animation", "video", "animation.mp4"),
                                  ("document", "file", "file")):
        obj = getattr(message, field, None)
        if obj:
            return obj, kind, getattr(obj, "file_name", None) or default
    return None


class ReplyController:
    def __init__(self, owner, store, transport, connected):
        self.owner = int(owner)
        self.store = store
        self.transport = transport
        self.connected = connected

    def authorized(self, update):
        return (update.effective_user is not None and update.effective_chat is not None
                and update.effective_user.id == self.owner
                and update.effective_chat.id == self.owner
                and update.effective_chat.type == "private")

    async def button(self, update, context):
        query = update.callback_query
        if not self.authorized(update):
            await query.answer("Нет доступа", show_alert=True)
            return
        data = query.data or ""
        route = self.store.route(data.removeprefix("maxreply:")) if data.startswith("maxreply:") else None
        if not route:
            await query.answer("Кнопка устарела. Используй новое уведомление MAX.", show_alert=True)
            return
        if not self.connected():
            await query.answer("MAX переподключается. Попробуй позже.", show_alert=True)
            return
        await query.answer()
        prompt = await query.message.reply_text(
            "Ответ в MAX → " + route["label"] + "\n\n"
            "Ответь на это сообщение текстом, ГС, фото или видео.\n"
            "Один ответ — одна отправка. /cancel — отменить.",
            reply_markup=ForceReply(selective=True, input_field_placeholder="Ответ для " + route["label"][:40]))
        self.store.add_prompt(self.owner, prompt.message_id, route["token"])

    async def cancel(self, update, context):
        if self.authorized(update):
            self.store.cancel(self.owner)
            await update.message.reply_text("Ответы в MAX отменены.")

    async def message(self, update, context):
        if not self.authorized(update):
            return
        message = update.message
        reply = message.reply_to_message
        if not reply:
            await message.reply_text("Для ответа нажми «Ответить в MAX» под нужным уведомлением.")
            return
        route = self.store.prompt(self.owner, reply.message_id)
        if not route:
            await message.reply_text("Этот запрос ответа уже завершён или устарел. Нажми кнопку под уведомлением ещё раз.")
            return
        if not self.connected():
            await message.reply_text("MAX переподключается. Ответ не отправлен. Попробуй позже.")
            return
        media = media_from(message)
        if not message.text and media is None:
            await message.reply_text("Поддерживаются текст, фото, видео, ГС и документы.")
            return
        if getattr(message, "media_group_id", None):
            await message.reply_text("Пришли фото или видео по одному, ответом на подсказку бота.")
            return
        if media and getattr(media[0], "file_size", 0) and media[0].file_size > MAX_DOWNLOAD_BYTES:
            await message.reply_text("Файл больше 20 МБ. Пришли меньший файл.")
            return
        if not self.store.claim(self.owner, message.message_id):
            await message.reply_text("Этот ответ уже обработан. Повторная отправка остановлена.")
            return
        try:
            kwargs = {}
            text = message.text or message.caption or ""
            if media:
                obj, kind, filename = media
                tg_file = await obj.get_file()
                if getattr(tg_file, "file_size", 0) and tg_file.file_size > MAX_DOWNLOAD_BYTES:
                    raise ValueError("File is too large")
                data = bytes(await tg_file.download_as_bytearray())
                if len(data) > MAX_DOWNLOAD_BYTES:
                    raise ValueError("File is too large")
                duration = getattr(obj, "duration", 0) or 0
                if hasattr(duration, "total_seconds"):
                    duration = duration.total_seconds()
                kwargs = {"kind": kind, "data": data, "filename": filename, "duration": duration}
            _, degraded = await self.transport.deliver(route["chat_id"], text, **kwargs)
        except Exception as exc:
            self.store.finish(self.owner, message.message_id, "unconfirmed")
            LOG.warning("MAX reply not confirmed (%s)", type(exc).__name__)
            await message.reply_text("Не удалось подтвердить отправку в MAX. Проверь диалог перед повтором.\n"
                                     "Для повтора отправь новое сообщение ответом на ту же подсказку.")
            return
        self.store.finish(self.owner, message.message_id, "sent")
        self.store.consume_prompt(self.owner, reply.message_id)
        suffix = "\nMAX не принял медиа в обычном виде: вложение доставлено как файл." if degraded else ""
        await message.reply_text("Отправлено в MAX → " + route["label"] + suffix)
