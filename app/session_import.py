"""Validate and copy Pyrogram SQLite sessions without displaying credentials."""
import sqlite3
from contextlib import closing
from pathlib import Path
from uuid import uuid4


def import_session(source: Path, destination_dir: Path) -> Path:
    source = source.resolve(strict=True)
    if source.suffix.lower() != '.session':
        raise ValueError('Нужен файл Pyrogram/Pyrofork .session')
    destination_dir.mkdir(parents=True, exist_ok=True)
    destination = destination_dir / f'{uuid4().hex}.session'
    connection = sqlite3.connect(source.as_uri() + '?mode=ro', uri=True)
    try:
        columns = {row[1] for row in connection.execute('PRAGMA table_info(sessions)')}
        if not {'api_id', 'auth_key', 'user_id', 'is_bot', 'dc_id', 'test_mode'} <= columns:
            raise ValueError('Это не сессия Pyrogram/Pyrofork; Telethon .session не подходит')
        row = connection.execute('SELECT api_id, auth_key, user_id, is_bot, test_mode FROM sessions LIMIT 1').fetchone()
        if not row or not row[0] or not row[1] or len(row[1]) != 256 or not row[2] or row[3] != 0 or row[4] is None:
            raise ValueError('Нужна уже авторизованная сессия пользовательского аккаунта')
        with closing(sqlite3.connect(destination)) as target:
            connection.backup(target)
    finally:
        connection.close()
    return destination.resolve()
