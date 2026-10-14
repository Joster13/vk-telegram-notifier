"""Persistent reply routing. Stores IDs and labels, never message/file contents."""
import secrets
import sqlite3
import time
from pathlib import Path


class ReplyStore:
    def __init__(self, path):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path, timeout=20)
        self.db.executescript('''
            CREATE TABLE IF NOT EXISTS routes (
                token TEXT PRIMARY KEY, chat_id TEXT NOT NULL,
                message_id TEXT NOT NULL, label TEXT NOT NULL, expires REAL NOT NULL,
                UNIQUE(chat_id, message_id));
            CREATE TABLE IF NOT EXISTS prompts (
                owner INTEGER NOT NULL, prompt_id INTEGER NOT NULL,
                token TEXT NOT NULL, expires REAL NOT NULL,
                PRIMARY KEY(owner, prompt_id));
            CREATE TABLE IF NOT EXISTS attempts (
                owner INTEGER NOT NULL, message_id INTEGER NOT NULL,
                state TEXT NOT NULL, created REAL NOT NULL,
                PRIMARY KEY(owner, message_id));
        ''')
        self.db.row_factory = sqlite3.Row

    def close(self):
        self.db.close()

    def add_route(self, chat_id, label, message_id):
        now = time.time()
        message_id = str(message_id or secrets.token_urlsafe(12))
        with self.db:
            self.db.execute("DELETE FROM routes WHERE expires < ?", (now,))
            self.db.execute("DELETE FROM prompts WHERE expires < ?", (now,))
            self.db.execute("DELETE FROM attempts WHERE created < ?", (now - 30 * 86400,))
            self.db.execute("INSERT OR IGNORE INTO routes VALUES (?,?,?,?,?)",
                (secrets.token_urlsafe(12), str(chat_id), message_id,
                 str(label)[:160], now + 30 * 86400))
        return self.db.execute("SELECT token FROM routes WHERE chat_id=? AND message_id=?",
            (str(chat_id), message_id)).fetchone()[0]

    def route(self, token):
        row = self.db.execute("SELECT * FROM routes WHERE token=? AND expires>?",
                              (token, time.time())).fetchone()
        return dict(row) if row else None

    def add_prompt(self, owner, prompt_id, token):
        with self.db:
            self.db.execute("INSERT OR REPLACE INTO prompts VALUES (?,?,?,?)",
                            (owner, prompt_id, token, time.time() + 3600))

    def prompt(self, owner, prompt_id):
        row = self.db.execute("SELECT token FROM prompts WHERE owner=? AND prompt_id=? AND expires>?",
                              (owner, prompt_id, time.time())).fetchone()
        return self.route(row[0]) if row else None

    def cancel(self, owner):
        with self.db:
            self.db.execute("DELETE FROM prompts WHERE owner=?", (owner,))

    def consume_prompt(self, owner, prompt_id):
        with self.db:
            self.db.execute("DELETE FROM prompts WHERE owner=? AND prompt_id=?", (owner, prompt_id))

    def claim(self, owner, message_id):
        with self.db:
            cur = self.db.execute("INSERT OR IGNORE INTO attempts VALUES (?,?,'sending',?)",
                                  (owner, message_id, time.time()))
        return cur.rowcount == 1

    def finish(self, owner, message_id, state):
        with self.db:
            self.db.execute("UPDATE attempts SET state=? WHERE owner=? AND message_id=?",
                            (state, owner, message_id))
