"""Persisted scheduled channel audio streams, driven by the local process."""
import asyncio
import logging
import secrets
import time
from contextlib import suppress
from datetime import datetime
from zoneinfo import ZoneInfo
from pathlib import Path

from pyrogram import Client, raw, utils
from pyrogram.handlers import RawUpdateHandler
from pyrogram.errors import GroupcallNotModified
from pytgcalls.types import MediaStream, GroupCallConfig, StreamEnded, Device

from app.media import validate_audio

log = logging.getLogger(__name__)


async def set_join_muted(client, call):
    try:
        await client.invoke(raw.functions.phone.ToggleGroupCallSettings(call=call, join_muted=True))
    except GroupcallNotModified:
        pass


def parse_start(value: str) -> int:
    try:
        local = datetime.strptime(value.strip(), '%d.%m.%Y %H:%M')
    except ValueError as error:
        raise ValueError('Введите дату в формате ДД.ММ.ГГГГ ЧЧ:ММ, например 04.10.2026 15:56. Год должен состоять из 4 цифр; время — МСК.') from error
    zone = ZoneInfo('Europe/Moscow')
    first, second = local.replace(tzinfo=zone, fold=0), local.replace(tzinfo=zone, fold=1)
    if first.utcoffset() != second.utcoffset():
        raise ValueError('Время попадает на перевод часов. Выберите другое время.')
    timestamp = int(first.timestamp())
    if not 30 <= timestamp - time.time() <= 8 * 86400:
        raise ValueError('Выберите время от 30 секунд до 8 дней в будущем')
    return timestamp


class StreamScheduler:
    def __init__(self, db, runtimes, bot, admin_id):
        self.db, self.runtimes, self.bot, self.admin_id = db, runtimes, bot, admin_id
        self.tasks = {}

    async def schedule(self, account_id, chat_id, start_at, audio_path):
        if not 10 <= start_at - time.time() <= 8 * 86400:
            raise ValueError('Дата уже прошла или находится дальше 8 дней')
        await validate_audio(Path(audio_path))
        runtime = await self.runtimes.get(account_id)
        client = runtime.client
        if not isinstance(client, Client):
            raise ValueError('Для трансляций подключите аккаунт через Pyrofork .session')
        peer = await client.resolve_peer(chat_id)
        channel = raw.types.InputChannel(channel_id=peer.channel_id, access_hash=peer.access_hash)
        full = await client.invoke(raw.functions.channels.GetFullChannel(channel=channel))
        if getattr(full.full_chat, 'call', None):
            raise ValueError('В канале уже есть активная или запланированная трансляция')
        updates = await client.invoke(raw.functions.phone.CreateGroupCall(
            peer=peer, random_id=secrets.randbelow(2**31), schedule_date=start_at,
            title='Аудиотрансляция'))
        call = next((u.call for u in updates.updates if isinstance(u, raw.types.UpdateGroupCall)), None)
        if call is None:
            raise RuntimeError('Telegram создал эфир, но не вернул его идентификатор. Проверьте канал перед повтором.')
        input_call = raw.types.InputGroupCall(id=call.id, access_hash=call.access_hash)
        try:
            await set_join_muted(client, input_call)
            job_id = self.db.save_stream(account_id, chat_id, start_at, audio_path, call.id, call.access_hash)
        except Exception:
            with suppress(Exception):
                await client.invoke(raw.functions.phone.DiscardGroupCall(call=input_call))
            raise
        return job_id

    async def notify(self, text):
        with suppress(Exception):
            await self.bot.send_message(self.admin_id, text)

    async def loop(self):
        # A crashed stream must be cleaned up rather than replayed from the beginning.
        for job in self.db.stream_jobs():
            if job['status'] == 'running':
                try:
                    runtime = await self.runtimes.get(job['account_id'])
                    await runtime.client.invoke(raw.functions.phone.DiscardGroupCall(
                        call=raw.types.InputGroupCall(id=job['call_id'], access_hash=job['access_hash'])))
                except Exception:
                    log.exception('Interrupted stream cleanup failed')
                self.db.stream_status(job['id'], 'interrupted', 'Программа была остановлена во время эфира')
        while True:
            for job in self.db.stream_jobs():
                if job['status'] == 'scheduled' and job['start_at'] <= time.time() and job['id'] not in self.tasks:
                    self.tasks[job['id']] = asyncio.create_task(self.run(job))
            await asyncio.sleep(1)

    async def run(self, job):
        runtime = None
        handler = None
        call = raw.types.InputGroupCall(id=job['call_id'], access_hash=job['access_hash'])
        ended = asyncio.Event()
        moderation_errors = []
        async def on_stream(_, update):
            if isinstance(update, StreamEnded) and update.chat_id == job['chat_id'] and update.device == Device.MICROPHONE:
                ended.set()
        try:
            runtime = await self.runtimes.get(job['account_id'])
            client = runtime.client
            me = await client.get_me()
            async def mute(participants):
                for p in participants:
                    if p.left or utils.get_peer_id(p.peer) == me.id:
                        continue
                    if p.muted and not p.can_self_unmute:
                        continue
                    # Muted admins may unmute themselves; avoid a repeated update loop.
                    if p.muted:
                        continue
                    peer = await client.resolve_peer(utils.get_peer_id(p.peer))
                    await client.invoke(raw.functions.phone.EditGroupCallParticipant(call=call, participant=peer, muted=True))
            async def raw_update(_, update, users, chats):
                if isinstance(update, raw.types.UpdateGroupCallParticipants) and update.call.id == call.id:
                    try:
                        await mute(update.participants)
                    except Exception as error:
                        log.exception('Participant moderation failed')
                        moderation_errors.append(error)
                        ended.set()
            handler = RawUpdateHandler(raw_update)
            client.add_handler(handler, group=99)
            runtime.calls.add_handler(on_stream)
            self.db.stream_status(job['id'], 'running')
            await set_join_muted(client, call)
            await client.invoke(raw.functions.phone.StartScheduledGroupCall(call=call))
            stream = MediaStream(Path(job['audio_path']), video_flags=MediaStream.Flags.IGNORE,
                                 audio_flags=MediaStream.Flags.REQUIRED)
            await runtime.calls.play(job['chat_id'], stream, config=GroupCallConfig(auto_start=False, join_as=await client.resolve_peer(me.id)))
            await client.invoke(raw.functions.phone.EditGroupCallParticipant(
                call=call, participant=raw.types.InputPeerSelf(), muted=False))
            offset = ''
            while True:
                page = await client.invoke(raw.functions.phone.GetGroupParticipants(
                    call=call, ids=[], sources=[], offset=offset, limit=100))
                await mute(page.participants)
                if not page.next_offset or page.next_offset == offset:
                    break
                offset = page.next_offset
            await self.notify(f"Трансляция #{job['id']} запущена.")
            await ended.wait()
            if moderation_errors:
                raise RuntimeError('Не удалось выключить микрофон участника; эфир остановлен') from moderation_errors[0]
            await asyncio.sleep(5)
            await runtime.calls.leave_call(job['chat_id'], close=True)
            self.db.stream_status(job['id'], 'completed')
            await self.notify(f"Трансляция #{job['id']} завершена.")
        except Exception as error:
            self.db.stream_status(job['id'], 'error', str(error))
            await self.notify(f"Ошибка трансляции #{job['id']}: {error}")
            log.exception('Scheduled stream failed')
        finally:
            if runtime:
                if handler:
                    runtime.client.remove_handler(handler, group=99)
                runtime.calls.remove_handler(on_stream)
                with suppress(Exception):
                    await runtime.client.invoke(raw.functions.phone.DiscardGroupCall(call=call))
            self.tasks.pop(job['id'], None)

    async def stop(self):
        tasks = list(self.tasks.values())
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
