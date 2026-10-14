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


def patch_source(source):
    if source.count(OLD_HEADER) != 1 or source.count(OLD_KEYBOARD) != 1:
        raise RuntimeError("Pinned MAX listener changed; refusing an unverified patch")
    source = source.replace(OLD_HEADER,
        "from app.dragon_notify_format import format_header as _header, open_max_keyboard")
    return source.replace(OLD_KEYBOARD,
        "kb = reply_keyboard(msg.chat_id) if reply_enabled else open_max_keyboard()")


def main(root):
    listener = Path(root) / "app" / "max_listener.py"
    source = patch_source(listener.read_text(encoding="utf-8"))
    compile(source, str(listener), "exec")
    shutil.copyfile(Path(__file__).with_name("max_notify_format.py"),
                    listener.with_name("dragon_notify_format.py"))
    listener.write_text(source, encoding="utf-8")


if __name__ == "__main__":
    main(sys.argv[1])
