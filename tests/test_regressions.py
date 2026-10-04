import asyncio
import tempfile
import sqlite3
from contextlib import closing
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch
from types import SimpleNamespace
from pyrogram.errors import SessionPasswordNeeded
from pyrogram import Client
from app.config import Settings
from app.main import build_router
from app.main import AddChannel, AddAccount, CreatePost, ImportSession, SetCallAudio, PlanStream
from aiogram import Bot, Dispatcher
from aiogram.types import Update, Message, Chat, User
from aiogram.exceptions import TelegramBadRequest
from aiogram.methods import GetChatMember
from datetime import datetime, timezone
from app.streams import StreamScheduler, parse_start, set_join_muted
from pyrogram.errors import GroupcallNotModified
from app.channel_input import parse_channel
from pytgcalls.types import StreamEnded, Device

from telethon.errors import FloodWaitError
from pyrogram.errors import FloodWait

from app.db import Account, Database
from app.runtime import RuntimeManager
from app.telegram_account import broadcast_to_users
from app.session_import import import_session


class RegressionTests(unittest.IsolatedAsyncioTestCase):
    async def test_runtime_initializes_profile_for_real_media_uploader(self):
        # Real Pyrofork client/uploader; transport and credentials are entirely fake.
        client = Client('offline-test', api_id=1, api_hash='0' * 32, in_memory=True)
        profile = SimpleNamespace(id=123, is_premium=False)
        calls = Mock()
        calls.on_update.return_value = lambda handler: handler
        calls.start = AsyncMock()
        manager = RuntimeManager(Settings('unused', 1, None, None, 4), Mock())
        with patch.object(client, 'connect', new=AsyncMock(return_value=True)), \
             patch.object(client, 'get_me', new=AsyncMock(return_value=profile)), \
             patch.object(client, 'initialize', new=AsyncMock()), \
             patch.object(client, 'add_handler'), \
             patch('app.runtime.new_client', return_value=client), \
             patch('app.runtime.PyTgCalls', return_value=calls):
            await manager.start_account(Account(1, '+10000000000', 'unused'))
        self.assertIs(client.me, profile)
        with tempfile.TemporaryDirectory() as folder:
            media = Path(folder) / 'fixture.ogg'
            media.write_bytes(b'offline upload fixture')
            session = AsyncMock()
            with patch('pyrogram.methods.advanced.save_file.Session', return_value=session), \
                 patch.object(client.storage, 'dc_id', new=AsyncMock(return_value=2)), \
                 patch.object(client.storage, 'auth_key', new=AsyncMock(return_value=bytes(256))), \
                 patch.object(client.storage, 'test_mode', new=AsyncMock(return_value=True)):
                uploaded = await client.save_file(str(media))
            self.assertIsNotNone(uploaded)
            self.assertEqual(uploaded.parts, 1)
            session.start.assert_awaited_once()

    async def test_photo_is_sent_as_photo_and_errors_are_reported(self):
        client = AsyncMock(spec=Client)
        client.send_photo = AsyncMock(side_effect=[ValueError('media upload failed'), Mock()])
        client.send_voice = AsyncMock()
        progress = AsyncMock()
        with patch('app.telegram_account.asyncio.sleep', new=AsyncMock()), \
             self.assertLogs('app.telegram_account', level='WARNING') as logs:
            result = await broadcast_to_users(client, [123, 456], 'caption', Path('photo.jpg'), 0, progress)
        self.assertEqual(client.send_photo.await_count, 2)
        client.send_voice.assert_not_awaited()
        self.assertEqual((result.sent, result.failed), (1, 1))
        self.assertEqual(result.errors, {'ValueError: media upload failed': 1})
        self.assertIn('media upload failed', logs.output[0])
        progress.assert_awaited_once()

    async def test_already_muted_is_not_a_scheduling_failure(self):
        client = AsyncMock()
        client.invoke.side_effect = GroupcallNotModified()
        await set_join_muted(client, SimpleNamespace(id=1, access_hash=2))
        client.invoke.assert_awaited_once()

    async def test_accounts_button_escapes_every_input_state(self):
        with tempfile.TemporaryDirectory() as folder:
            db = Database(str(Path(folder) / 'db.sqlite3'))
            bot = Bot('123456:ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghi')
            dp = Dispatcher()
            dp.include_router(build_router(Settings('token', 1, None, None, 4), db, bot, AsyncMock()))
            state = dp.fsm.get_context(bot=bot, chat_id=1, user_id=1)
            for index, previous in enumerate((AddChannel.name, AddAccount.phone, AddAccount.code, AddAccount.password, CreatePost.content, ImportSession.file, SetCallAudio.file, PlanStream.date)):
                await state.set_state(previous)
                await state.update_data(old='discard')
                message = Message(message_id=index+1, date=datetime.now(timezone.utc), chat=Chat(id=1,type='private'), from_user=User(id=1,is_bot=False,first_name='Admin'), text='📋 Аккаунты')
                with patch.object(Message, 'answer', new=AsyncMock()) as answer:
                    await dp.feed_update(bot, Update(update_id=index, message=message))
                self.assertEqual(answer.await_args.args[0], 'Аккаунтов пока нет.')
                self.assertIsNone(await state.get_state())
                self.assertEqual(await state.get_data(), {})
            await bot.session.close()
            await dp.storage.close()

    async def test_channel_access_error_leaves_input_and_identifies_bot(self):
        with tempfile.TemporaryDirectory() as folder:
            db = Database(str(Path(folder) / 'db.sqlite3'))
            bot = AsyncMock()
            bot.get_chat.return_value = SimpleNamespace(id=-100123, type='channel', title='Channel')
            bot.get_me.return_value = SimpleNamespace(id=123, username='correct_bot')
            bot.get_chat_member.side_effect = TelegramBadRequest(method=GetChatMember(chat_id=-100123,user_id=123), message='member list is inaccessible')
            router = build_router(Settings('token', 1, None, None, 4), db, bot, AsyncMock())
            handler = next(h.callback for h in router.message.handlers if h.callback.__name__ == 'add_channel')
            message, state = AsyncMock(), AsyncMock()
            message.text = 'https://t.me/channelname'
            await handler(message, state)
            state.clear.assert_awaited_once()
            self.assertIn('@correct_bot', message.answer.await_args.args[0])
            self.assertEqual(db.channels(), [])

    def test_channel_links_and_private_invites(self):
        for value in ('https://t.me/ididsomethingreallybad', 't.me/ididsomethingreallybad', '@ididsomethingreallybad', 'https://t.me/s/ididsomethingreallybad/42'):
            self.assertEqual(parse_channel(value), '@ididsomethingreallybad')
        self.assertEqual(parse_channel('-100123456'), -100123456)
        with self.assertRaises(ValueError):
            parse_channel('https://t.me/+privateinvite')

    async def test_stream_waits_for_end_then_five_seconds_and_closes(self):
        db, bot, manager = AsyncMock(), AsyncMock(), AsyncMock()
        db.stream_status = Mock()
        client, calls = AsyncMock(), AsyncMock()
        calls.add_handler = Mock()
        calls.remove_handler = Mock()
        client.add_handler = Mock()
        client.remove_handler = Mock()
        client.get_me.return_value = SimpleNamespace(id=1)
        client.invoke.return_value = SimpleNamespace(participants=[], next_offset='')
        manager.get.return_value = SimpleNamespace(client=client, calls=calls)
        async def play(*args, **kwargs):
            callback = calls.add_handler.call_args.args[0]
            await callback(calls, StreamEnded(-100123, StreamEnded.Type.AUDIO, Device.MICROPHONE))
        calls.play.side_effect = play
        scheduler = StreamScheduler(db, manager, bot, 1)
        job = dict(id=1, account_id=1, chat_id=-100123, call_id=99, access_hash=123, audio_path='audio.ogg')
        with patch('app.streams.asyncio.sleep', new=AsyncMock()) as sleep:
            await scheduler.run(job)
        sleep.assert_awaited_once_with(5)
        calls.leave_call.assert_awaited_once_with(-100123, close=True)
        db.stream_status.assert_any_call(1, 'completed')

    async def test_voice_is_sent_as_voice(self):
        client = AsyncMock(spec=Client)
        client.send_voice = AsyncMock()
        client.send_photo = AsyncMock()
        with patch('app.telegram_account.asyncio.sleep', new=AsyncMock()):
            result = await broadcast_to_users(client, [123], 'caption', Path('voice.ogg'), 4, media_kind='voice')
        client.send_voice.assert_awaited_once_with(123, 'voice.ogg', caption='caption')
        client.send_photo.assert_not_awaited()
        self.assertEqual(result.sent, 1)

    def test_callers_are_separate_deduplicated_and_persistent(self):
        with tempfile.TemporaryDirectory() as folder:
            path = str(Path(folder) / 'app.sqlite3')
            db = Database(path)
            account = db.save_account('+123', 'session')
            db.upsert_recipient(account.id, 1, None, 'Writer')
            db.remember_caller(account.id, 2)
            db.remember_caller(account.id, 2)
            restored = Database(path)
            self.assertEqual(restored.caller_ids(account.id), [2])
            self.assertEqual(restored.list_recipient_ids(account.id), [1])
            restored.save_channel(-100123, 'Channel')
            self.assertEqual(restored.channels()[0]['chat_id'], -100123)
            restored.delete_account(account.id)
            self.assertEqual(restored.caller_ids(account.id), [])

    async def test_bot_login_2fa_creates_and_applies_pyrofork_session(self):
        with tempfile.TemporaryDirectory() as folder:
            db = Database(str(Path(folder) / 'app.sqlite3'))
            runtime = AsyncMock()
            router = build_router(Settings('token', 1, 123, 'hash', 4), db, AsyncMock(), runtime)
            handlers = {h.callback.__name__: h.callback for h in router.message.handlers}
            state = AsyncMock()
            state.get_data.return_value = {'phone': '+123456789', 'phone_code_hash': 'challenge'}
            message = AsyncMock()
            message.from_user = SimpleNamespace(id=1)
            message.text = '+123456789'
            client = AsyncMock()
            client.workdir = folder
            client.name = 'created'
            client.is_connected = True
            client.send_code.return_value = SimpleNamespace(phone_code_hash='challenge')
            client.sign_in.side_effect = SessionPasswordNeeded()
            client.get_me.return_value = SimpleNamespace(phone_number='123456789')
            with patch('app.main.Client', return_value=client):
                await handlers['add_account_phone'](message, state)
            message.text = '1 2 3 4 5'
            await handlers['add_account_code'](message, state)
            client.sign_in.assert_awaited_once_with(phone_number='+123456789', phone_code_hash='challenge', phone_code='12345')
            message.text = 'test-password'
            await handlers['add_account_password'](message, state)
            client.check_password.assert_awaited_once_with('test-password')
            account = db.list_accounts()[0]
            self.assertEqual(account.session_string, 'pyrofile:' + str((Path(folder) / 'created.session').resolve()))
            runtime.start_account.assert_awaited_once_with(account)
            client.disconnect.assert_awaited_once()

    def test_session_import_copies_and_rejects_telethon(self):
        with tempfile.TemporaryDirectory() as folder:
            source = Path(folder) / 'source.session'
            with closing(sqlite3.connect(source)) as connection, connection:
                connection.execute('CREATE TABLE sessions(api_id INTEGER, auth_key BLOB, user_id INTEGER, is_bot INTEGER, dc_id INTEGER, test_mode INTEGER)')
                connection.execute('INSERT INTO sessions VALUES(1, ?, 123, 0, 2, 0)', (bytes(256),))
            original = source.read_bytes()
            imported = import_session(source, Path(folder) / 'copies')
            self.assertNotEqual(source, imported)
            self.assertEqual(source.read_bytes(), original)
            with closing(sqlite3.connect(imported)) as connection:
                self.assertEqual(connection.execute('SELECT user_id FROM sessions').fetchone()[0], 123)
            other = Path(folder) / 'other.session'
            with closing(sqlite3.connect(other)) as connection, connection:
                connection.execute('CREATE TABLE sessions(dc_id INTEGER, auth_key BLOB)')
            with self.assertRaises(ValueError):
                import_session(other, Path(folder) / 'copies')

    async def test_pyrofork_flood_wait_stops(self):
        client = AsyncMock()
        client.send_message.side_effect = FloodWait(9)
        result = await broadcast_to_users(client, [1, 2], 'post', None, 0)
        self.assertEqual(result.stopped_by_flood_wait, 9)
        self.assertEqual(client.send_message.await_count, 1)

    async def test_flood_wait_stops_before_next_recipient(self):
        client = AsyncMock()
        client.send_message.side_effect = [None, FloodWaitError(None, capture=12)]
        with patch('app.telegram_account.asyncio.sleep', new=AsyncMock()):
            result = await broadcast_to_users(client, [1, 2, 3], 'post', None, 4)
        self.assertEqual(client.send_message.await_count, 2)
        self.assertEqual(result.sent, 1)
        self.assertEqual(result.stopped_by_flood_wait, 12)

    async def test_private_message_failure_does_not_abort_others(self):
        client = AsyncMock()
        client.send_message.side_effect = [RuntimeError('blocked'), None]
        with patch('app.telegram_account.asyncio.sleep', new=AsyncMock()):
            result = await broadcast_to_users(client, [1, 2], 'post', None, 4)
        self.assertEqual((result.sent, result.failed), (1, 1))

    def test_delete_account_cascades_but_preserves_other_account(self):
        with tempfile.TemporaryDirectory() as folder:
            db = Database(str(Path(folder) / 'test.sqlite3'))
            first = db.save_account('+100', 'first')
            second = db.save_account('+200', 'second')
            for account in (first, second):
                db.upsert_recipient(account.id, 123, None, 'User')
                db.set_call_audio(account.id, 'audio.wav', 'audio.wav')
            db.delete_account(first.id)
            self.assertEqual(db.recipient_count(first.id), 0)
            self.assertEqual(db.recipient_count(second.id), 1)
            self.assertIsNotNone(db.get_account(second.id))


if __name__ == '__main__':
    unittest.main()
