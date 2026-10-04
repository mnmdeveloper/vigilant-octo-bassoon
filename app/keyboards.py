from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, KeyboardButton, ReplyKeyboardMarkup

from app.db import Account

MENU_TEXTS = {
    '📂 Импорт .session', '🔐 Создать сессию', '➕ Добавить аккаунт',
    '📋 Аккаунты', '📣 Каналы аккаунта', '✍️ Создать пост',
    '☎️ Настроить звонки', '📊 История', '➕ Добавить канал',
    '📅 Запланировать эфир',
}


def main_menu() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="📂 Импорт .session")],
            [KeyboardButton(text="🔐 Создать сессию"), KeyboardButton(text="📋 Аккаунты")],
            [KeyboardButton(text="📣 Каналы аккаунта"), KeyboardButton(text="✍️ Создать пост")],
            [KeyboardButton(text="☎️ Настроить звонки"), KeyboardButton(text="📊 История")],
            [KeyboardButton(text="➕ Добавить канал")],
            [KeyboardButton(text="📅 Запланировать эфир")],
        ],
        resize_keyboard=True,
    )


def account_picker(accounts: list[Account], prefix: str) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(text=account.phone, callback_data=f"{prefix}:{account.id}")]
        for account in accounts
    ]
    rows.append([InlineKeyboardButton(text="Отмена", callback_data="cancel")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def confirm_broadcast(account_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="✅ Начать отправку", callback_data=f"confirm:{account_id}")],
            [InlineKeyboardButton(text="Отмена", callback_data="cancel")],
        ]
    )

