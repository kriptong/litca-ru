#!/usr/bin/env python3
"""Build a radio-play narration of a story from the StiryVoice director's script.

The story text is cut into pieces (narration, radio voice, voice from underground).
Each piece is voiced separately, then this script stitches them back together:
per-piece effects, designed pauses with a low hum or radio static bed, loudness
normalisation.

  # 1. print the exact text of every piece (voice them one by one with one voice_id)
  python3 voices/StiryVoice/build_radioplay.py --script voices/StiryVoice/radioplay_zazemlenie.json --print-text

  # 2. put the voiced pieces into --dir (rp01.wav, rp02.wav, ...) and assemble
  python3 voices/StiryVoice/build_radioplay.py --script voices/StiryVoice/radioplay_zazemlenie.json \
      --dir /tmp/tts/rp --assemble

Needs ffmpeg and numpy.
"""
import argparse
import json
import re
import shutil
import subprocess
import sys
import wave
from pathlib import Path

import numpy as np

SR = 44100
HEADERS = {'I': 'первая', 'II': 'вторая', 'III': 'третья', 'IV': 'четвёртая', 'V': 'пятая', 'VI': 'шестая'}


# ---------------------------------------------------------------- text assembly

def read_paragraphs(story, overrides):
    """Markdown story -> list of narration paragraphs (one per source line)."""
    paras = []
    for line in Path(story).read_text(encoding='utf-8').split('\n'):
        line = line.strip()
        if not line:
            continue
        idx = len(paras)
        if str(idx) in overrides:
            paras.append(overrides[str(idx)])
        elif line.startswith('# '):
            paras.append(line[2:].strip() + '.')
        elif line.startswith('## '):
            paras.append('Часть ' + HEADERS[line[3:].strip()] + '.')
        else:
            paras.append(line.replace('*', '').strip())
    return paras


def split_pieces(paras, splits):
    """Cut marked paragraphs in two; return text for 'N' or 'N.k' references."""
    out = {}
    for i, text in enumerate(paras):
        marker = splits.get(str(i))
        if marker and marker in text:
            pos = text.index(marker)
            out[f'{i}.0'] = text[:pos].strip()
            out[f'{i}.1'] = text[pos:].strip()
        else:
            out[str(i)] = text
    return out


def piece_text(ref, pieces):
    if '-' in ref:
        a, b = ref.split('-')
        return ' '.join(pieces[str(i)] for i in range(int(a), int(b) + 1))
    return pieces[ref]


# ---------------------------------------------------------------- audio helpers

def ffmpeg(args):
    proc = subprocess.run([FF, '-hide_banner', '-nostdin', '-y', *args], capture_output=True, text=True)
    if proc.returncode != 0:
        sys.exit('ffmpeg failed:\n' + proc.stderr[-3000:])


def read_wav(path):
    with wave.open(str(path), 'rb') as w:
        assert w.getsampwidth() == 2, f'{path}: expected 16-bit PCM'
        data = np.frombuffer(w.readframes(w.getnframes()), dtype='<i2').astype(np.float32) / 32768.0
        if w.getnchannels() > 1:
            data = data.reshape(-1, w.getnchannels()).mean(axis=1)
        if w.getframerate() != SR:
            raise SystemExit(f'{path}: expected {SR} Hz, got {w.getframerate()}')
    return data


def write_wav(path, data):
    data = np.clip(data, -1.0, 1.0)
    with wave.open(str(path), 'wb') as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SR)
        w.writeframes((data * 32767.0).astype('<i2').tobytes())


def trim(data, thresh_db=-45.0, keep_s=0.15):
    """Trim leading/trailing near-silence, keeping a short natural edge."""
    thr = 10 ** (thresh_db / 20)
    loud = np.flatnonzero(np.abs(data) > thr)
    if loud.size == 0:
        return data
    keep = int(keep_s * SR)
    return data[max(0, loud[0] - keep):loud[-1] + keep]


def silences(data, thresh_db=-45.0, min_s=0.12):
    """List of (start, end) sample indexes of the silent runs inside a clip."""
    thr = 10 ** (thresh_db / 20)
    quiet = np.abs(data) <= thr
    runs = []
    start = None
    for i, q in enumerate(quiet):
        if q and start is None:
            start = i
        elif not q and start is not None:
            if i - start >= min_s * SR:
                runs.append((start, i))
            start = None
    if start is not None and len(quiet) - start >= min_s * SR:
        runs.append((start, len(quiet)))
    return runs


def boost_header_pause(data, text, pieces_note, boost):
    """Give the pause after a leading 'Часть N.' sentence a little more air."""
    m = re.match(r'^Часть\s+\w+\.', text)
    if not m:
        return data
    guess = len(m.group(0)) / max(1, len(text)) * len(data)
    runs = silences(data, boost['silence_db'], boost['min_silence_s'])
    if not runs:
        return data
    start, end = min(runs, key=lambda r: abs(0.5 * (r[0] + r[1]) - guess))
    pad = np.zeros(int(boost['after_header_s'] * SR), dtype=np.float32)
    return np.concatenate([data[:end], pad, data[end:]])


def apply_effect(data, chain, kind, tmp):
    """Run one piece through ffmpeg (effect chain or plain resample)."""
    src, dst = tmp / f'eff_{kind}_in.wav', tmp / f'eff_{kind}_out.wav'
    write_wav(src, data)
    filters = [chain] if chain else []
    filters.append(f'aformat=sample_fmts=s16:sample_rates={SR}:channel_layouts=mono')
    ffmpeg(['-i', str(src), '-af', ','.join(filters), '-c:a', 'pcm_s16le', str(dst)])
    return read_wav(dst)


def make_bed(recipe, seconds, tmp, tag):
    """Low hum of the station or radio static, as a numpy array."""
    dur = max(0.15, seconds)
    hum_src = ('sine=frequency=58:sample_rate=44100,aformat=sample_fmts=s16:channel_layouts=mono[h];'
               'anoisesrc=color=brown:amplitude=0.5:sample_rate=44100,aformat=sample_fmts=s16:channel_layouts=mono,'
               'lowpass=f=300,volume=0.35[n];[h][n]amix=inputs=2:weights=1 1,volume=0.25[out]')
    noise_src = ('anoisesrc=color=white:amplitude=0.6:sample_rate=44100,aformat=sample_fmts=s16:channel_layouts=mono,'
                 'highpass=f=500,lowpass=f=2400,tremolo=f=3.5:d=0.25,volume=0.3[out]')
    graph = {'hum': hum_src, 'noise': noise_src}.get(recipe)
    if graph is None:
        return np.zeros(int(dur * SR), dtype=np.float32)
    dst = tmp / f'bed_{tag}.wav'
    ffmpeg(['-f', 'lavfi', '-i', 'anullsrc=r=44100:cl=mono', '-t', f'{dur}',
            '-filter_complex', graph, '-map', '[out]',
            '-c:a', 'pcm_s16le', str(dst)])
    data = read_wav(dst)
    rms = float(np.sqrt(np.mean(data ** 2)))
    return data / rms if rms > 1e-6 else data  # unit RMS; caller sets the level


def make_gap(cfg, gap_cfg, tmp, tag):
    """Silence (optionally with a hum/static bed), with ramps at both ends."""
    dur = gap_cfg['dur']
    n = int(dur * SR)
    data = np.zeros(n, dtype=np.float32)
    if gap_cfg.get('bed', 'hum') != 'none':
        data = make_bed(gap_cfg['bed'], dur, tmp, tag)
        data = data * 10 ** (gap_cfg['bed_db'] / 20)
        ramp = int(min(0.25, dur / 4) * SR)
        if ramp:
            data[:ramp] *= np.linspace(0, 1, ramp, dtype=np.float32)
            data[-ramp:] *= np.linspace(1, 0, ramp, dtype=np.float32)
    return data


def fade(data, fade_out_db, ramp_s=0.35):
    """Fade the tail of a clip down to the level the gap bed continues at."""
    n = int(min(ramp_s, len(data) / SR / 3) * SR)
    if n:
        k = 10 ** (fade_out_db / 20)
        data = data.copy()
        data[-n:] *= np.linspace(1, k, n, dtype=np.float32)
    return data


# ---------------------------------------------------------------- main

def print_text(cfg, pieces):
    for seg in cfg['segments']:
        text = piece_text_seg(seg, pieces)
        print(f'=== {seg["id"]} ({seg["kind"]}) ===')
        print(text)
        print()


def piece_text_seg(seg, pieces):
    return ' '.join(piece_text(ref, pieces) for ref in seg['src'])


def resolve_clip(workdir, seg_id, tmp):
    """Find a voiced piece (WAV or FLAC/MP3/Ogg), decoding it to WAV when needed."""
    for ext in ('.wav', '.flac', '.mp3', '.ogg'):
        path = Path(workdir) / f'{seg_id}{ext}'
        if not path.exists():
            continue
        if ext == '.wav':
            return path
        dst = tmp / f'dec_{seg_id}.wav'
        ffmpeg(['-i', str(path), '-ac', '1', '-ar', str(SR), '-c:a', 'pcm_s16le', str(dst)])
        return dst
    sys.exit(f'missing piece {seg_id} in {workdir}')


def match_level(data, target_rms_db=-19.0, floor_db=-50.0):
    """Bring a piece to a consistent working level before the effect chain.

    The speech engine occasionally returns a piece much quieter than the rest
    (different internal loudness handling); this flattens those differences.
    """
    active = np.abs(data) > 10 ** (floor_db / 20)
    if not active.any():
        return data
    rms = float(np.sqrt(np.mean(data[active] ** 2)))
    gain_db = float(np.clip(target_rms_db - 20 * np.log10(rms + 1e-9), -12.0, 30.0))
    return data * 10 ** (gain_db / 20)


def assemble(cfg, pieces, workdir, tmpdir):
    segs = cfg['segments']
    tmp = Path(tmpdir)
    tmp.mkdir(parents=True, exist_ok=True)
    clips = []
    for i, seg in enumerate(segs):
        raw = resolve_clip(workdir, seg['id'], tmp)
        data = trim(read_wav(raw))
        data = match_level(data, cfg.get('piece_target_rms_db', -19.0))
        chain = cfg['effects'].get(seg['kind'])
        if chain:
            data = apply_effect(data, chain, f'{i:02d}', tmp)
        if seg.get('gain_db'):
            data = data * 10 ** (seg['gain_db'] / 20)   # artistic offset after the effect
        data = boost_header_pause(data, piece_text_seg(seg, pieces), None, cfg['pause_boost'])
        clips.append(data)

    parts = [np.zeros(int(cfg['lead_in_s'] * SR), dtype=np.float32)]
    for i, (seg, data) in enumerate(zip(segs, clips)):
        nxt = segs[i + 1]['id'] if i + 1 < len(segs) else 'end'
        key = f'{seg["id"]}->{nxt}'
        gap_cfg = dict(cfg['gap_default'])
        gap_cfg['dur'] = seg['pause_after']
        gap_cfg.update(cfg['gaps'].get(key, {}))
        if nxt == 'end':
            parts.append(data)  # the closing tail is faded once, at the very end
        else:
            parts.append(fade(data, gap_cfg.get('bed_db', -38) - 6))
        parts.append(make_gap(cfg, gap_cfg, tmp, f'{seg["id"]}_{nxt}'))
        rms = float(np.sqrt(np.mean(data ** 2)))
        print(f'{seg["id"]:5s} {seg["kind"]:9s} {len(data) / SR:6.2f}s  rms {20 * np.log10(rms + 1e-9):5.1f} dB  '
              f'gap {gap_cfg["dur"]:.1f}s ({gap_cfg.get("bed")} {gap_cfg.get("bed_db")} dB)')

    tail = np.zeros(int(cfg['tail_fade_s'] * SR), dtype=np.float32)
    parts.append(tail)
    joined = np.concatenate(parts)
    joined = fade(joined[::-1], -60, 2.0)[::-1]  # slow fade of the very end

    joined_path = tmp / 'joined.wav'
    write_wav(joined_path, joined)

    # loudness: measure, then apply the measured values (EBU R128, two passes)
    loud = cfg['loudness']
    target = f"loudnorm=I={loud['lufs']}:TP={loud['tp']}:LRA={loud['lra']}"
    err = subprocess.run([FF, '-hide_banner', '-i', str(joined_path), '-af', target + ':print_format=json',
                          '-f', 'null', '-'], capture_output=True, text=True).stderr
    m = json.loads(re.findall(r'\{[^{}]*\}', err)[-1])
    second = (f"{target}:measured_I={m['input_i']}:measured_TP={m['input_tp']}"
              f":measured_LRA={m['input_lra']}:measured_thresh={m['input_thresh']}"
              f":offset={m['target_offset']}:linear=true")
    out = Path(cfg['output'])
    out.parent.mkdir(parents=True, exist_ok=True)
    ffmpeg(['-i', str(joined_path), '-af', second, '-ac', '1', '-ar', str(SR),
            '-c:a', 'libmp3lame', '-b:a', f"{loud['mp3_kbps']}k",
            '-metadata', f"title={cfg['title']}", '-metadata', 'artist=StiryVoice', str(out)])
    info = subprocess.run([FF, '-hide_banner', '-i', str(out)], capture_output=True, text=True).stderr
    print('\n' + '\n'.join(l.strip() for l in info.splitlines() if 'Duration' in l or 'Stream' in l))
    print('written:', out)


def main():
    global FF
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument('--script', required=True, help="director's script JSON")
    ap.add_argument('--print-text', action='store_true', help='print the text of every piece')
    ap.add_argument('--assemble', action='store_true', help='join voiced pieces from --dir')
    ap.add_argument('--dir', default='/tmp/tts/rp', help='folder with rp01.wav ... pieces')
    ap.add_argument('--tmp', default='/tmp/tts/rp/tmp', help='scratch folder')
    args = ap.parse_args()
    FF = shutil.which('ffmpeg')
    if not FF:
        import imageio_ffmpeg
        FF = imageio_ffmpeg.get_ffmpeg_exe()

    cfg = json.loads(Path(args.script).read_text(encoding='utf-8'))
    paras = read_paragraphs(cfg['story'], cfg.get('text_overrides', {}))
    pieces = split_pieces(paras, cfg.get('splits', {}))
    if args.print_text or not args.assemble:
        print_text(cfg, pieces)
    if args.assemble:
        assemble(cfg, pieces, args.dir, args.tmp)


if __name__ == '__main__':
    main()
