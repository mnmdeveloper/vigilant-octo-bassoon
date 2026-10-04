import asyncio
import json
from pathlib import Path


async def validate_audio(path: Path) -> None:
    process = await asyncio.create_subprocess_exec(
        'ffprobe', '-v', 'error', '-show_streams', '-of', 'json', str(path),
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, _ = await asyncio.wait_for(process.communicate(), timeout=30)
    except asyncio.TimeoutError:
        process.kill()
        await process.wait()
        raise RuntimeError('Проверка аудио превысила 30 секунд')
    if process.returncode or not any(
        stream.get('codec_type') == 'audio'
        for stream in json.loads(stdout).get('streams', [])
    ):
        raise RuntimeError('Файл не содержит читаемого аудио')
