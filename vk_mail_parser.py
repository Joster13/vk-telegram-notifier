"""Parse the observed VK single-message and digest email templates offline.

Authentication-Results is trusted ONLY for mail fetched directly from Gmail
over authenticated TLS. This module does not cryptographically verify .eml files.
Unknown layouts fail closed rather than borrowing another card's sender/text.
"""
from __future__ import annotations

import email
import logging
import re
from email import policy
from urllib.parse import parse_qs, urlencode, urlsplit

from bs4 import BeautifulSoup

MAX_MAIL_BYTES = 2_000_000
MISSING_TEXT = "Текст сообщения не включён в письмо ВК. Откройте сообщение по кнопке."
STAMP = re.compile(r"\d{1,2}\s+(?:янв|фев|мар|апр|ма[йя]|июн|июл|авг|сен|окт|ноя|дек)[а-яё]*\.?\s+в\s+(?:[01]?\d|2[0-3]):[0-5]\d", re.I)
HOSTS = {"vk.ru", "vk.com", "m.vk.ru", "m.vk.com"}
LOG = logging.getLogger("vk-notify.parser")


def clean_vk_link(link: str) -> str | None:
    """Allow only message permalinks and profile paths; never fetch trackers."""
    for _ in range(5):
        if not isinstance(link, str) or len(link) > 8192 or re.search(r"[\s\\\x00-\x1f]", link):
            return None
        try:
            url = urlsplit(link)
            if (url.scheme != "https" or url.hostname not in HOSTS or url.username
                    or url.password or url.port not in (None, 443) or url.fragment):
                return None
            q = parse_qs(url.query, keep_blank_values=True, max_num_fields=30)
        except ValueError:
            return None
        if url.path == "/email.php":
            if len(q.get("u", [])) != 1:
                return None
            link = q["u"][0]
            continue
        if url.path == "/im":
            if not re.fullmatch(r"-?[1-9]\d{0,19}", q.get("sel", [""])[0]):
                return None
            if not any(k in q for k in ("msgid", "cmid")):
                return None
            for key in ("sel", "msgid", "cmid"):
                if key in q and (len(q[key]) != 1 or not re.fullmatch(
                        r"-?[1-9]\d{0,19}" if key == "sel" else r"[1-9]\d{0,19}", q[key][0])):
                    return None
            # Preserve the previous ordering for compatibility with sent hashes.
            return "https://vk.com/im?" + urlencode([(k, q[k][0]) for k in ("sel", "msgid", "cmid") if k in q])
        if not url.query and re.fullmatch(r"/[A-Za-z0-9_.-]{1,100}", url.path) and url.path not in {
                "/settings", "/feed", "/im", "/friends", "/login", "/away.php", "/email_direct.php"}:
            return "https://vk.com" + url.path
        return None
    return None


def authenticated(mail, sender: str) -> bool:
    """Require one From mailbox and Gmail's aligned DKIM + DMARC verdicts.

    Do not accept ARC or a pass asserted by another authentication service.
    Gmail must strip externally supplied headers claiming mx.google.com (RFC8601).
    """
    values = mail.get_all("From", [])
    if len(values) != 1 or values[0].defects:
        return False
    addresses = values[0].addresses
    if len(addresses) != 1 or addresses[0].addr_spec.lower() != sender.lower():
        return False
    domain = addresses[0].domain.lower()
    if domain not in {"notify.vk.com", "notify.vk.ru"}:
        return False
    results = [str(v) for v in mail.get_all("Authentication-Results", [])
               if str(v).split(";", 1)[0].strip().lower() == "mx.google.com"]
    if len(results) != 1:
        return False
    # Ignore comments and quoted strings: neither can assert a passing method.
    value = re.sub(r'"(?:\\.|[^"\\])*"', '""', results[0].lower())
    while re.search(r"\([^()]*\)", value):
        value = re.sub(r"\([^()]*\)", "", value)
    if "(" in value or ")" in value:
        return False
    clauses = value.split(";")[1:]
    dkim = any(re.match(r"\s*dkim=pass\s", c) and re.search(
        r"\bheader\.(?:i=@|d=)" + re.escape(domain) + r"(?=\s|$)", c) for c in clauses)
    root = domain.removeprefix("notify.")
    dmarc = any(re.match(r"\s*dmarc=pass\s", c) and re.search(
        r"\bheader\.from=(?:" + re.escape(domain) + "|" + re.escape(root) + r")(?=\s|$)", c) for c in clauses)
    return dkim and dmarc


def normalized(node) -> str:
    return re.sub(r"\s+", " ", node.get_text(" ", strip=True)).strip()


def body_text(node) -> str:
    # Keep explicit paragraph/line breaks without splitting inline formatting.
    copy = BeautifulSoup(str(node), "html.parser")
    for br in copy.find_all("br"):
        br.replace_with("\n")
    for block in copy.find_all(["p", "div"]):
        block.insert_before("\n")
        block.insert_after("\n")
    return "\n".join(re.sub(r"[\t \r\f\v]+", " ", line).strip()
                     for line in copy.get_text().split("\n")).strip()


def html_messages(content: str) -> list[dict]:
    soup = BeautifulSoup(content, "html.parser")
    for node in list(soup.find_all(["script", "style", "head", "blockquote"])):
        node.decompose()
    for node in list(soup.find_all(True)):
        if node.attrs is not None and (node.has_attr("hidden") or re.search(
                r"(?:display\s*:\s*none|visibility\s*:\s*hidden)", node.get("style", ""), re.I)):
            node.decompose()
    result = {}
    for a in soup.find_all("a", href=True):
        target = clean_vk_link(a["href"])
        if not target or urlsplit(target).path != "/im":
            continue
        # Each observed card starts with a two-row table: profile and date.
        header = a.find_parent("table")
        if header is None:
            continue
        rows = [r for r in header.find_all("tr") if r.find_parent("table") is header]
        if len(rows) != 2:
            continue
        names = [p for p in rows[0].find_all("a", href=True)
                 if normalized(p) and (u := clean_vk_link(p["href"])) and urlsplit(u).path != "/im"]
        dates = [normalized(td) for td in rows[1].find_all("td", recursive=False)]
        if len(names) != 1 or len(dates) != 1 or not STAMP.fullmatch(dates[0]):
            continue
        sender = normalized(names[0])
        if len(sender) > 140:
            continue
        if a.find_parent("tr") is rows[1] and STAMP.fullmatch(normalized(a)):
            # Single notification: body is the next row in the OUTER card table.
            card = header.find_parent("table")
            if card is None:
                continue
            card_rows = [r for r in card.find_all("tr") if r.find_parent("table") is card]
            if len(card_rows) < 2 or header.find_parent("tr") is not card_rows[0]:
                continue
            cells = card_rows[1].find_all("td", recursive=False)
            if len(cells) != 1:
                continue
            text = body_text(cells[0]) or MISSING_TEXT
        elif a.find_parent("tr") is rows[0] and a.parent is names[0].parent:
            # Digest: distinguish the template's generic link from literal text
            # "сообщение" by the words BEFORE the anchor, not its caption.
            prefix = " ".join(str(s) if isinstance(s, str) else s.get_text(" ", strip=True)
                              for s in reversed(list(a.previous_siblings)))
            prefix = re.sub(r"\s+", " ", prefix).strip()
            if re.search(r"отправил[аи]? вам сообщение", prefix, re.I):
                text = body_text(a) or MISSING_TEXT
            elif re.search(r"отправил[аи]? вам", prefix, re.I) and normalized(a).lower() == "сообщение":
                text = MISSING_TEXT
            else:
                continue
        else:
            continue
        item = {"sender": sender, "text": text, "url": target, "time": dates[0]}
        if target in result and result[target] != item:
            return []  # Conflicting cards must not choose an arbitrary sender.
        result[target] = item
    return list(result.values())


def plain_messages(content: str) -> list[dict]:
    # The observed plain digest contains only a count and /im?tab=unread.
    # It cannot supply per-message names, timestamps or permalinks.
    content = content.replace("\r\n", "\n")
    match = re.search(r"(?:^|\n\n)([^\n]{1,140}) оставил[аио]? вам личное сообщение\.\n\n"
                      r"(.*?)\n\nПросмотреть сообщение можно на странице:\s*\n"
                      r"\s*(https://[^\s]+)", content, re.S)
    if not match:
        return []
    sender, text, link = match.groups()
    target = clean_vk_link(link)
    if not target or urlsplit(target).path != "/im":
        return []
    # Some plain parts repeat the same permalink at the end of the preview.
    lines = text.strip().splitlines()
    if lines and clean_vk_link(lines[-1].strip()) == target:
        lines.pop()
    return [{"sender": sender, "text": "\n".join(lines).strip() or MISSING_TEXT,
             "url": target, "time": ""}]


def extract_messages(data: bytes, sender: str = "admin@notify.vk.com") -> list[dict]:
    if len(data) > MAX_MAIL_BYTES:
        LOG.warning("VK mail skipped: size limit")
        return []
    try:
        mail = email.message_from_bytes(data, policy=policy.default)
        if not authenticated(mail, sender):
            LOG.warning("VK mail skipped: authentication not accepted")
            return []
        html_part = mail.get_body(preferencelist=("html",))
        if html_part is not None:
            # Do not merge MIME alternatives or turn malformed HTML into guesses.
            items = html_messages(html_part.get_content())
            if not items:
                LOG.info("VK mail skipped: no supported message cards")
            return items
        plain = mail.get_body(preferencelist=("plain",))
        return plain_messages(plain.get_content()) if plain is not None else []
    except (ValueError, LookupError, UnicodeError, RecursionError):
        LOG.warning("VK mail skipped: malformed content")
        return []
