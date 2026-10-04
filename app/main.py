from __future__ import annotations

import asyncio
from contextlib import suppress
from pathlib import Path
import logging
import shutil
import tempfile
import os
from uuid import uuid4

from aiogram import Bot, Dispatcher, F, Router
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from aiogram.exceptions import TelegramBadRequest, TelegramRetryAfter
from pyrogram import Client, enums
from pyrogram.errors import PhoneCodeInvalid, PhoneCodeExpired, SessionPasswordNeeded

from app.config import PROJECT_ROOT, Settings, load_settings
from app.db import Account, Database
from app.keyboards import account_picker, confirm_broadcast, main_menu, MENU_TEXTS
from app.runtime import RuntimeManager
from app.telegram_account import (
    broadcast_to_users,
    channel_summary,
    find_opted_in_users,
    new_client,
)
from app.media import validate_audio
from app.session_import import import_session
from app.streams import StreamScheduler, parse_start
from app.channel_input import parse_channel

log = logging.getLogger(__name__)


class AddAccount(StatesGroup):
    phone = State()
    code = State()
    password = State()


class CreatePost(StatesGroup):
    content = State()
    account = State()
    confirm = State()
    audience = State()


class AddChannel(StatesGroup):
    name = State()


class PlanStream(StatesGroup):
    account = State()
    audio = State()
    channel = State()
    date = State()
    confirm = State()


class SetCallAudio(StatesGroup):
    file = State()


class ImportSession(StatesGroup):
    file = State()


def call_settings_keyboard(account_id: int, enabled: bool) -> InlineKeyboardMarkup:
    toggle_text = "⏸ Выключить автоответ" if enabled else "▶️ Включить автоответ"
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🎵 Загрузить аудио", callback_data=f"callaudio:{account_id}")],
            [InlineKeyboardButton(text=toggle_text, callback_data=f"calltoggle:{account_id}")],
            [InlineKeyboardButton(text="Закрыть", callback_data="cancel")],
        ]
    )


def account_info_keyboard(account_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🗑 Удалить аккаунт", callback_data=f"deleteask:{account_id}")],
        ]
    )


def delete_confirm_keyboard(account_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="Да, удалить", callback_data=f"deleteconfirm:{account_id}")],
            [InlineKeyboardButton(text="Отмена", callback_data="cancel")],
        ]
    )


def build_router(
    settings: Settings, db: Database, bot: Bot, runtimes: RuntimeManager, scheduler=None
) -> Router:
    router = Router()
    # Apply access checks before state-specific handlers as well.
    router.message.filter(F.from_user.id == settings.admin_id, F.chat.type == 'private')
    router.callback_query.filter(F.from_user.id == settings.admin_id)
    auth_clients: dict[int, object] = {}
    broadcast_tasks: dict[int, asyncio.Task] = {}

    @router.message.outer_middleware()
    async def reset_navigation(handler, event, data):
        text = event.text or ''
        if text in MENU_TEXTS or text.split('@')[0] in ('/start', '/cancel'):
            await clear_login(event.from_user.id)
            await data['state'].clear()
            data['raw_state'] = None
        return await handler(event, data)

    def is_admin(user_id: int) -> bool:
        return user_id == settings.admin_id

    @router.message(F.text == '📅 Запланировать эфир')
    async def plan_stream(message: Message, state: FSMContext):
        accounts = [a for a in db.list_accounts() if a.session_string.startswith('pyrofile:')]
        if not accounts or not db.channels():
            await message.answer('Сначала импортируйте или создайте сессию аккаунта и добавьте канал. Аккаунту нужны права управления трансляциями.')
            return
        await state.set_state(PlanStream.account)
        await message.answer('Выберите аккаунт, затем загрузите аудио для эфира.', reply_markup=account_picker(accounts, 'streamaccount'))

    @router.callback_query(PlanStream.account, F.data.startswith('streamaccount:'))
    async def stream_account(callback: CallbackQuery, state: FSMContext):
        account_id = int(callback.data.split(':')[1])
        audio = db.get_call_settings(account_id)
        await state.update_data(stream_account_id=account_id, saved_stream_audio=audio.audio_path)
        await state.set_state(PlanStream.audio)
        await callback.answer()
        await callback.message.answer('Пришлите аудиофайл, голосовое или аудио документом. Можно написать «Сохранённое», чтобы использовать аудио из настроек звонков. /cancel — отмена.')

    @router.message(PlanStream.audio)
    async def stream_audio(message: Message, state: FSMContext):
        data = await state.get_data()
        if message.text and message.text.strip().lower() == 'сохранённое':
            path = data.get('saved_stream_audio')
            if not path or not Path(path).is_file():
                await message.answer('Сохранённого аудио нет. Пришлите файл.')
                return
            name = db.get_call_settings(data['stream_account_id']).audio_name or Path(path).name
        else:
            file = message.audio or message.voice or message.document
            if file is None:
                await message.answer('Пришлите аудиофайл или голосовое сообщение.')
                return
            name = getattr(file, 'file_name', None) or 'voice.ogg'
            folder = Path('data/stream_audio')
            folder.mkdir(parents=True, exist_ok=True)
            destination = folder / (uuid4().hex + (Path(name).suffix or '.audio'))
            try:
                await bot.download(file.file_id, destination=destination)
                await validate_audio(destination)
            except Exception as error:
                destination.unlink(missing_ok=True)
                await message.answer(f'Не удалось прочитать аудио: {error}')
                return
            path = str(destination.resolve())
        await state.update_data(stream_audio=path, stream_audio_name=name)
        await state.set_state(PlanStream.channel)
        rows = [[InlineKeyboardButton(text=r['title'], callback_data=f"streamchannel:{r['chat_id']}")] for r in db.channels()]
        await message.answer(f'Аудио: {name}. Выберите канал:', reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))

    @router.callback_query(PlanStream.channel, F.data.startswith('streamchannel:'))
    async def stream_channel(callback: CallbackQuery, state: FSMContext):
        chat_id = int(callback.data.split(':')[1])
        channel = next((r for r in db.channels() if r['chat_id'] == chat_id), None)
        if channel is None:
            await callback.answer('Канал не найден', show_alert=True)
            return
        await state.update_data(stream_channel_id=chat_id, stream_channel_title=channel['title'])
        await state.set_state(PlanStream.date)
        await callback.answer()
        await callback.message.answer('Введите дату: ДД.ММ.ГГГГ ЧЧ:ММ. Время московское — МСК (UTC+3). Можно планировать до 8 дней вперёд. ПК и программа должны работать в момент эфира.')

    @router.message(PlanStream.date, F.text)
    async def stream_date(message: Message, state: FSMContext):
        try:
            timestamp = parse_start(message.text)
        except ValueError as error:
            await message.answer(f'Некорректная дата: {error}')
            return
        await state.update_data(stream_start=timestamp)
        await state.set_state(PlanStream.confirm)
        data = await state.get_data()
        await message.answer(f"Создать запланированную трансляцию в «{data['stream_channel_title']}» на {message.text} МСК (UTC+3)?\nАудио: {data['stream_audio_name']}\nУчастники входят с выключенным микрофоном; другие администраторы могут включить его обратно.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text='✅ Запланировать', callback_data='streamconfirm')],
                [InlineKeyboardButton(text='Отмена', callback_data='cancel')]]))

    @router.callback_query(PlanStream.confirm, F.data == 'streamconfirm')
    async def stream_confirm(callback: CallbackQuery, state: FSMContext):
        data = await state.get_data()
        await state.clear()
        await callback.answer()
        try:
            if scheduler is None:
                raise RuntimeError('Планировщик не запущен')
            job_id = await scheduler.schedule(data['stream_account_id'], data['stream_channel_id'], data['stream_start'], data['stream_audio'])
            await callback.message.answer(f'Эфир #{job_id} создан в Telegram и запланирован. После аудио — пауза 5 секунд и завершение.', reply_markup=main_menu())
        except Exception as error:
            await callback.message.answer(f'Не удалось запланировать эфир: {error}', reply_markup=main_menu())

    @router.message(F.text == '➕ Добавить канал')
    async def request_channel(message: Message, state: FSMContext):
        await state.set_state(AddChannel.name)
        await message.answer('Добавьте этого бота администратором канала, затем пришлите @username или числовой ID канала (-100...).')

    @router.message(AddChannel.name, F.text)
    async def add_channel(message: Message, state: FSMContext):
        try:
            name = parse_channel(message.text)
            chat = await bot.get_chat(name)
            if chat.type != 'channel':
                raise ValueError('Нужен канал')
            me = await bot.get_me()
            member = await bot.get_chat_member(chat.id, me.id)
            if member.status not in ('administrator', 'creator'):
                raise ValueError('Бот должен быть администратором канала')
            db.save_channel(chat.id, chat.title or str(chat.id))
            await state.clear()
            await message.answer(f'Канал «{chat.title}» добавлен.', reply_markup=main_menu())
        except TelegramBadRequest as error:
            await state.clear()
            me = await bot.get_me()
            detail = str(error).lower()
            if 'member list is inaccessible' in detail or 'chat_admin_required' in detail:
                reason = f'Telegram не дал этому боту проверить права в канале. Добавьте @{me.username} администратором именно этого канала и повторите добавление.'
            elif 'chat not found' in detail:
                reason = f'Канал недоступен боту @{me.username}. Проверьте ссылку и добавьте бота администратором. Для приватного канала используйте ID (-100...).'
            else:
                reason = f'Telegram отказал при проверке канала: {error.message}'
            await message.answer(reason, reply_markup=main_menu())
        except ValueError as error:
            await message.answer(f'{error}\nПришлите канал ещё раз или выберите пункт меню. /cancel — отмена.')
        except Exception:
            await state.clear()
            log.exception('Channel registration failed')
            await message.answer('Не удалось проверить канал. Попробуйте добавить его заново.', reply_markup=main_menu())

    async def clear_login(user_id: int) -> None:
        client = auth_clients.pop(user_id, None)
        if client:
            if client.is_connected:
                await client.disconnect()
            path = Path(client.workdir) / f'{client.name}.session'
            path.unlink(missing_ok=True)

    @router.message(F.text == '📂 Импорт .session')
    async def request_session(message: Message, state: FSMContext):
        await clear_login(message.from_user.id)
        await state.set_state(ImportSession.file)
        await message.answer('Введите полный путь к готовому Pyrogram/Pyrofork .session на этом ПК или отправьте файл документом. Исходный файл будет скопирован. /cancel — отмена')

    @router.message(ImportSession.file)
    async def receive_session(message: Message, state: FSMContext):
        temporary = None
        destination = None
        client = None
        try:
            if message.document:
                if not (message.document.file_name or '').endswith('.session'):
                    raise ValueError('Нужен файл .session')
                with tempfile.NamedTemporaryFile(suffix='.session', delete=False) as file:
                    temporary = Path(file.name)
                await bot.download(message.document.file_id, destination=temporary)
                source = temporary
                with suppress(Exception):
                    await message.delete()
            elif message.text:
                source = Path(message.text.strip().strip('"'))
            else:
                raise ValueError('Пришлите файл или путь к нему')
            destination = import_session(source, Path('data/sessions'))
            marker = 'pyrofile:' + str(destination)
            client = new_client(settings, marker)
            if not await client.connect():
                raise ValueError('Сессия не авторизована')
            me = await client.get_me()
            if me.is_bot:
                raise ValueError('Нужна пользовательская сессия')
            phone = '+' + me.phone_number if me.phone_number else f'User {me.id}'
            await client.disconnect()
            client = None
            account = db.save_account(phone, marker)
            destination = None
            await state.clear()
            await runtimes.stop_account(account.id)
            await runtimes.start_account(account)
            await message.answer(f'Аккаунт {phone} подключён через .session. API-ключи не потребовались.', reply_markup=main_menu())
        except Exception as error:
            await message.answer(f'Не удалось подключить сессию: {error}')
        finally:
            if client and client.is_connected:
                await client.disconnect()
            if temporary:
                temporary.unlink(missing_ok=True)
            if destination:
                destination.unlink(missing_ok=True)

    @router.message(CommandStart())
    async def start(message: Message, state: FSMContext) -> None:
        if not is_admin(message.from_user.id):
            await message.answer("Это закрытая панель управления.")
            return
        await clear_login(message.from_user.id)
        await state.clear()
        await message.answer("Панель управления запущена.", reply_markup=main_menu())

    @router.message(Command("cancel"))
    async def cancel_command(message: Message, state: FSMContext) -> None:
        if not is_admin(message.from_user.id):
            return
        await clear_login(message.from_user.id)
        await state.clear()
        await message.answer("Действие отменено.", reply_markup=main_menu())

    @router.callback_query(F.data == "cancel")
    async def cancel_callback(callback: CallbackQuery, state: FSMContext) -> None:
        if not is_admin(callback.from_user.id):
            return
        await clear_login(callback.from_user.id)
        await state.clear()
        await callback.answer("Отменено")
        if callback.message:
            await callback.message.answer("Действие отменено.", reply_markup=main_menu())

    @router.message(F.text.in_({'➕ Добавить аккаунт', '🔐 Создать сессию'}))
    async def add_account_start(message: Message, state: FSMContext) -> None:
        if not is_admin(message.from_user.id):
            return
        if not settings.api_id or not settings.api_hash:
            await message.answer('Без API-ключей используйте кнопку «📂 Импорт .session». Вход по номеру требует API-ключи.')
            return
        await clear_login(message.from_user.id)
        await state.clear()
        await state.set_state(AddAccount.phone)
        await message.answer(
            "Введите номер в международном формате, например +31612345678. /cancel — отмена"
        )

    @router.message(AddAccount.phone, F.text)
    async def add_account_phone(message: Message, state: FSMContext) -> None:
        phone = message.text.strip().replace(" ", "")
        if not phone.startswith('+') or not phone[1:].isdigit() or not 7 <= len(phone[1:]) <= 15:
            await message.answer('Введите номер с + и кодом страны, без букв.')
            return
        await clear_login(message.from_user.id)
        session_dir = Path('data/sessions').resolve()
        session_dir.mkdir(parents=True, exist_ok=True)
        client = Client(uuid4().hex, workdir=str(session_dir), api_id=settings.api_id,
                        api_hash=settings.api_hash, no_updates=False, sleep_threshold=0,
                        parse_mode=enums.ParseMode.DISABLED)
        auth_clients[message.from_user.id] = client
        try:
            await client.connect()
            sent = await client.send_code(phone)
        except Exception as error:
            await clear_login(message.from_user.id)
            await message.answer(f"Не удалось запросить код: {error}")
            return
        await state.update_data(phone=phone, phone_code_hash=sent.phone_code_hash)
        await state.set_state(AddAccount.code)
        await message.answer('Введите код вручную, разделив цифры пробелами: например 1 2 3 4 5. Не пересылайте сообщение Telegram с кодом. После чтения сообщение удаляется.')

    @router.message(AddAccount.code, F.text)
    async def add_account_code(message: Message, state: FSMContext) -> None:
        client = auth_clients.get(message.from_user.id)
        if client is None:
            await state.clear()
            await message.answer("Сессия входа истекла. Начните заново.")
            return
        code = "".join(character for character in message.text if character.isdigit())
        with suppress(Exception):
            await message.delete()
        data = await state.get_data()
        try:
            await client.sign_in(phone_number=data['phone'], phone_code_hash=data['phone_code_hash'], phone_code=code)
        except SessionPasswordNeeded:
            await state.set_state(AddAccount.password)
            await message.answer("Введите облачный 2FA-пароль. Сообщение будет удалено.")
            return
        except PhoneCodeInvalid:
            await message.answer("Неверный код. Попробуйте ещё раз.")
            return
        except PhoneCodeExpired:
            await clear_login(message.from_user.id)
            await state.set_state(AddAccount.phone)
            await message.answer('Код истёк. Введите номер ещё раз, чтобы запросить новый.')
            return
        except Exception as error:
            await message.answer(f"Ошибка входа: {error}")
            return
        await finish_login(message, state, client, data["phone"])

    @router.message(AddAccount.password, F.text)
    async def add_account_password(message: Message, state: FSMContext) -> None:
        client = auth_clients.get(message.from_user.id)
        if client is None:
            await state.clear()
            await message.answer("Сессия входа истекла. Начните заново.")
            return
        password = message.text
        with suppress(Exception):
            await message.delete()
        data = await state.get_data()
        try:
            await client.check_password(password)
        except Exception as error:
            await message.answer(f"Ошибка пароля: {error}")
            return
        await finish_login(message, state, client, data["phone"])

    async def finish_login(message: Message, state: FSMContext, client, phone: str) -> None:
        me = await client.get_me()
        phone = '+' + me.phone_number if me.phone_number else phone
        path = (Path(client.workdir) / f'{client.name}.session').resolve()
        await client.disconnect()
        account = db.save_account(phone, 'pyrofile:' + str(path))
        auth_clients.pop(message.from_user.id, None)
        await state.clear()
        try:
            await runtimes.stop_account(account.id)
            await runtimes.start_account(account)
            suffix = " Аккаунт подключён к фоновому сервису."
        except Exception as error:
            log.exception("Could not start newly added account")
            suffix = f" Фоновый сервис не запустился: {error}"
        await message.answer(f'Сессия {phone} создана и сохранена локально.{suffix}', reply_markup=main_menu())

    @router.message(F.text == "📋 Аккаунты")
    async def list_accounts(message: Message) -> None:
        if not is_admin(message.from_user.id):
            return
        accounts = db.list_accounts()
        if not accounts:
            await message.answer("Аккаунтов пока нет.")
            return
        for account in accounts:
            calls = db.get_call_settings(account.id)
            recipients = db.recipient_count(account.id)
            await message.answer(
                f"{account.phone}\nПолучателей: {recipients}\nАвтоответ на звонки: "
                f"{'включён' if calls.enabled else 'выключен'}",
                reply_markup=account_info_keyboard(account.id),
            )

    @router.callback_query(F.data.startswith("deleteask:"))
    async def delete_account_ask(callback: CallbackQuery) -> None:
        if not is_admin(callback.from_user.id):
            return
        account_id = int(callback.data.split(":", 1)[1])
        account = db.get_account(account_id)
        if account is None:
            await callback.answer("Аккаунт не найден", show_alert=True)
            return
        await callback.answer()
        await callback.message.answer(
            f"Удалить {account.phone}, его сессию, получателей и настройки?",
            reply_markup=delete_confirm_keyboard(account_id),
        )

    @router.callback_query(F.data.startswith("deleteconfirm:"))
    async def delete_account_confirm(callback: CallbackQuery) -> None:
        if not is_admin(callback.from_user.id):
            return
        account_id = int(callback.data.split(":", 1)[1])
        account = db.get_account(account_id)
        if account is None:
            await callback.answer("Уже удалён", show_alert=True)
            return
        if any(job['account_id'] == account_id for job in db.stream_jobs()):
            await callback.answer('У аккаунта есть запланированный или активный эфир', show_alert=True)
            return
        if account_id in broadcast_tasks and not broadcast_tasks[account_id].done():
            await callback.answer('Сначала дождитесь окончания отправки', show_alert=True)
            return
        await runtimes.stop_account(account_id)
        db.delete_account(account_id)
        await callback.answer("Удалено")
        await callback.message.answer(f"Аккаунт {account.phone} удалён из приложения.")

    @router.message(F.text == "📣 Каналы аккаунта")
    async def choose_channel_account(message: Message) -> None:
        if not is_admin(message.from_user.id):
            return
        accounts = db.list_accounts()
        if not accounts:
            await message.answer("Сначала добавьте аккаунт.")
            return
        await message.answer("Выберите аккаунт:", reply_markup=account_picker(accounts, "channels"))

    @router.callback_query(F.data.startswith("channels:"))
    async def show_channels(callback: CallbackQuery) -> None:
        if not is_admin(callback.from_user.id):
            return
        account_id = int(callback.data.split(":", 1)[1])
        await callback.answer()
        status = await callback.message.answer("Проверяю список…")
        try:
            runtime = await runtimes.get(account_id)
            summary = await channel_summary(runtime.client)
            await status.delete()
            for start_at in range(0, len(summary), 3900):
                await callback.message.answer(summary[start_at : start_at + 3900])
        except Exception as error:
            await status.edit_text(f"Ошибка: {error}")

    @router.message(F.text == "☎️ Настроить звонки")
    async def choose_call_account(message: Message) -> None:
        if not is_admin(message.from_user.id):
            return
        accounts = db.list_accounts()
        if not accounts:
            await message.answer("Сначала добавьте аккаунт.")
            return
        await message.answer("Выберите аккаунт:", reply_markup=account_picker(accounts, "callcfg"))

    @router.callback_query(F.data.startswith("callcfg:"))
    async def show_call_settings(callback: CallbackQuery) -> None:
        if not is_admin(callback.from_user.id):
            return
        account_id = int(callback.data.split(":", 1)[1])
        account = db.get_account(account_id)
        if account is None:
            await callback.answer("Аккаунт не найден", show_alert=True)
            return
        config = db.get_call_settings(account_id)
        ffmpeg_status = "найден" if shutil.which("ffmpeg") and shutil.which("ffprobe") else "не найден"
        await callback.answer()
        await callback.message.answer(
            f"Звонки для {account.phone}\n"
            f"Аудио: {config.audio_name or 'не загружено'}\n"
            f"Автоответ: {'включён' if config.enabled else 'выключен'}\n"
            f"FFmpeg: {ffmpeg_status}",
            reply_markup=call_settings_keyboard(account_id, config.enabled),
        )

    @router.callback_query(F.data.startswith("callaudio:"))
    async def request_call_audio(callback: CallbackQuery, state: FSMContext) -> None:
        if not is_admin(callback.from_user.id):
            return
        account_id = int(callback.data.split(":", 1)[1])
        if db.get_account(account_id) is None:
            await callback.answer("Аккаунт не найден", show_alert=True)
            return
        await state.set_state(SetCallAudio.file)
        await state.update_data(account_id=account_id)
        await callback.answer()
        await callback.message.answer(
            "Пришлите аудиофайл, voice-сообщение или документ с аудио. После загрузки отдельно включите автоответ."
        )

    @router.message(SetCallAudio.file)
    async def receive_call_audio(message: Message, state: FSMContext) -> None:
        if not is_admin(message.from_user.id):
            return
        telegram_file = message.audio or message.voice or message.document
        if telegram_file is None:
            await message.answer("Нужен аудиофайл, voice-сообщение или аудиодокумент.")
            return
        mime_type = getattr(telegram_file, "mime_type", "") or ""
        original_name = getattr(telegram_file, "file_name", None) or "voice.ogg"
        if message.document and not mime_type.startswith("audio/"):
            await message.answer("Этот документ не распознан как аудио.")
            return
        data = await state.get_data()
        account_id = int(data["account_id"])
        suffix = Path(original_name).suffix.lower() or ".audio"
        audio_dir = Path("data/audio")
        audio_dir.mkdir(parents=True, exist_ok=True)
        destination = audio_dir / f"{account_id}-{uuid4().hex}{suffix}"
        try:
            await bot.download(telegram_file.file_id, destination=destination)
            await validate_audio(destination)
            db.set_call_audio(account_id, str(destination.resolve()), original_name)
        except Exception as error:
            destination.unlink(missing_ok=True)
            await message.answer(f"Не удалось сохранить файл: {error}")
            return
        await state.clear()
        await message.answer(
            f"Аудио «{original_name}» сохранено. Теперь включите автоответ в меню звонков.",
            reply_markup=main_menu(),
        )

    @router.callback_query(F.data.startswith("calltoggle:"))
    async def toggle_calls(callback: CallbackQuery) -> None:
        if not is_admin(callback.from_user.id):
            return
        account_id = int(callback.data.split(":", 1)[1])
        if db.get_account(account_id) is None:
            await callback.answer('Аккаунт удалён', show_alert=True)
            return
        config = db.get_call_settings(account_id)
        if not config.enabled and (not config.audio_path or not Path(config.audio_path).is_file()):
            await callback.answer("Сначала загрузите аудио", show_alert=True)
            return
        if not config.enabled and (not shutil.which("ffmpeg") or not shutil.which("ffprobe")):
            await callback.answer("FFmpeg не найден. Запустите install.cmd ещё раз.", show_alert=True)
            return
        db.set_calls_enabled(account_id, not config.enabled)
        await callback.answer("Настройка изменена")
        updated = db.get_call_settings(account_id)
        await callback.message.answer(
            f"Автоответ на звонки {'включён' if updated.enabled else 'выключен'}.",
            reply_markup=call_settings_keyboard(account_id, updated.enabled),
        )

    @router.message(F.text == "✍️ Создать пост")
    async def create_post(message: Message, state: FSMContext) -> None:
        if not is_admin(message.from_user.id):
            return
        if not db.list_accounts():
            await message.answer("Сначала добавьте аккаунт.")
            return
        await state.set_state(CreatePost.content)
        await message.answer('Пришлите текст, картинку с подписью или голосовое сообщение. /cancel — отмена')

    @router.message(CreatePost.content, F.text | F.photo | F.voice)
    async def receive_post(message: Message, state: FSMContext) -> None:
        text = message.caption if message.photo or message.voice else message.text
        photo_file_id = message.photo[-1].file_id if message.photo else None
        await state.update_data(text=text or '', photo_file_id=photo_file_id,
                                voice_file_id=message.voice.file_id if message.voice else None)
        await state.set_state(CreatePost.account)
        await message.answer(
            'Пост сохранён. Выберите аккаунт отправителя:',
            reply_markup=account_picker(db.list_accounts(), "post_account"),
        )

    @router.callback_query(CreatePost.account, F.data.startswith("post_account:"))
    async def choose_post_account(callback: CallbackQuery, state: FSMContext) -> None:
        if not is_admin(callback.from_user.id):
            return
        account_id = int(callback.data.split(":", 1)[1])
        account = db.get_account(account_id)
        if account is None:
            await callback.answer("Аккаунт не найден", show_alert=True)
            return
        if account_id in broadcast_tasks and not broadcast_tasks[account_id].done():
            await callback.answer("Для этого аккаунта уже идёт отправка", show_alert=True)
            return
        await state.update_data(account_id=account_id)
        await state.set_state(CreatePost.audience)
        await callback.answer()
        rows = [[InlineKeyboardButton(text=title, callback_data='aud:' + key)] for key, title in (
            ('messages', '💬 Писавшие аккаунту'), ('calls', '☎️ Звонившие аккаунту'),
            ('both', '💬☎️ Писавшие или звонившие'))]
        rows += [[InlineKeyboardButton(text='📣 ' + row['title'], callback_data=f"aud:channel:{row['chat_id']}")] for row in db.channels()]
        await callback.message.answer(
            'Выберите аудиторию. Для канала проверяется подписка среди писавших и звонивших этому аккаунту.',
            reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
        )

    @router.callback_query(CreatePost.audience, F.data.startswith('aud:'))
    async def choose_audience(callback: CallbackQuery, state: FSMContext):
        audience = callback.data.removeprefix('aud:')
        if audience not in ('messages', 'calls', 'both') and audience not in {f"channel:{r['chat_id']}" for r in db.channels()}:
            await callback.answer('Аудитория недоступна', show_alert=True)
            return
        await state.update_data(audience=audience)
        data = await state.get_data()
        await state.set_state(CreatePost.confirm)
        await callback.answer()
        await callback.message.answer('Подтвердите отправку выбранной аудитории.', reply_markup=confirm_broadcast(data['account_id']))

    @router.callback_query(CreatePost.confirm, F.data.startswith("confirm:"))
    async def confirm_post(callback: CallbackQuery, state: FSMContext) -> None:
        if not is_admin(callback.from_user.id):
            return
        account_id = int(callback.data.split(":", 1)[1])
        data = await state.get_data()
        if data.get("account_id") != account_id:
            await callback.answer("Черновик устарел", show_alert=True)
            return
        account = db.get_account(account_id)
        if account is None:
            await callback.answer("Аккаунт не найден", show_alert=True)
            return
        if account_id in broadcast_tasks and not broadcast_tasks[account_id].done():
            await callback.answer("Отправка уже идёт", show_alert=True)
            return
        await state.clear()
        await callback.answer()
        status = await callback.message.answer("Ищу подходящие входящие диалоги…")
        task = asyncio.create_task(run_broadcast(status, account, data))
        broadcast_tasks[account_id] = task

    async def run_broadcast(status: Message, account: Account, data: dict) -> None:
        media_path: Path | None = None
        broadcast_id = db.create_broadcast(account.id)
        result = None
        try:
            runtime = await runtimes.get(account.id)

            def remember(entity) -> None:
                name = " ".join(
                    part
                    for part in (
                        getattr(entity, "first_name", None),
                        getattr(entity, "last_name", None),
                    )
                    if part
                ) or str(entity.id)
                db.upsert_recipient(
                    account.id, entity.id, getattr(entity, "username", None), name
                )

            audience = data.get('audience', 'messages')
            if audience != 'calls':
                await find_opted_in_users(runtime.client, on_found=remember)
            messages = db.list_recipient_ids(account.id)
            callers = db.caller_ids(account.id)
            recipients = messages if audience == 'messages' else callers if audience == 'calls' else list(dict.fromkeys(messages + callers))
            if audience.startswith('channel:'):
                channel_id = int(audience.split(':')[1])
                me = await bot.get_me()
                admin = await bot.get_chat_member(channel_id, me.id)
                if admin.status not in ('administrator', 'creator'):
                    raise RuntimeError('Бот больше не администратор выбранного канала')
                selected = []
                for user_id in recipients:
                    try:
                        member = await bot.get_chat_member(channel_id, user_id)
                    except TelegramRetryAfter as error:
                        raise RuntimeError(f'Проверка подписки ограничена Telegram. Повторите через {error.retry_after} сек.') from error
                    except TelegramBadRequest:
                        continue
                    if member.status in ('member', 'administrator', 'creator') or (member.status == 'restricted' and member.is_member):
                        selected.append(user_id)
                    await asyncio.sleep(0.1)
                recipients = selected
            media_kind = 'voice' if data.get('voice_file_id') else 'photo'
            file_id = data.get('voice_file_id') or data.get('photo_file_id')
            if file_id:
                temp = tempfile.NamedTemporaryFile(prefix='tg-promo-', suffix='.ogg' if media_kind == 'voice' else '.jpg', delete=False)
                temp.close()
                media_path = Path(temp.name)
                await bot.download(file_id, destination=media_path)

            async def progress(current) -> None:
                with suppress(Exception):
                    await status.edit_text(
                        f"Отправка: {current.sent + current.failed}/{current.eligible}; "
                        f"успешно {current.sent}, ошибок {current.failed}."
                    )

            result = await broadcast_to_users(
                runtime.client,
                recipients,
                data.get("text"),
                media_path,
                settings.send_delay_seconds,
                progress,
                media_kind=media_kind,
            )
            final_status = "flood_wait" if result.stopped_by_flood_wait is not None else "completed"
            db.finish_broadcast(
                broadcast_id, result.eligible, result.sent, result.failed, final_status
            )
            details = (
                f"Готово. Получателей: {result.eligible}; отправлено: {result.sent}; "
                f"ошибок: {result.failed}."
            )
            if result.stopped_by_flood_wait is not None:
                details += (
                    f" Telegram потребовал паузу {result.stopped_by_flood_wait} сек.; "
                    "текущая отправка остановлена."
                )
            await status.edit_text(details)
        except Exception as error:
            if result is None:
                db.finish_broadcast(broadcast_id, 0, 0, 1, "error")
            await status.edit_text(f"Отправка завершилась ошибкой: {error}")
            log.exception("Broadcast failed")
        finally:
            broadcast_tasks.pop(account.id, None)
            if media_path:
                media_path.unlink(missing_ok=True)

    @router.message(F.text == "📊 История")
    async def broadcast_history(message: Message) -> None:
        if not is_admin(message.from_user.id):
            return
        rows = db.recent_broadcasts()
        if not rows:
            await message.answer("Отправок пока не было.")
            return
        labels = {
            "running": "идёт",
            "completed": "готово",
            "flood_wait": "остановлено FloodWait",
            "error": "ошибка",
        }
        lines = [
            f"#{row['id']} {row['phone']} — {labels.get(row['status'], row['status'])}; "
            f"{row['sent']}/{row['eligible']}, ошибок {row['failed']}"
            for row in rows
        ]
        await message.answer("\n".join(lines))

    # Navigation must win over broad state-specific text/document handlers.
    navigation = {'start', 'cancel_command', 'request_channel', 'request_session',
                  'add_account_start', 'list_accounts', 'choose_channel_account',
                  'choose_call_account', 'create_post', 'broadcast_history', 'plan_stream'}
    router.message.handlers.sort(key=lambda h: h.callback.__name__ not in navigation)
    return router


async def main() -> None:
    os.chdir(PROJECT_ROOT)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    settings = load_settings()
    db = Database(settings.database_path)
    bot = Bot(settings.bot_token)
    # This local application receives updates through polling.
    # Preserve queued updates while removing a previous webhook registration.
    await bot.delete_webhook(drop_pending_updates=False)
    runtimes = RuntimeManager(settings, db)
    await runtimes.start_all()
    dispatcher = Dispatcher()
    scheduler = StreamScheduler(db, runtimes, bot, settings.admin_id)
    dispatcher.include_router(build_router(settings, db, bot, runtimes, scheduler))
    scheduler_task = asyncio.create_task(scheduler.loop())
    try:
        await dispatcher.start_polling(bot)
    finally:
        scheduler_task.cancel()
        with suppress(asyncio.CancelledError):
            await scheduler_task
        await scheduler.stop()
        await runtimes.stop_all()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
    except (RuntimeError, ValueError) as error:
        print(f'Не удалось запустить приложение: {error}')
        raise SystemExit(1)
