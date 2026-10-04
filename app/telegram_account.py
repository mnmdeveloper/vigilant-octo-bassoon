from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path
from typing import Awaitable, Callable

from telethon import TelegramClient
from telethon.errors import FloodWaitError
from telethon.sessions import StringSession
from pyrogram import Client, enums
from pyrogram.errors import FloodWait

from app.config import Settings


@dataclass
class BroadcastResult:
    eligible: int = 0
    sent: int = 0
    failed: int = 0
    stopped_by_flood_wait: int | None = None


def new_client(settings: Settings, session: str = ""):
    if session.startswith('pyrofile:'):
        path = Path(session.removeprefix('pyrofile:'))
        if not path.is_file():
            raise RuntimeError('Файл сессии отсутствует; импортируйте его повторно')
        return Client(path.stem, workdir=str(path.parent), no_updates=False,
                      sleep_threshold=0, parse_mode=enums.ParseMode.DISABLED)
    if not settings.api_id or not settings.api_hash:
        raise RuntimeError('Для входа по номеру или старой сессии Telethon нужны API-ключи. Импортируйте Pyrofork .session.')
    client = TelegramClient(
        StringSession(session), settings.api_id, settings.api_hash,
        flood_sleep_threshold=0,
    )
    client.parse_mode = None
    return client


async def channel_summary(client: TelegramClient) -> str:
    lines: list[str] = []
    if isinstance(client, Client):
        async for dialog in client.get_dialogs():
            if dialog.chat.type in (enums.ChatType.CHANNEL, enums.ChatType.SUPERGROUP):
                lines.append(f'• {dialog.chat.title} — {dialog.chat.type.value}')
        return '\n'.join(lines) or 'Каналы и супергруппы не найдены.'
    async for dialog in client.iter_dialogs():
        if dialog.is_channel:
            kind = "группа" if getattr(dialog.entity, "megagroup", False) else "канал"
            lines.append(f"• {dialog.name or 'Без названия'} — {kind}")
    return "\n".join(lines) if lines else "Каналы и супергруппы не найдены."


async def find_opted_in_users(
    client: TelegramClient,
    on_found: Callable[[object], None] | None = None,
    scan_limit: int = 100,
) -> list[int]:
    """Find private users with an incoming message in recent dialog history."""
    result: list[int] = []
    me = await client.get_me()
    if isinstance(client, Client):
        async for dialog in client.get_dialogs():
            chat = dialog.chat
            if chat.type != enums.ChatType.PRIVATE or chat.id == me.id:
                continue
            async for message in client.get_chat_history(chat.id, limit=scan_limit):
                if not message.outgoing and message.from_user and not message.from_user.is_bot:
                    result.append(chat.id)
                    if on_found:
                        on_found(message.from_user)
                    break
        return result
    async for dialog in client.iter_dialogs():
        entity = dialog.entity
        if not dialog.is_user or entity.id == me.id or getattr(entity, "bot", False):
            continue
        async for message in client.iter_messages(entity, limit=scan_limit):
            if not message.out:
                result.append(entity.id)
                if on_found:
                    on_found(entity)
                break
    return result


async def broadcast_to_users(
    client: TelegramClient,
    recipients: list[int],
    text: str | None,
    media_path: Path | None,
    delay_seconds: float,
    progress: Callable[[BroadcastResult], Awaitable[None]] | None = None,
    media_kind: str = 'photo',
) -> BroadcastResult:
    result = BroadcastResult(eligible=len(recipients))
    for index, user_id in enumerate(recipients, start=1):
        try:
            if media_path:
                if media_kind == 'voice':
                    if isinstance(client, Client):
                        await client.send_voice(user_id, str(media_path), caption=text or '')
                    else:
                        await client.send_file(user_id, str(media_path), caption=text or '', voice_note=True)
                elif isinstance(client, Client):
                    await client.send_photo(user_id, str(media_path), caption=text or '')
                else:
                    await client.send_file(user_id, str(media_path), caption=text or "")
            elif text:
                await client.send_message(user_id, text)
            result.sent += 1
        except FloodWaitError as error:
            result.stopped_by_flood_wait = int(error.seconds)
            break
        except FloodWait as error:
            result.stopped_by_flood_wait = int(error.value)
            break
        except Exception:
            result.failed += 1
        if progress and index % 10 == 0:
            await progress(result)
        await asyncio.sleep(delay_seconds)
    return result
