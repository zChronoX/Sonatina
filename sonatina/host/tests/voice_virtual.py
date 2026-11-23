"""Headset microphone cleaning (1.2.0, A3): RNNoise, automatic level, gate, limiter in the
codec library, and its place in ScoAudio. No hardware, no numpy: a vowel-like synthetic
voice (pulse train through three formants, syllables 3 per second) plus white noise.

  1. Noise reduction: between syllables the noise drops by >= 8 dB ("normal") and more
     with "strong"; the voice keeps its level (within 3 dB).
  2. Automatic level: a quiet voice is raised by >= 10 dB within a few seconds; a loud one
     never goes over the limiter (29000).
  3. Gate: with the gate on, the pauses go much quieter than the voice.
  4. ScoAudio: 8 kHz in, 48 kHz stereo out (left = right), settings read live.
"""
import array
import math
import os
import random
import sys
import types

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
import sonatina_hfp as shfp           # noqa: E402
from sonatina_codecs import CodecLib  # noqa: E402

RATE = 48000
FRAME = 480
LIB = CodecLib()
assert LIB.has_voice, 'codec library without the voice functions (rebuild it)'
L = LIB.lib


def resonator(x, f, bw):
    r = math.exp(-math.pi * bw / RATE)
    a1, a2 = -2 * r * math.cos(2 * math.pi * f / RATE), r * r
    y1 = y2 = 0.0
    out = [0.0] * len(x)
    g = 1 - r
    for i, v in enumerate(x):
        y = g * v - a1 * y1 - a2 * y2
        out[i] = y
        y2, y1 = y1, y
    return out


def voice(seconds, peak, syllables=3.0):
    """(samples as floats, mask of the voiced parts)."""
    n = int(seconds * RATE)
    phase = 0.0
    x = [0.0] * n
    for i in range(n):
        f0 = 120 + 15 * math.sin(2 * math.pi * 0.7 * i / RATE)
        phase += f0 / RATE
        if phase >= 1.0:
            phase -= 1.0
            x[i] = 1.0
    for f, bw in ((700, 90), (1220, 110), (2600, 160)):
        x = resonator(x, f, bw)
    env, e = [0.0] * n, 0.0
    mask = [False] * n
    for i in range(n):
        on = math.sin(2 * math.pi * syllables * i / RATE) > 0
        e += 0.01 * ((1.0 if on else 0.0) - e)
        env[i] = e
        mask[i] = e > 0.5
    y = [a * b for a, b in zip(x, env)]
    top = max(abs(v) for v in y) or 1.0
    return [v / top * peak * 32767 for v in y], mask


def noise(n, level, seed=3):
    rnd = random.Random(seed)
    return [rnd.gauss(0, level * 32767) for _ in range(n)]


def to_pcm(x):
    return array.array('h', (max(-32768, min(32767, int(v))) for v in x))


def process(pcm, flags, strength=1.0, gain_db=0.0):
    v = L.sont_voice_create(gain_db)
    out = array.array('h')
    for i in range(0, len(pcm) - FRAME + 1, FRAME):
        buf = bytearray(pcm[i:i + FRAME].tobytes())
        cbuf = (__import__('ctypes').c_char * len(buf)).from_buffer(buf)
        L.sont_voice_process(v, cbuf, flags, strength, gain_db)
        out.frombytes(bytes(buf))
    L.sont_voice_destroy(v)
    return out


def db(samples):
    if not samples:
        return -120.0
    return 10 * math.log10(sum(s * s for s in samples) / len(samples) + 1e-9)


def parts(out, mask, delay, skip_s=1.0, settle_s=0.0):
    """(voice dB, pause dB) after the first second, out aligned by `delay` samples; pause
    samples only once the pause has lasted `settle_s`."""
    v, p = [], []
    start = int(skip_s * RATE)
    quiet = 0
    for i in range(start, len(out) - delay):
        quiet = 0 if mask[i] else quiet + 1
        if mask[i]:
            v.append(out[i + delay])
        elif quiet > settle_s * RATE:
            p.append(out[i + delay])
    return db(v), db(p)


def main():
    speech, mask = voice(5, 0.25)
    mixed = to_pcm([a + b for a, b in zip(speech, noise(len(speech), 0.02))])

    # 1. noise reduction (RNNoise output is one frame late)
    off_v, off_p = parts(process(mixed, 0), mask, 0)
    nor_v, nor_p = parts(process(mixed, 1, 0.85), mask, FRAME)
    str_v, str_p = parts(process(mixed, 1, 1.0), mask, FRAME)
    print(f'off     voice {off_v:.1f} dB pauses {off_p:.1f} dB')
    print(f'normal  voice {nor_v:.1f} dB pauses {nor_p:.1f} dB')
    print(f'strong  voice {str_v:.1f} dB pauses {str_p:.1f} dB')
    assert off_p - nor_p >= 8, 'noise reduction too weak'
    assert nor_p - str_p >= 0.5, '"strong" not stronger than "normal"'
    assert abs(nor_v - off_v) <= 3 and abs(str_v - off_v) <= 3, 'the voice lost level'
    print('1. noise reduction OK')

    # 2. automatic level
    quiet, qmask = voice(6, 0.03)
    qpcm = to_pcm(quiet)
    plain_v, _ = parts(process(qpcm, 0), qmask, 0, skip_s=4.0)
    agc_v, _ = parts(process(qpcm, 2), qmask, FRAME, skip_s=4.0)
    print(f'quiet voice {plain_v:.1f} dB -> AGC {agc_v:.1f} dB')
    assert agc_v - plain_v >= 10, 'quiet voice not raised'
    loud, _lmask = voice(3, 0.95)
    lout = process(to_pcm(loud), 2 | 1, 0.85, 12.0)
    assert max(abs(v) for v in lout[RATE:]) <= 29500, max(abs(v) for v in lout[RATE:])
    fixed = process(to_pcm(loud), 0, 1.0, 12.0)            # fixed +12 dB: the limiter holds it
    assert max(abs(v) for v in fixed[RATE:]) <= 29500, max(abs(v) for v in fixed[RATE:])
    print('2. automatic level and limiter OK')

    # 3. gate (it waits 200 ms of no voice: pauses of 1 s here)
    slow, smask = voice(6, 0.25, syllables=0.5)
    smixed = to_pcm([a + b for a, b in zip(slow, noise(len(slow), 0.02))])
    nor_v, nor_p = parts(process(smixed, 1, 0.85), smask, FRAME, settle_s=0.35)
    gate_v, gate_p = parts(process(smixed, 1 | 4, 0.85), smask, FRAME, settle_s=0.35)
    print(f'no gate voice {nor_v:.1f} dB pauses {nor_p:.1f} dB')
    print(f'gate    voice {gate_v:.1f} dB pauses {gate_p:.1f} dB')
    assert gate_p <= nor_p - 12 and abs(gate_v - nor_v) <= 3, 'gate not closing in the pauses'
    print('3. gate OK')

    # 4. in ScoAudio: 8 kHz CVSD PCM in, 48 kHz stereo out, settings live
    settings = types.SimpleNamespace(mic_denoise='off', mic_agc=False, mic_gate=False, mic_gain_db=6.0)
    got = []
    sco = shfp.ScoAudio(L, 'cvsd', types.SimpleNamespace(), None, got.append, 6.0,
                        lambda sdu: None, settings=settings)
    tone = array.array('h', (int(8000 * math.sin(2 * math.pi * 440 * i / 8000)) for i in range(8000)))
    raw = tone.tobytes()
    for i in range(0, len(raw), 240):
        sco._receive(raw[i:i + 240], 0)
    out = array.array('h')
    out.frombytes(b''.join(got))
    assert len(out) >= 2 * 46000 and out[0::2] == out[1::2], len(out)
    assert 15000 < max(out) < 17000, max(out)                 # +6 dB fixed gain
    assert sco.voice_flags == 0 and 'gain +6 dB' in sco.stats(), sco.stats()
    settings.mic_denoise, settings.mic_agc = 'strong', True
    for i in range(0, len(raw), 240):
        sco._receive(raw[i:i + 240], 0)
    assert sco.voice_flags == 3 and 'rnnoise' in sco.stats() and 'agc' in sco.stats(), sco.stats()
    sco.close()
    print('4. ScoAudio path OK:', sco.voice_summary())
    print('VOICE OK')


main()
