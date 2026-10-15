"""Native MAX media upload and confirmed sends over the existing WebSocket.

Protocol shapes checked against MaxApiTeam/PyMax upload service, commit
53103f0c8df32f36110cbe0bcbfc3ad82173808a (src/pymax/api/uploads/service.py).
No second session, third-party upload service, or Telegram file URL is used.
"""
import asyncio
import json
import time
from urllib.parse import quote, urlsplit

import aiohttp


class MaxRejected(RuntimeError):
    pass


class UploadFailed(RuntimeError):
    pass


class NativeTransport:
    def __init__(self, client):
        self.client = client
        self.requests = set()
        self.ready = {}
        original = client._handle

        async def handle(packet):
            seq = packet.get("seq")
            payload = packet.get("payload") or {}
            if packet.get("cmd") == 3 and seq in self.requests:
                future = client._pending.get(seq)
                if future is not None and not future.done():
                    code = str(payload.get("error") or payload.get("code") or "rejected")[:120]
                    future.set_exception(MaxRejected(code))
                return
            if packet.get("opcode") == 136:
                for key in ("videoId", "audioId"):
                    event = self.ready.get(str(payload.get(key)))
                    if event is not None:
                        event.set()
            await original(packet)

        client._handle = handle

    async def request(self, opcode, payload, timeout=30):
        if not self.client._ws or self.client._ws.closed:
            raise ConnectionError("MAX is disconnected")
        loop = asyncio.get_running_loop()
        seq = self.client._seq
        future = loop.create_future()
        # Register before sending so even a very fast reply cannot be lost.
        self.client._pending[seq] = future
        self.requests.add(seq)
        try:
            if await self.client._send(opcode, payload) != seq:
                raise ConnectionError("MAX request was not sent")
            response = await asyncio.wait_for(future, timeout)
            if not response:
                raise MaxRejected("empty response")
            return response
        finally:
            self.requests.discard(seq)
            self.client._pending.pop(seq, None)

    @staticmethod
    def validate_url(url):
        parts = urlsplit(url)
        host = (parts.hostname or "").lower()
        if (parts.scheme != "https" or parts.username or parts.password
                or not any(host == d or host.endswith("." + d)
                           for d in ("oneme.ru", "okcdn.ru", "max.ru"))):
            raise UploadFailed("Unexpected MAX upload destination")

    async def http_upload(self, url, data, filename, *, photo=False):
        self.validate_url(url)
        session = self.client._session
        if session is None or session.closed:
            raise UploadFailed("MAX upload session is unavailable")
        headers = {}
        if photo:
            body = aiohttp.FormData()
            body.add_field("file", data, filename="photo.jpg", content_type="image/jpeg")
        else:
            body = data
            headers = {"Content-Disposition": "attachment; filename=" + quote(filename, safe=""),
                       "Content-Range": f"bytes 0-{len(data)-1}/{len(data)}",
                       "Content-Length": str(len(data)), "Content-Type": "application/octet-stream"}
        async with session.post(url, data=body, headers=headers, allow_redirects=False,
                                timeout=aiohttp.ClientTimeout(total=300)) as response:
            raw = await response.read()
            if response.status != 200:
                raise UploadFailed("MAX upload failed")
            if photo:
                try:
                    return json.loads(raw)
                except (ValueError, UnicodeError) as exc:
                    raise UploadFailed("Invalid MAX photo upload response") from exc
        return {}

    async def upload_native(self, kind, data, filename, duration=0):
        payload = {"count": 1, "type": 2 if kind == "voice" else 0,
                   "uploaderType": 1 if kind == "voice" else 0, "profile": False}
        response = await self.request(80 if kind == "photo" else 82, payload)
        if kind == "photo":
            url = response.get("url", "")
            result = await self.http_upload(url, data, filename, photo=True)
            photos = result.get("photos") or {}
            if len(photos) != 1:
                raise UploadFailed("Missing MAX photo token")
            token = next(iter(photos.values())).get("token")
            if not token:
                raise UploadFailed("Missing MAX photo token")
            return {"_type": "PHOTO", "photoToken": token}
        slots = response.get("info") or []
        if not slots or not all(slots[0].get(k) for k in ("url", "videoId", "token")):
            raise UploadFailed("Missing MAX media upload slot")
        slot = slots[0]
        media_id = str(slot["videoId"])
        event = self.ready.setdefault(media_id, asyncio.Event())
        await self.http_upload(slot["url"], data, filename)
        if kind == "voice":
            # MAX's voice attachment also carries an 80-byte waveform.  The
            # reference client uses zero samples when Telegram supplies no
            # waveform in its voice object.
            return {"_type": "AUDIO", "token": slot["token"],
                    "duration": int(duration * 1000), "wave": "\x00" * 80}
        try:
            await asyncio.wait_for(event.wait(), 60)
        except asyncio.TimeoutError as exc:
            self.ready.pop(media_id, None)
            raise UploadFailed("MAX video processing timed out") from exc
        return {"_type": "VIDEO", "videoId": slot["videoId"], "token": slot["token"], "videoType": 0}

    async def send_confirmed(self, chat_id, text, attaches):
        response = await self.request(64, {"chatId": int(chat_id), "notify": True,
            "message": {"text": text or None, "cid": time.time_ns() // 1000,
                        "elements": [], "attaches": attaches}})
        message = response.get("message") or response
        if not isinstance(message, dict) or not message.get("id"):
            raise RuntimeError("MAX did not confirm a message ID")
        return str(message["id"])

    async def deliver(self, chat_id, text, *, kind=None, data=None, filename="file", duration=0):
        degraded = False
        attaches = []
        try:
            if data is not None:
                if kind in ("photo", "video", "voice"):
                    try:
                        attach = await self.upload_native(kind, data, filename, duration)
                    except (UploadFailed, MaxRejected, asyncio.TimeoutError):
                        # No message has been sent yet: a file fallback is safe.
                        attach = await self.client.upload_file(data, filename)
                        degraded = True
                else:
                    attach = await self.client.upload_file(data, filename)
                if not attach:
                    raise UploadFailed("MAX rejected the upload")
                attaches = [attach]
            try:
                result = await self.send_confirmed(chat_id, text, attaches)
            except MaxRejected as exc:
                # Retry only an explicit rejection, never an uncertain timeout.
                if kind not in ("photo", "video", "voice") or degraded or "attachment" not in str(exc):
                    raise
                if "not.ready" in str(exc):
                    ready_to_retry = False
                    try:
                        events = list(self.ready.values())
                        if events:
                            await asyncio.wait_for(events[-1].wait(), 30)
                        else:
                            await asyncio.sleep(2)
                        ready_to_retry = True
                    except asyncio.TimeoutError:
                        pass
                    if ready_to_retry:
                        try:
                            result = await self.send_confirmed(chat_id, text, attaches)
                            return result, False
                        except MaxRejected:
                            pass
                attach = await self.client.upload_file(data, filename)
                if not attach:
                    raise UploadFailed("MAX rejected the file fallback")
                result = await self.send_confirmed(chat_id, text, [attach])
                degraded = True
            return result, degraded
        finally:
            self.ready.clear()
