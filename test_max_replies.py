import asyncio
from pathlib import Path
import tempfile
from types import SimpleNamespace as NS
import unittest
from unittest.mock import AsyncMock

from max_native_media import MaxRejected, NativeTransport, UploadFailed
from max_replies import MAX_DOWNLOAD_BYTES, ReplyController
from max_reply_store import ReplyStore
import max_notify_format as fmt


def update_for(message_id=200, prompt_id=100, *, owner=42, **kwargs):
    values = dict(message_id=message_id, reply_to_message=NS(message_id=prompt_id),
                  text="Ответ", caption=None, photo=[], document=None, voice=None,
                  video=None, video_note=None, audio=None, animation=None,
                  media_group_id=None, reply_text=AsyncMock())
    values.update(kwargs)
    return NS(effective_user=NS(id=owner), effective_chat=NS(id=owner, type="private"),
              message=NS(**values))


class ReplyFlowTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "routes.sqlite3"
        self.store = ReplyStore(self.path)
        self.addCleanup(lambda: self.store.close())
        self.token = self.store.add_route(-123, "Группа: Тест", "max-message-1")
        self.store.add_prompt(42, 100, self.token)
        self.transport = NS(deliver=AsyncMock(return_value=("confirmed-1", False)))
        self.controller = ReplyController(42, self.store, self.transport, lambda: True)

    async def test_button_binds_prompt_to_registered_route_only(self):
        query = NS(data="maxreply:" + self.token, answer=AsyncMock(),
                   message=NS(reply_text=AsyncMock(return_value=NS(message_id=101))))
        update = update_for()
        update.callback_query = query
        await self.controller.button(update, None)
        self.assertEqual(self.store.prompt(42, 101)["chat_id"], "-123")
        self.assertIn("Группа: Тест", query.message.reply_text.call_args.args[0])
        query.data = "maxreply:forged"
        query.message.reply_text.reset_mock()
        await self.controller.button(update, None)
        query.message.reply_text.assert_not_called()

    async def test_unrelated_message_never_goes_to_last_selected_recipient(self):
        update = update_for(reply_to_message=None)
        await self.controller.message(update, None)
        self.transport.deliver.assert_not_called()

    async def test_other_user_cannot_reply_or_activate_button(self):
        update = update_for(owner=43)
        await self.controller.message(update, None)
        self.transport.deliver.assert_not_called()
        update.callback_query = NS(answer=AsyncMock(), data="maxreply:" + self.token)
        await self.controller.button(update, None)
        self.assertTrue(update.callback_query.answer.call_args.kwargs["show_alert"])

    async def test_multiple_prompts_keep_their_original_recipients(self):
        second = self.store.add_route(456, "Другой контакт", "max-message-2")
        self.store.add_prompt(42, 101, second)
        await self.controller.message(update_for(prompt_id=100), None)
        await self.controller.message(update_for(message_id=201, prompt_id=101), None)
        self.assertEqual([call.args[0] for call in self.transport.deliver.call_args_list], ["-123", "456"])

    async def test_processed_message_is_not_sent_twice_even_after_restart(self):
        await self.controller.message(update_for(), None)
        self.store.close()
        self.store = ReplyStore(self.path)
        self.controller.store = self.store
        self.store.add_prompt(42, 100, self.token)
        await self.controller.message(update_for(), None)
        self.transport.deliver.assert_awaited_once()

    async def test_cancel_and_expired_prompt_do_not_send(self):
        await self.controller.cancel(update_for(), None)
        await self.controller.message(update_for(), None)
        self.transport.deliver.assert_not_called()
        self.store.add_prompt(42, 100, self.token)
        with self.store.db:
            self.store.db.execute("UPDATE prompts SET expires=0")
        await self.controller.message(update_for(), None)
        self.transport.deliver.assert_not_called()

    async def test_voice_is_downloaded_as_bytes_without_forwarding_token_url(self):
        tg_file = NS(file_size=4, download_as_bytearray=AsyncMock(return_value=bytearray(b"OggS")),
                     file_path="https://api.telegram.org/file/botSECRET/private.ogg")
        voice = NS(file_size=4, duration=3, get_file=AsyncMock(return_value=tg_file))
        update = update_for(text=None, voice=voice)
        await self.controller.message(update, None)
        call = self.transport.deliver.call_args
        self.assertEqual(call.args[0], "-123")
        self.assertEqual(call.kwargs, {"kind": "voice", "data": b"OggS", "filename": "voice.ogg", "duration": 3})
        self.assertNotIn("SECRET", str(call))

    async def test_large_attachment_and_album_are_rejected_before_download(self):
        photo = NS(file_size=MAX_DOWNLOAD_BYTES + 1, get_file=AsyncMock())
        update = update_for(text=None, photo=[photo])
        await self.controller.message(update, None)
        photo.get_file.assert_not_called()
        self.transport.deliver.assert_not_called()
        await self.controller.message(update_for(media_group_id="album"), None)
        self.transport.deliver.assert_not_called()

    async def test_timeout_does_not_retry_or_consume_another_prompt(self):
        self.transport.deliver.side_effect = asyncio.TimeoutError()
        update = update_for()
        await self.controller.message(update, None)
        self.transport.deliver.assert_awaited_once()
        self.assertIsNotNone(self.store.prompt(42, 100))
        self.assertIn("Проверь диалог", update.message.reply_text.call_args.args[0])

    async def test_file_fallback_is_explicit_in_confirmation(self):
        self.transport.deliver.return_value = ("confirmed", True)
        update = update_for()
        await self.controller.message(update, None)
        self.assertIn("как файл", update.message.reply_text.call_args.args[0])


class NativeMediaTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.client = NS(_handle=AsyncMock(), _ws=NS(closed=False), _seq=0, _pending={},
                         upload_file=AsyncMock(return_value={"_type": "FILE", "fileId": 77}))
        self.transport = NativeTransport(self.client)

    async def test_request_registers_future_before_fast_response(self):
        async def send(opcode, payload):
            seq = self.client._seq
            self.client._seq += 1
            future = self.client._pending[seq]
            future.set_result({"message": {"id": "ack"}})
            return seq
        self.client._send = send
        self.assertEqual(await self.transport.send_confirmed(7, "text", []), "ack")
        self.assertEqual(self.client._pending, {})

    async def test_media_without_caption_uses_null_text(self):
        async def send(opcode, payload):
            self.assertIsNone(payload["message"]["text"])
            seq = self.client._seq
            self.client._seq += 1
            self.client._pending[seq].set_result({"message": {"id": "ack"}})
            return seq
        self.client._send = send
        self.assertEqual(await self.transport.send_confirmed(7, "", [{"_type": "AUDIO"}]), "ack")

    async def test_explicit_protocol_error_is_preserved_without_private_payload(self):
        async def send(opcode, payload):
            seq = self.client._seq
            self.client._seq += 1
            await self.client._handle({"cmd": 3, "seq": seq,
                "payload": {"error": "attachment.not.ready", "token": "SECRET"}})
            return seq
        self.client._send = send
        with self.assertRaisesRegex(MaxRejected, "attachment.not.ready") as caught:
            await self.transport.request(64, {})
        self.assertNotIn("SECRET", str(caught.exception))

    async def test_photo_uses_native_photo_token(self):
        self.transport.request = AsyncMock(return_value={"url": "https://iu.oneme.ru/upload"})
        self.transport.http_upload = AsyncMock(return_value={"photos": {"12": {"token": "photo-token"}}})
        attach = await self.transport.upload_native("photo", b"jpeg", "photo.jpg")
        self.assertEqual(attach, {"_type": "PHOTO", "photoToken": "photo-token"})
        self.assertEqual(self.transport.request.call_args.args[0], 80)

    async def test_voice_uses_audio_attachment_with_duration_not_generic_file(self):
        self.transport.request = AsyncMock(return_value={"info": [{"url": "https://au.oneme.ru/upload", "videoId": 12, "token": "audio-token"}]})
        self.transport.http_upload = AsyncMock(return_value={})
        attach = await self.transport.upload_native("voice", b"OggS", "voice.ogg", 3)
        self.assertEqual(attach, {"_type": "AUDIO", "token": "audio-token", "duration": 3000,
                                  "wave": "\x00" * 80})
        self.assertEqual(self.transport.request.call_args.args[1]["type"], 2)
        self.assertEqual(self.transport.request.call_args.args[1]["uploaderType"], 1)

    async def test_video_waits_for_processing_then_builds_video_attachment(self):
        self.transport.request = AsyncMock(return_value={"info": [{"url": "https://vu.okcdn.ru/upload", "videoId": 13, "token": "video-token"}]})
        async def upload(*args, **kwargs):
            await self.client._handle({"opcode": 136, "payload": {"videoId": 13}})
            return {}
        self.transport.http_upload = upload
        attach = await self.transport.upload_native("video", b"mp4", "video.mp4")
        self.assertEqual(attach["_type"], "VIDEO")
        self.assertEqual(attach["videoId"], 13)

    async def test_uncertain_send_timeout_never_falls_back_or_sends_again(self):
        self.transport.upload_native = AsyncMock(return_value={"_type": "PHOTO", "photoToken": "p"})
        self.transport.send_confirmed = AsyncMock(side_effect=asyncio.TimeoutError())
        with self.assertRaises(asyncio.TimeoutError):
            await self.transport.deliver(7, "caption", kind="photo", data=b"jpeg")
        self.transport.send_confirmed.assert_awaited_once()
        self.client.upload_file.assert_not_called()

    async def test_uncertain_retry_timeout_also_never_falls_back(self):
        self.transport.upload_native = AsyncMock(return_value={"_type": "AUDIO", "token": "a"})
        event = asyncio.Event()
        event.set()
        self.transport.ready["1"] = event
        self.transport.send_confirmed = AsyncMock(side_effect=[MaxRejected("attachment.not.ready"), asyncio.TimeoutError()])
        with self.assertRaises(asyncio.TimeoutError):
            await self.transport.deliver(7, "", kind="voice", data=b"ogg")
        self.assertEqual(self.transport.send_confirmed.await_count, 2)
        self.client.upload_file.assert_not_called()

    async def test_failed_native_upload_can_safely_fall_back_before_any_send(self):
        self.transport.upload_native = AsyncMock(side_effect=UploadFailed())
        self.transport.send_confirmed = AsyncMock(return_value="ack")
        result = await self.transport.deliver(7, "caption", kind="photo", data=b"jpeg")
        self.assertEqual(result, ("ack", True))
        self.client.upload_file.assert_awaited_once()
        self.transport.send_confirmed.assert_awaited_once()

    def test_upload_destination_must_be_a_trusted_https_max_host(self):
        for url in ("https://iu.oneme.ru/upload", "https://vu.okcdn.ru/upload"):
            self.transport.validate_url(url)
        for url in ("http://iu.oneme.ru/upload", "https://oneme.ru.evil.test/upload", "https://localhost/upload", "https://u:p@iu.oneme.ru/upload"):
            with self.assertRaises(UploadFailed):
                self.transport.validate_url(url)


class NotificationButtonTests(unittest.TestCase):
    def tearDown(self):
        fmt.configure_replies(None)

    def test_button_carries_opaque_token_and_correct_group_route(self):
        calls = []
        fmt.configure_replies(lambda *args: calls.append(args) or "opaque-token")
        msg = NS(chat_id=-123, message_id="message-1")
        keyboard = fmt.notification_keyboard(msg, "Анна &amp; Иван", "ИТ &lt;чат&gt;", False)
        self.assertEqual(keyboard.inline_keyboard[0][0].callback_data, "maxreply:opaque-token")
        self.assertEqual(calls, [(-123, "Группа: ИТ <чат>", "message-1")])
