"""Apply only the presentation changes to the reviewed, pinned MAX source."""
from pathlib import Path
import shutil
import sys

OLD_HEADER = '''def _header(msg: MaxMessage, sender_label: str, chat_label: str, is_dm: bool) -> str:
    if is_dm:
        return f"✉ <b>{sender_label}</b>"
    if chat_label:
        return f"💬 <b>{chat_label}</b> | {sender_label}"
    return f"💬 <b>{sender_label}</b>"'''
OLD_KEYBOARD = "kb = reply_keyboard(msg.chat_id) if reply_enabled else None"
OLD_READY = "        participant_ids = resolver.load_snapshot(snapshot)"
OLD_MESSAGE = "    @client.on_message\\n    async def handle_message(msg: MaxMessage):\\n        log.info("


def patch_source(source):
    if any(source.count(marker) != 1 for marker in (OLD_HEADER, OLD_KEYBOARD, OLD_READY, OLD_MESSAGE)):
        raise RuntimeError("Pinned MAX listener changed; refusing an unverified patch")
    source = source.replace(OLD_HEADER,
        "from app.dragon_notify_format import format_header as _header, notification_keyboard, should_mute_max_chat")
    source = source.replace(OLD_READY, OLD_READY + """
        # Do not forward any events from specifically muted groups.
        # Store IDs in upstream's early dispatch filter to suppress media too.
        for _cid, _title in resolver.chats.items():
            if should_mute_max_chat(_cid, _title, resolver.is_dm(_cid)):
                if _cid not in client.exclude_chat_ids:
                    client.exclude_chat_ids.append(_cid)""")
    source = source.replace(OLD_MESSAGE, """    @client.on_message
    async def handle_message(msg: MaxMessage):
        # Also check names when an event arrived before the snapshot was cached.
        if should_mute_max_chat(msg.chat_id, resolver.chat_name(msg.chat_id), resolver.is_dm(msg.chat_id)):
            return
        log.info(""")
    return source.replace(OLD_KEYBOARD,
        "kb = reply_keyboard(msg.chat_id) if reply_enabled else notification_keyboard(msg, sender_label, chat_label, is_dm)")


def main(root):
    listener = Path(root) / "app" / "max_listener.py"
    source = patch_source(listener.read_text(encoding="utf-8"))
    compile(source, str(listener), "exec")
    shutil.copyfile(Path(__file__).with_name("max_notify_format.py"),
                    listener.with_name("dragon_notify_format.py"))
    listener.write_text(source, encoding="utf-8")


if __name__ == "__main__":
    main(sys.argv[1])
