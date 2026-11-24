"""M4 (1.3.0): 10-band equalizer, presets per headset, no clipping, live."""
import array, asyncio, math, os, struct, sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
import sonatina_host as sh

RATE = sh.RATE


def tone(freq, amp=8000, frames=RATE):
    return b"".join(struct.pack("<hh", v, v) for v in
                    (int(amp * math.sin(2 * math.pi * freq * i / RATE)) for i in range(frames)))


def level_db(pcm):
    a = array.array("h"); a.frombytes(pcm)
    a = a[len(a) // 2:]                         # after the filters settle
    rms = math.sqrt(sum(v * v for v in a) / len(a))
    return 20 * math.log10(max(rms, 1e-9))


def run(eq, pcm):
    out = b""
    step = 128 * 4
    for i in range(0, len(pcm), step):
        out += eq.process(pcm[i:i + step], 128)
    return out


def main():
    lib = sh.CodecLib()
    eq = sh.Equalizer(lib)
    low, mid, high = tone(62), tone(1000), tone(8000)
    ref = {f: level_db(p) for f, p in ((62, low), (1000, mid), (8000, high))}
    assert run(eq, mid) == mid, 'flat: bit exact'
    eq.set(sh.EQ_PRESETS["bass"])
    assert eq.preamp_db == -6
    d62 = level_db(run(eq, low)) - ref[62]
    d1k = level_db(run(eq, mid)) - ref[1000]
    print(f'bass preset: 62 Hz {d62:+.1f} dB, 1 kHz {d1k:+.1f} dB (preamp -6)')
    assert -1.5 < d62 < 0.5 and -6.8 < d1k < -5.2, (d62, d1k)      # +5 -6, 0 -6
    eq.set(sh.EQ_PRESETS["treble"])
    d8k = level_db(run(eq, high)) - ref[8000]
    print(f'treble preset: 8 kHz {d8k:+.1f} dB (+5 -6)')
    assert -2 < d8k < 0.5, d8k
    eq.set([12] * 10)                           # extreme: the preamp keeps it from clipping
    loud = run(eq, tone(1000, amp=32000))
    a = array.array("h"); a.frombytes(loud)
    print(f'all +12, full scale: peak {max(abs(v) for v in a)}')
    assert max(abs(v) for v in a) < 32767
    eq.set([0] * 10)
    assert run(eq, mid) == mid, 'back to flat: untouched'
    eq.close()
    # settings per headset, presets and custom
    host = sh.SonatinaHost(sh.Settings(address='AA:AA:AA:AA:AA:01', headsets={'AA:AA:AA:AA:AA:01': {'name': 'A'}}))
    run_ = asyncio.run
    run_(host.set_eq('AA:AA:AA:AA:AA:01', on=True, preset='bass'))
    assert host.eq.gains == sh.EQ_PRESETS['bass']
    run_(host.set_eq('AA:AA:AA:AA:AA:01', preset='custom'))
    assert host.s.headsets['AA:AA:AA:AA:AA:01']['eq']['custom'] == sh.EQ_PRESETS['bass'], 'custom starts from the preset'
    run_(host.set_eq('AA:AA:AA:AA:AA:01', custom=[20, -20, 3.4, 0, 0, 0, 0, 0, 0]))
    assert host.eq.gains == [12, -12, 3, 0, 0, 0, 0, 0, 0, 0], host.eq.gains
    run_(host.set_eq('BB:BB:BB:BB:BB:02', on=True, preset='treble'))
    assert host.eq.gains == [12, -12, 3, 0, 0, 0, 0, 0, 0, 0], 'other headset: not applied now'
    run_(host.set_eq('AA:AA:AA:AA:AA:01', on=False))
    assert host.eq.gains == [0] * 10 and not host.eq.active, 'switch off: original sound'
    snap = host.snapshot()
    hs = {h['address']: h for h in snap['headsets']}
    assert hs['BB:BB:BB:BB:BB:02']['eq'] == {'on': True, 'preset': 'treble', 'custom': [0] * 10}, hs['BB:BB:BB:BB:BB:02']
    assert snap['eq']['presets']['voice'] == sh.EQ_PRESETS['voice']
    try:
        run_(host.set_eq('AA:AA:AA:AA:AA:01', preset='loud'))
        raise AssertionError('unknown preset accepted')
    except ValueError:
        pass
    print('EQ OK')


main()
