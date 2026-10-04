from __future__ import annotations

from dataclasses import dataclass
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
import sqlite3


@dataclass(frozen=True)
class Account:
    id: int
    phone: str
    session_string: str


@dataclass(frozen=True)
class CallSettings:
    account_id: int
    enabled: bool
    audio_path: str | None
    audio_name: str | None


class Database:
    def __init__(self, path: str) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()

    @contextmanager
    def _connect(self):
        connection = sqlite3.connect(self.path)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def _init_schema(self) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS accounts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    phone TEXT NOT NULL UNIQUE,
                    session_string TEXT NOT NULL,
                    added_at TEXT NOT NULL
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS call_settings (
                    account_id INTEGER PRIMARY KEY,
                    enabled INTEGER NOT NULL DEFAULT 0,
                    audio_path TEXT,
                    audio_name TEXT,
                    updated_at TEXT NOT NULL,
                    FOREIGN KEY(account_id) REFERENCES accounts(id) ON DELETE CASCADE
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS recipients (
                    account_id INTEGER NOT NULL,
                    user_id INTEGER NOT NULL,
                    username TEXT,
                    display_name TEXT NOT NULL,
                    first_seen TEXT NOT NULL,
                    last_seen TEXT NOT NULL,
                    incoming_count INTEGER NOT NULL DEFAULT 1,
                    PRIMARY KEY(account_id, user_id),
                    FOREIGN KEY(account_id) REFERENCES accounts(id) ON DELETE CASCADE
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS broadcasts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    account_id INTEGER NOT NULL,
                    created_at TEXT NOT NULL,
                    finished_at TEXT,
                    eligible INTEGER NOT NULL DEFAULT 0,
                    sent INTEGER NOT NULL DEFAULT 0,
                    failed INTEGER NOT NULL DEFAULT 0,
                    status TEXT NOT NULL,
                    FOREIGN KEY(account_id) REFERENCES accounts(id) ON DELETE CASCADE
                )
                """
            )
            connection.execute('CREATE TABLE IF NOT EXISTS callers(account_id INTEGER NOT NULL REFERENCES accounts(id) ON DELETE CASCADE, user_id INTEGER NOT NULL, PRIMARY KEY(account_id,user_id))')
            connection.execute('CREATE TABLE IF NOT EXISTS channels(chat_id INTEGER PRIMARY KEY, title TEXT NOT NULL)')
            connection.execute('''CREATE TABLE IF NOT EXISTS streams(
                id INTEGER PRIMARY KEY, account_id INTEGER NOT NULL REFERENCES accounts(id),
                chat_id INTEGER NOT NULL, start_at INTEGER NOT NULL, audio_path TEXT NOT NULL,
                call_id INTEGER NOT NULL, access_hash INTEGER NOT NULL,
                status TEXT NOT NULL DEFAULT 'scheduled', error TEXT)''')

    def save_stream(self, account_id, chat_id, start_at, audio_path, call_id, access_hash):
        with self._connect() as c:
            return c.execute('INSERT INTO streams(account_id,chat_id,start_at,audio_path,call_id,access_hash) VALUES(?,?,?,?,?,?)',
                             (account_id,chat_id,start_at,audio_path,call_id,access_hash)).lastrowid

    def stream_jobs(self):
        with self._connect() as c:
            return c.execute("SELECT * FROM streams WHERE status IN ('scheduled','running') ORDER BY start_at").fetchall()

    def stream_status(self, job_id, status, error=None):
        with self._connect() as c:
            c.execute('UPDATE streams SET status=?,error=? WHERE id=?', (status,error,job_id))

    def remember_caller(self, account_id: int, user_id: int) -> None:
        with self._connect() as connection:
            connection.execute('INSERT OR IGNORE INTO callers VALUES (?, ?)', (account_id, user_id))

    def caller_ids(self, account_id: int) -> list[int]:
        with self._connect() as connection:
            return [row[0] for row in connection.execute('SELECT user_id FROM callers WHERE account_id=?', (account_id,))]

    def save_channel(self, chat_id: int, title: str) -> None:
        with self._connect() as connection:
            connection.execute('INSERT INTO channels VALUES (?, ?) ON CONFLICT(chat_id) DO UPDATE SET title=excluded.title', (chat_id, title))

    def channels(self):
        with self._connect() as connection:
            return connection.execute('SELECT chat_id,title FROM channels ORDER BY title').fetchall()

    def save_account(self, phone: str, session_string: str) -> Account:
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO accounts(phone, session_string, added_at)
                VALUES (?, ?, ?)
                ON CONFLICT(phone) DO UPDATE SET session_string = excluded.session_string
                """,
                (phone, session_string, datetime.now(timezone.utc).isoformat()),
            )
            row = connection.execute(
                "SELECT id, phone, session_string FROM accounts WHERE phone = ?", (phone,)
            ).fetchone()
        assert row is not None
        return Account(int(row["id"]), str(row["phone"]), str(row["session_string"]))

    def list_accounts(self) -> list[Account]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT id, phone, session_string FROM accounts ORDER BY id"
            ).fetchall()
        return [Account(int(r["id"]), str(r["phone"]), str(r["session_string"])) for r in rows]

    def get_account(self, account_id: int) -> Account | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT id, phone, session_string FROM accounts WHERE id = ?", (account_id,)
            ).fetchone()
        if row is None:
            return None
        return Account(int(row["id"]), str(row["phone"]), str(row["session_string"]))

    def delete_account(self, account_id: int) -> None:
        with self._connect() as connection:
            connection.execute("DELETE FROM accounts WHERE id = ?", (account_id,))

    def get_call_settings(self, account_id: int) -> CallSettings:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT account_id, enabled, audio_path, audio_name FROM call_settings WHERE account_id = ?",
                (account_id,),
            ).fetchone()
        if row is None:
            return CallSettings(account_id, False, None, None)
        return CallSettings(
            int(row["account_id"]), bool(row["enabled"]), row["audio_path"], row["audio_name"]
        )

    def set_call_audio(self, account_id: int, audio_path: str, audio_name: str) -> None:
        now = datetime.now(timezone.utc).isoformat()
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO call_settings(account_id, enabled, audio_path, audio_name, updated_at)
                VALUES (?, 0, ?, ?, ?)
                ON CONFLICT(account_id) DO UPDATE SET
                    audio_path = excluded.audio_path,
                    audio_name = excluded.audio_name,
                    updated_at = excluded.updated_at
                """,
                (account_id, audio_path, audio_name, now),
            )

    def set_calls_enabled(self, account_id: int, enabled: bool) -> None:
        now = datetime.now(timezone.utc).isoformat()
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO call_settings(account_id, enabled, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(account_id) DO UPDATE SET
                    enabled = excluded.enabled,
                    updated_at = excluded.updated_at
                """,
                (account_id, int(enabled), now),
            )

    def upsert_recipient(
        self, account_id: int, user_id: int, username: str | None, display_name: str
    ) -> None:
        now = datetime.now(timezone.utc).isoformat()
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO recipients(
                    account_id, user_id, username, display_name, first_seen, last_seen, incoming_count
                ) VALUES (?, ?, ?, ?, ?, ?, 1)
                ON CONFLICT(account_id, user_id) DO UPDATE SET
                    username = excluded.username,
                    display_name = excluded.display_name,
                    last_seen = excluded.last_seen,
                    incoming_count = recipients.incoming_count + 1
                """,
                (account_id, user_id, username, display_name, now, now),
            )

    def list_recipient_ids(self, account_id: int) -> list[int]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT user_id FROM recipients WHERE account_id = ? ORDER BY last_seen DESC",
                (account_id,),
            ).fetchall()
        return [int(row["user_id"]) for row in rows]

    def recipient_count(self, account_id: int) -> int:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT COUNT(*) AS amount FROM recipients WHERE account_id = ?", (account_id,)
            ).fetchone()
        return int(row["amount"] if row else 0)

    def create_broadcast(self, account_id: int) -> int:
        with self._connect() as connection:
            cursor = connection.execute(
                "INSERT INTO broadcasts(account_id, created_at, status) VALUES (?, ?, 'running')",
                (account_id, datetime.now(timezone.utc).isoformat()),
            )
            return int(cursor.lastrowid)

    def finish_broadcast(
        self, broadcast_id: int, eligible: int, sent: int, failed: int, status: str
    ) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                UPDATE broadcasts
                SET finished_at = ?, eligible = ?, sent = ?, failed = ?, status = ?
                WHERE id = ?
                """,
                (
                    datetime.now(timezone.utc).isoformat(), eligible, sent, failed, status,
                    broadcast_id,
                ),
            )

    def recent_broadcasts(self, limit: int = 10) -> list[sqlite3.Row]:
        with self._connect() as connection:
            return connection.execute(
                """
                SELECT b.*, a.phone
                FROM broadcasts b JOIN accounts a ON a.id = b.account_id
                ORDER BY b.id DESC LIMIT ?
                """,
                (limit,),
            ).fetchall()

