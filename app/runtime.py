from __future__ import annotations

import asyncio
from contextlib import suppress
import logging
from dataclasses import dataclass
from pathlib import Path

from pytgcalls import PyTgCalls
from pytgcalls.types import ChatUpdate, Device, MediaStream, StreamEnded
from pytgcalls.types.stream import AudioQuality
from telethon import TelegramClient, events
from pyrogram import Client, filters
from pyrogram.handlers import MessageHandler

from app.config import Settings
from app.db import Account, Database
from app.telegram_account import new_client

log = logging.getLogger(__name__)


@dataclass
class AccountRuntime:
    account: Account
    client: TelegramClient
    calls: PyTgCalls
    active_auto_calls: set[int]


class RuntimeManager:
    def __init__(self, settings: Settings, db: Database) -> None:
        self.settings = settings
        self.db = db
        self._items: dict[int, AccountRuntime] = {}
        self._lock = asyncio.Lock()

    async def start_all(self) -> None:
        for account in self.db.list_accounts():
            try:
                await self.start_account(account)
            except Exception:
                log.exception("Could not start account %s", account.phone)

    async def start_account(self, account: Account) -> AccountRuntime:
        async with self._lock:
            existing = self._items.get(account.id)
            if existing:
                return existing

            client = new_client(self.settings, account.session_string)
            if isinstance(client, Client):
                authorized = await client.connect()
                if authorized:
                    try:
                        # connect/initialize bypass Client.start(), which normally
                        # populates this field. Media uploads read me.is_premium.
                        client.me = await client.get_me()
                        await client.initialize()
                    except Exception:
                        await client.disconnect()
                        raise
            else:
                await client.connect()
                authorized = await client.is_user_authorized()
            if not authorized:
                await client.disconnect()
                raise RuntimeError(f"Сессия {account.phone} недействительна")

            async def maybe_auto_reply(user_id: int, send):
                try:
                    enabled, text = self.db.get_auto_reply(account.id)
                    if not enabled or not text.strip():
                        return
                    if not self.db.claim_auto_reply(account.id, user_id):
                        return
                    try:
                        await send(text)
                    except Exception:
                        self.db.release_auto_reply(account.id, user_id)
                        raise
                except Exception:
                    log.exception("Auto-reply failed for account %s and user %s", account.phone, user_id)

            async def remember_sender(event) -> None:
                if not event.is_private:
                    return
                try:
                    sender = await event.get_sender()
                    if sender is None or getattr(sender, "bot", False):
                        return
                    name = " ".join(
                        part
                        for part in (
                            getattr(sender, "first_name", None),
                            getattr(sender, "last_name", None),
                        )
                        if part
                    ) or str(sender.id)
                    self.db.upsert_recipient(
                        account.id, sender.id, getattr(sender, "username", None), name
                    )
                except Exception:
                    log.exception("Could not remember incoming message sender for %s", account.phone)
                sender_id = getattr(event, "sender_id", None)
                if sender_id is not None:
                    await maybe_auto_reply(sender_id, lambda text: event.reply(text))

            if isinstance(client, Client):
                async def remember_pyro(_, message):
                    sender = message.from_user
                    try:
                        sender = message.from_user
                        if sender is None or sender.is_bot:
                            return
                        name = " ".join(
                            part for part in (sender.first_name, sender.last_name) if part
                        ) or str(sender.id)
                        self.db.upsert_recipient(
                            account.id, sender.id, sender.username, name
                        )
                    except Exception:
                        log.exception("Could not remember incoming message sender for %s", account.phone)
                    if sender and not sender.is_bot:
                        await maybe_auto_reply(sender.id, lambda text: message.reply_text(text))
                client.add_handler(MessageHandler(remember_pyro, filters.private & filters.incoming))
            else:
                client.add_event_handler(remember_sender, events.NewMessage(incoming=True))

            calls = PyTgCalls(client)
            runtime = AccountRuntime(account, client, calls, set())

            @calls.on_update()
            async def handle_call_update(call_client: PyTgCalls, update) -> None:
                if isinstance(update, ChatUpdate) and update.status & ChatUpdate.Status.INCOMING_CALL:
                    self.db.remember_caller(account.id, update.chat_id)
                    call_settings = self.db.get_call_settings(account.id)
                    audio_path = Path(call_settings.audio_path) if call_settings.audio_path else None
                    if not call_settings.enabled or audio_path is None or not audio_path.exists():
                        return
                    try:
                        runtime.active_auto_calls.add(update.chat_id)
                        stream = MediaStream(
                            audio_path,
                            audio_parameters=AudioQuality.HIGH,
                            video_flags=MediaStream.Flags.IGNORE,
                            audio_flags=MediaStream.Flags.REQUIRED,
                        )
                        await call_client.play(update.chat_id, stream)
                    except Exception:
                        runtime.active_auto_calls.discard(update.chat_id)
                        with suppress(Exception):
                            await call_client.leave_call(update.chat_id)
                        log.exception("Could not answer incoming call for %s", account.phone)

                if (
                    isinstance(update, StreamEnded)
                    and update.chat_id in runtime.active_auto_calls
                    and update.device == Device.MICROPHONE
                ):
                    runtime.active_auto_calls.discard(update.chat_id)
                    with suppress(Exception):
                        await call_client.leave_call(update.chat_id)

                if isinstance(update, ChatUpdate) and update.status & ChatUpdate.Status.LEFT_CALL:
                    runtime.active_auto_calls.discard(update.chat_id)

            try:
                await calls.start()
            except Exception:
                if isinstance(client, Client):
                    await client.stop()
                else:
                    await client.disconnect()
                raise
            self._items[account.id] = runtime
            return runtime

    async def stop_account(self, account_id: int) -> None:
        async with self._lock:
            runtime = self._items.pop(account_id, None)
        if runtime:
            for chat_id in list(runtime.active_auto_calls):
                with suppress(Exception):
                    await runtime.calls.leave_call(chat_id)
            if isinstance(runtime.client, Client):
                await runtime.client.stop()
            else:
                await runtime.client.disconnect()

    async def stop_all(self) -> None:
        for account_id in list(self._items):
            await self.stop_account(account_id)

    async def get(self, account_id: int) -> AccountRuntime:
        runtime = self._items.get(account_id)
        if runtime:
            return runtime
        account = self.db.get_account(account_id)
        if account is None:
            raise RuntimeError("Аккаунт не найден")
        return await self.start_account(account)
