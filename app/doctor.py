"""Offline dependency checks; never reads or prints credentials."""
import shutil
import sys


def main() -> int:
    failures = []
    for name in ('aiogram', 'pyrogram', 'dotenv', 'pytgcalls', 'ntgcalls'):
        try:
            __import__(name)
            print(f'{name}: OK')
        except Exception as error:
            failures.append(name)
            print(f'{name}: FAILED ({type(error).__name__})')
    for name in ('ffmpeg', 'ffprobe'):
        found = shutil.which(name)
        print(f'{name}: {"OK" if found else "MISSING"}')
        if not found:
            failures.append(name)
    return int(bool(failures))


if __name__ == '__main__':
    sys.exit(main())
