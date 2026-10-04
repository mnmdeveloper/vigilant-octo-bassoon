from urllib.parse import urlparse
import re


def parse_channel(value: str) -> int | str:
    value = value.strip()
    if re.fullmatch(r'-?\d+', value):
        return int(value)
    if value.startswith(('t.me/', 'telegram.me/')):
        value = 'https://' + value
    if '://' in value:
        url = urlparse(value)
        if url.hostname not in ('t.me', 'www.t.me', 'telegram.me', 'www.telegram.me'):
            raise ValueError('Нужна ссылка t.me, @username или числовой ID канала')
        parts = url.path.strip('/').split('/')
        if parts[0] in ('c', 'joinchat') or parts[0].startswith('+'):
            raise ValueError('Для приватного канала используйте его ID (-100...). Ссылка-приглашение не является ID канала. Бот должен быть администратором.')
        if parts[0] == 's':
            parts = parts[1:]
        value = parts[0] if parts else ''
    value = value.removeprefix('@')
    if not re.fullmatch(r'[A-Za-z][A-Za-z0-9_]{3,31}', value):
        raise ValueError('Введите ссылку на публичный канал, @username или ID (-100...)')
    return '@' + value
