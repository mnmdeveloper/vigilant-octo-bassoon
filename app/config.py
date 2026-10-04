from __future__ import annotations

from dataclasses import dataclass
import os

from dotenv import load_dotenv

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


@dataclass(frozen=True)
class Settings:
    bot_token: str
    admin_id: int
    api_id: int | None
    api_hash: str | None
    send_delay_seconds: float
    database_path: str = "data/app.sqlite3"


def load_settings() -> Settings:
    load_dotenv(os.path.join(PROJECT_ROOT, '.env'), encoding='utf-8-sig')
    required = {
        "BOT_TOKEN": os.getenv("BOT_TOKEN"),
        "ADMIN_ID": os.getenv("ADMIN_ID"),
    }
    missing = [name for name, value in required.items() if not value]
    if missing:
        raise RuntimeError(f"Заполните .env: {', '.join(missing)}")

    return Settings(
        bot_token=str(required["BOT_TOKEN"]),
        admin_id=int(str(required["ADMIN_ID"])),
        api_id=int(os.environ['TELEGRAM_API_ID']) if os.getenv('TELEGRAM_API_ID') else None,
        api_hash=os.getenv('TELEGRAM_API_HASH') or None,
        send_delay_seconds=max(2.0, float(os.getenv("SEND_DELAY_SECONDS", "4"))),
    )

