#!/usr/bin/env python3
"""Turn StiryVoice narration chunks into one normalised MP3.

Settings (the same values are recorded in voices/StiryVoice/voice.json):
  * pitch +0.5 semitone, overall speed x1.07;
  * pauses longer than 0.4 s are cut down to 0.3 s (silence threshold -40 dB);
  * edge silence trimmed, chunks joined with 0.5 s gaps;
  * two-pass EBU R128 loudness: -16 LUFS, true peak -1.5 dBTP, LRA 11;
  * output: MP3, 128 kbps, mono, 44.1 kHz.

Needs ffmpeg on PATH, or the imageio-ffmpeg package (pip install imageio-ffmpeg).

Example:
  python3 voices/StiryVoice/process_narration.py \
      --out stories/audio/zazemlenie.mp3 --title "Заземление" \
      /tmp/tts/narr/raw_01.wav /tmp/tts/narr/raw_02.wav ...
"""
import argparse
import json
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

PITCH_SEMITONES = 0.5
SPEED = 1.07
PAUSE_MIN = 0.4    # pauses longer than this (s) ...
PAUSE_KEEP = 0.3   # ... are cut down to this (s)
PAUSE_DB = -40     # level below which audio counts as silence inside speech
GAP_START = 0.4    # s of silence before the first chunk
GAP_JOIN = 0.5     # s between chunks
GAP_END = 1.0      # s after the last chunk
SR = 44100
LUFS, TP, LRA = -16, -1.5, 11

FF = None  # ffmpeg binary, set in main()


def ffmpeg_bin():
    exe = shutil.which('ffmpeg')
    if exe:
        return exe
    import imageio_ffmpeg
    return imageio_ffmpeg.get_ffmpeg_exe()


def run(args):
    """Run ffmpeg and return its stderr, where the filters log their results."""
    proc = subprocess.run([FF, '-hide_banner', '-nostdin', *args],
                          capture_output=True, text=True)
    if proc.returncode != 0:
        sys.exit('ffmpeg failed:\n' + proc.stderr[-2000:])
    return proc.stderr


def duration(path):
    err = subprocess.run([FF, '-hide_banner', '-i', str(path)],
                         capture_output=True, text=True).stderr
    h, m, s = re.search(r'Duration: (\d+):(\d+):([\d.]+)', err).groups()
    return int(h) * 3600 + int(m) * 60 + float(s)


def process_chunk(src, dst):
    ratio = 2 ** (PITCH_SEMITONES / 12)
    chain = [
        f'asetrate={round(SR * ratio)}',   # raises pitch and speed by ratio
        f'aresample={SR}',
        f'atempo={SPEED / ratio:.6f}',     # brings overall speed to SPEED
        f'silenceremove=stop_periods=-1:stop_duration={PAUSE_MIN}'
        f':stop_threshold={PAUSE_DB}dB:stop_silence={PAUSE_KEEP}',
        'silenceremove=start_periods=1:start_duration=0:start_threshold=-45dB:start_silence=0.02',
        'areverse',
        'silenceremove=start_periods=1:start_duration=0:start_threshold=-45dB:start_silence=0.02',
        'areverse',
        f'aformat=sample_fmts=s16:sample_rates={SR}:channel_layouts=mono',
    ]
    run(['-y', '-i', str(src), '-af', ','.join(chain), '-c:a', 'pcm_s16le', str(dst)])


def make_silence(dst, seconds):
    run(['-y', '-f', 'lavfi', '-i', f'anullsrc=r={SR}:cl=mono',
         '-t', str(seconds), '-c:a', 'pcm_s16le', str(dst)])


def concat(parts, dst):
    lst = Path(f'{dst}.list.txt')
    lst.write_text(''.join(f"file '{p}'\n" for p in parts), encoding='utf-8')
    run(['-y', '-f', 'concat', '-safe', '0', '-i', str(lst), '-c', 'copy', str(dst)])


def loudnorm_encode(src, dst, title):
    target = f'loudnorm=I={LUFS}:TP={TP}:LRA={LRA}'
    err = run(['-i', str(src), '-af', target + ':print_format=json', '-f', 'null', '-'])
    m = json.loads(re.findall(r'\{[^{}]*\}', err)[-1])  # pass 1: measure
    second = (f"{target}:measured_I={m['input_i']}:measured_TP={m['input_tp']}"
              f":measured_LRA={m['input_lra']}:measured_thresh={m['input_thresh']}"
              f":offset={m['target_offset']}:linear=true:print_format=summary")
    run(['-y', '-i', str(src), '-af', second, '-ac', '1', '-ar', str(SR),
         '-c:a', 'libmp3lame', '-b:a', '128k',
         '-metadata', f'title={title}', '-metadata', 'artist=StiryVoice', str(dst)])


def measure(path):
    summary = run(['-i', str(path), '-af', 'ebur128=peak=true', '-f', 'null', '-']).split('Summary:')[-1]
    lufs = re.search(r'I:\s+(-?[\d.]+) LUFS', summary).group(1)
    lra = re.search(r'LRA:\s+([\d.]+) LU', summary).group(1)
    peak = re.search(r'Peak:\s+(-?[\d.]+) dBFS', summary).group(1)
    return lufs, lra, peak


def main():
    global FF
    ap = argparse.ArgumentParser(description='Join and normalise StiryVoice narration chunks.')
    ap.add_argument('chunks', nargs='+', help='raw chunk files in reading order')
    ap.add_argument('--out', required=True, help='output MP3 path')
    ap.add_argument('--title', default='', help='title tag')
    args = ap.parse_args()
    FF = ffmpeg_bin()
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='stiry_') as tmpdir:
        tmp = Path(tmpdir)
        gaps = {}
        for name, sec in [('start', GAP_START), ('join', GAP_JOIN), ('end', GAP_END)]:
            gaps[name] = tmp / f'gap_{name}.wav'
            make_silence(gaps[name], sec)
        parts = [gaps['start']]
        for i, src in enumerate(args.chunks, 1):
            dst = tmp / f'proc_{i:02d}.wav'
            process_chunk(src, dst)
            print(f'chunk {i:02d}: {duration(src):6.1f} s raw -> {duration(dst):6.1f} s processed')
            if i > 1:
                parts.append(gaps['join'])
            parts.append(dst)
        parts.append(gaps['end'])
        joined = tmp / 'joined.wav'
        concat(parts, joined)
        loudnorm_encode(joined, out, args.title)
    lufs, lra, peak = measure(out)
    print(f'{out}: {duration(out) / 60:.2f} min, {lufs} LUFS, LRA {lra} LU, true peak {peak} dBFS')


if __name__ == '__main__':
    main()
