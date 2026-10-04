"""Generate an original fictional horror broadcast using installed Windows voices."""
import array
import asyncio
import json
import math
from pathlib import Path
import random
import shutil
import subprocess
import sys
import wave

ROOT = Path(__file__).resolve().parent
NEURAL = '--neural' in sys.argv
OUT = ROOT / ('final' if NEURAL else 'output')
OUT.mkdir(exist_ok=True)
RATE = 24000
random.seed(73)
ffmpeg = shutil.which('ffmpeg')
if not ffmpeg:
    candidates = list((Path.home() / 'AppData/Local/Microsoft/WinGet/Packages').glob('Gyan.FFmpeg*/**/ffmpeg.exe'))
    ffmpeg = str(candidates[0]) if candidates else None
if not ffmpeg:
    raise RuntimeError('FFmpeg not found')

lines = [
 ('f', 'Внимание! Пропал человек! Пропал ночью у въезда в пригород.', -1),
 ('f', 'Приметы: худой. Высокий.', -1),
 ('f', 'Странно одет.', -1),
 ('f', 'Особые приметы: голова в форме', -1),
 ('f', 'с длинными лучами. Оно хочет', -1),
 ('m', 'Убить тебя.', -2),
 ('m', 'Оно близко. Он реален.', -2),
 ('m', 'Ха! Ха ха! А ха ха ха ха!', 3),
 ('m', 'Я не могу выбраться уже... Уже восемь месяцев!', 1),
 ('m', 'Я... Я застрял... Оно готовит для меня что-то...', 0),
 ('m', 'Я... Я не могу сказать или описать это...', -1),
 ('m', 'ОНО БЛИЗКО!', 1),
 ('m', 'А-а-а-а!', 3),
]
manifest = [dict(voice='Microsoft Irina Desktop' if sex == 'f' else 'Microsoft Pavel',
                 text=text, rate=rate, path=str(OUT / f'raw{i}.wav'))
            for i, (sex, text, rate) in enumerate(lines)]
(OUT / 'manifest.json').write_text(json.dumps(manifest, ensure_ascii=False), encoding='utf-8')
if NEURAL:
    import edge_tts
    async def synthesize():
        semaphore = asyncio.Semaphore(3)
        async def one(i, sex, text, rate):
            async with semaphore:
                target = OUT / f'raw{i}.mp3'
                if target.exists() and target.stat().st_size > 1000:
                    return
                try:
                    await asyncio.wait_for(edge_tts.Communicate(
                        text, 'ru-RU-SvetlanaNeural' if sex == 'f' else 'ru-RU-DmitryNeural',
                        rate='-8%' if sex == 'f' else ('+12%' if i >= 7 else '-14%'),
                        pitch='-2Hz' if sex == 'f' else '-12Hz',
                    ).save(str(target)), timeout=50)
                except Exception:
                    if sex != 'm':
                        raise
                    shell = next((Path.home() / '.cache/codex-runtimes').glob('**/powershell/pwsh.exe'))
                    item = OUT / f'fallback{i}.json'
                    item.write_text(json.dumps([manifest[i]], ensure_ascii=False), encoding='utf-8')
                    subprocess.run([str(shell), '-NoProfile', '-File', str(ROOT/'speak.ps1'), '-Manifest', str(item)], check=True)
                    subprocess.run([ffmpeg,'-v','error','-y','-i', str(OUT/f'raw{i}.wav'),str(target)], check=True)
                print(f'Voice segment {i+1}/{len(lines)} ready', flush=True)
        await asyncio.gather(*(one(i, *line) for i, line in enumerate(lines)))
    asyncio.run(synthesize())
else:
    shell = sys.argv[1] if len(sys.argv) > 1 else 'powershell.exe'
    subprocess.run([shell, '-NoProfile', '-ExecutionPolicy', 'Bypass', '-File',
                    str(ROOT / 'speak.ps1'), '-Manifest', str(OUT / 'manifest.json')], check=True)

def decode(path, filters):
    data = subprocess.run([ffmpeg, '-v', 'error', '-i', str(path), '-af', filters,
        '-f', 's16le', '-ac', '1', '-ar', str(RATE), 'pipe:1'], check=True, capture_output=True).stdout
    values = array.array('h'); values.frombytes(data)
    peak = max(map(abs, values), default=1) or 1
    return [v / peak * 0.42 for v in values]

def noise(seconds, volume=0.1):
    return [random.uniform(-volume, volume) for _ in range(int(seconds*RATE))]

def tone(seconds, frequencies=(1000,), volume=0.22):
    count = int(seconds*RATE)
    return [volume * sum(math.sin(2*math.pi*f*i/RATE) for f in frequencies)/len(frequencies)
            * min(1, i/240, (count-i)/240) for i in range(count)]

track = []
def append(values): track.extend(values)
def silence(seconds): append([0.] * int(seconds*RATE))
def speech(i, interference=False):
    sex = lines[i][0]
    filters = 'highpass=f=230,lowpass=f=3100,aecho=0.8:0.65:24:0.1'
    if sex == 'm':
        filters = 'aresample=24000,asetrate=20400,aresample=24000,atempo=1.13,highpass=f=100,lowpass=f=2600,aecho=0.8:0.7:65|115:0.18|0.1'
    values = decode(OUT / f'raw{i}.{"mp3" if NEURAL else "wav"}', filters)
    while len(values) > 2400 and max(map(abs, values[-1200:])) < .004:
        values = values[:-1200]
    for j, value in enumerate(values):
        hiss = random.uniform(-0.014, 0.014)
        if interference:
            value *= 0.25 if (j // 1800) % 4 == 1 else 1
            hiss += random.uniform(-0.11, 0.11) * (1 if (j // 2400) % 3 == 0 else 0.15)
        if sex == 'm':
            value = math.tanh(value*2.1)/2.1
        values[j] = value + hiss
    append(values)

append(tone(2.2, (850, 960), .19)); append(noise(.45)); silence(.3)
speech(0); silence(.6); speech(1); silence(.2); speech(2, True); append(noise(.45))
speech(3); append(tone(.8, (1150,), .21)); speech(4)
append(noise(.25, .2)); speech(5); silence(.55)
speech(6); append(noise(.85)); speech(7, True); silence(.4)
speech(8); silence(.4); speech(9); silence(.65); speech(10); silence(1.2)
speech(11); append(noise(.2, .14))
if NEURAL:
    speech(12, True)
# Original synthetic scream: shifting harmonics and breath noise, abrupt cutoff.
for i in range(int(1.5*RATE)):
    t = i / RATE
    phase = 2*math.pi*(520*t + 150*t*t + 6*math.sin(2*math.pi*8*t))
    env = min(1, t*8)
    track.append(env * (.22*math.sin(phase) + .11*math.sin(2*phase) + random.uniform(-.07,.07)))
silence(.4)
pcm = array.array('h', [int(max(-.8, min(.8, x))*32767) for x in track])
wav = OUT / 'horror_broadcast.wav'
with wave.open(str(wav), 'wb') as file:
    file.setnchannels(1); file.setsampwidth(2); file.setframerate(RATE); file.writeframes(pcm.tobytes())
for suffix, codec in [('mp3', ['-c:a', 'libmp3lame', '-b:a', '128k']),
                      ('ogg', ['-c:a', 'libopus', '-b:a', '64k'])]:
    subprocess.run([ffmpeg, '-v', 'error', '-y', '-i', str(wav), '-af',
                    'loudnorm=I=-18:TP=-2:LRA=9', *codec,
                    str(OUT / ('horror_broadcast.' + suffix))], check=True)
print(f'Duration: {len(track)/RATE:.1f}s; peak: {max(map(abs,track)):.3f}')
print(OUT / 'horror_broadcast.mp3')
