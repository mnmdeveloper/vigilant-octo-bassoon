"""Interactive local configuration wizard. Secrets never enter command arguments."""
from getpass import getpass
from pathlib import Path
import json
import os
import re

ROOT = Path(__file__).resolve().parent


def ask(name, prompt, validate, optional=False, secret=False):
    while True:
        value = (getpass(prompt) if secret else input(prompt)).strip()
        if optional and not value:
            return ''
        if validate(value):
            return value
        print(f'Invalid value for {name}. Please try again.')


def main():
    target = ROOT / '.env'
    print('Telegram server setup. Values are saved only in the local .env file.')
    if target.exists() and input('Replace existing .env? Type yes to continue: ').strip() != 'yes':
        print('Existing configuration preserved.')
        return
    token = ask('BOT_TOKEN', 'BotFather token (hidden): ', lambda v: bool(re.fullmatch(r'\d+:[A-Za-z0-9_-]{20,}', v)), secret=True)
    admin = ask('ADMIN_ID', 'Your numeric Telegram user ID: ', lambda v: v.isdigit() and int(v) > 0)
    print('API keys are optional for importing an authorized Pyrofork .session.')
    api_id = ask('TELEGRAM_API_ID', 'API ID (Enter to skip): ', lambda v: v.isdigit() and int(v)>0, optional=True)
    api_hash = ask('TELEGRAM_API_HASH', 'API hash (hidden, Enter to skip): ', lambda v: bool(re.fullmatch(r'[a-fA-F0-9]{32}',v)), optional=not api_id, secret=True)
    if not api_id:
        api_hash = ''
    delay = ask('SEND_DELAY_SECONDS', 'Delay between messages in seconds (minimum 2): ', lambda v: v.isdigit() and int(v)>=2)
    config = dict(BOT_TOKEN=token, ADMIN_ID=admin, TELEGRAM_API_ID=api_id,
                  TELEGRAM_API_HASH=api_hash, SEND_DELAY_SECONDS=delay)
    descriptor = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, 'w', encoding='utf-8') as file:
        for name, value in config.items():
            file.write(f'{name}={json.dumps(value)}\n')
    if os.name != 'nt':
        target.chmod(0o600)
    (ROOT / 'data').mkdir(exist_ok=True)
    print('Configuration saved. With Docker: docker compose up -d --build')
    print('Without Docker: .venv/bin/python -m app.main (Windows: start.cmd)')


if __name__ == '__main__':
    try:
        main()
    except (KeyboardInterrupt, EOFError):
        print('\nSetup cancelled.')
        raise SystemExit(1)
