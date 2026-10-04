import array
import json
from pathlib import Path
import subprocess
import sys

source = Path(sys.argv[1])
ffmpeg = next((Path.home() / 'AppData/Local/Microsoft/WinGet/Packages').glob('Gyan.FFmpeg*/**/ffmpeg.exe'))
probe = ffmpeg.with_name('ffprobe.exe')
metadata = json.loads(subprocess.check_output([str(probe), '-v','error','-show_format','-show_streams','-of','json',str(source)]))
print('Reference duration:', metadata['format']['duration'])
print('Channels:', metadata['streams'][0]['channels'])
data = subprocess.check_output([str(ffmpeg),'-v','error','-i',str(source),'-ar','8000','-ac','1','-f','s16le','pipe:1'])
samples = array.array('h'); samples.frombytes(data)
for i in range(0,len(samples),8000*5):
    chunk = samples[i:i+8000*5]
    rms = (sum((x/32768)**2 for x in chunk)/len(chunk))**.5
    print(f'{i/8000:.0f}-{min(i+len(chunk),len(samples))/8000:.0f}s RMS={rms:.3f}')
