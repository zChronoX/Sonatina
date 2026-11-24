"""Slow radio link (headphones out of range): the pump must drop audio instead of piling
it up in Bumble's queue (2026-11-21: silence, earbuds out of sync, seconds looping)."""
import asyncio
import collections
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import sonatina_host as sh   # noqa: E402


class Queue:
    def __init__(self):
        self._packets = collections.deque()


class Host:
    def __init__(self, queue):
        self.queue = queue

    def get_data_packet_queue(self, handle):
        return self.queue if handle == 7 else None


class Channel:
    """Media channel whose writes go to the fake queue; `radio` moves them out."""
    def __init__(self):
        self.queue = Queue()
        self.connection = type("C", (), {})()
        self.connection.handle = 7
        self.connection.device = type("D", (), {})()
        self.connection.device.host = Host(self.queue)
        self.written = []

    def write(self, pdu):
        self.written.append(pdu)
        self.queue._packets.appendleft((pdu, 7))


class Encoder:
    """One 128-sample packet per 128 samples pushed (like aptX: ~2.7 ms each)."""
    name, kbps, delay_ms, rtp = "fake", 0, 0, True

    def __init__(self):
        self.pending = 0

    def push(self, pcm):
        self.pending += len(pcm) // sh.FRAME_BYTES
        while self.pending >= 128:
            self.pending -= 128
            yield b"x" * 100, 128

    def close(self):
        pass


async def main():
    ring = sh.PcmRing(prefill_ms=0)
    pump = sh.MediaPump(ring, Encoder, None, 8)
    ch = Channel()

    async def feed():
        while True:
            ring.write(bytes(480 * sh.FRAME_BYTES))
            await asyncio.sleep(0.01)
    feeder = asyncio.ensure_future(feed())
    await pump.start(ch)

    async def radio(seconds, ok):
        """ok: the radio takes everything; else nothing leaves the queue."""
        end = asyncio.get_running_loop().time() + seconds
        while asyncio.get_running_loop().time() < end:
            if ok:
                ch.queue._packets.clear()
            await asyncio.sleep(0.005)

    # just after the start the headphones may be slow: up to 0.5 s may wait (no drops)
    await radio(0.4, False)
    assert pump.radio_dropped == 0, pump.radio_dropped
    await radio(3.0, True)
    sent_ok = len(ch.written)
    assert pump.radio_dropped == 0 and sent_ok > 200, (pump.radio_dropped, sent_ok)
    await radio(2.0, False)                     # out of range for 2 s
    waiting_s = len(ch.queue._packets) * 128 / sh.RATE
    print(f"out of range: {len(ch.queue._packets)} packets waiting ({waiting_s * 1000:.0f} ms),"
          f" {pump.radio_dropped} dropped")
    assert waiting_s <= sh.LinkBacklog.LIMIT_S + 0.01 and pump.radio_dropped > 500
    before = len(ch.written)
    await radio(1.0, True)                      # back in range: fresh audio flows again
    assert len(ch.written) - before > 200 and pump.radio_episodes == 1
    seqs = [int.from_bytes(p[2:4], "big") for p in ch.written]
    assert any(b - a > 1 for a, b in zip(seqs, seqs[1:])), "the gap must show in the RTP numbers"
    await pump.stop()
    feeder.cancel()
    adapter_check()
    print("BACKLOG OK")


class FakeLdac:
    name = "LDAC"
    levels = (990, 660, 330)

    def __init__(self, eqmid):
        self.level, self.kbps = eqmid, sh.LDAC_KBPS[eqmid]

    def set_level(self, level):
        self.level, self.kbps = level, sh.LDAC_KBPS[level]
        return True


def adapter_check():
    """Adaptive quality: down when audio waits for the radio for 0.5 s (or is lost), the
    lowest step only with lost audio or a long queue, up again after a clean stretch, never
    above the chosen quality; failed steps up make the next wait longer (up to 5 min), and
    are forgotten after 10 quiet minutes (N3, 1.3.0)."""
    enc = FakeLdac(0)                                   # chosen: 990
    a = sh.QualityAdapter(enc, 0.0)
    t = 0.0

    def run(seconds, queued, dropped=False):
        nonlocal t
        end = t + seconds
        while t < end:
            t += 0.25
            a.update(t, queued, dropped)
    run(5, 0.0)
    assert enc.level == 0                               # clean: stays at the ceiling
    run(0.25, 0.1); run(0.25, 0.0); run(0.25, 0.1); run(0.25, 0.0)
    assert enc.level == 0                               # short spikes: no step (N3)
    run(0.75, 0.1)
    assert enc.level == 1                               # 0.5 s behind: 990 -> 660
    run(5.0, 0.1)
    assert enc.level == 1                               # 100 ms queue: not enough for 330
    run(0.75, 0.2)
    assert enc.level == 2                               # 200 ms queue: -> 330
    run(5, 0.0, dropped=True)
    assert enc.level == 2                               # bottom
    run(9.5, 0.0)
    assert enc.level == 2                               # not yet clean for 10 s
    run(1.0, 0.0)
    assert enc.level == 1                               # 330 -> 660
    run(3.0, 0.0, dropped=True)                         # bad again soon: that try failed
    assert enc.level == 2 and a.failed_ups == 1
    run(20, 0.0)
    assert enc.level == 2                               # now it waits 30 s
    run(15, 0.0)
    assert enc.level == 1
    run(70, 0.0)
    assert enc.level == 0                               # back to the ceiling...
    run(300, 0.0)
    assert enc.level == 0                               # ...and never above it
    # repeated failed tries: waits grow to 5 minutes, then are forgiven slowly
    for _ in range(5):
        run(0.25, 0.0, dropped=True)                    # down
        while enc.level != 0 and enc.level > a.ceiling:
            run(1.0, 0.0)
            if enc.level < 2 and a.last_up == t:
                run(1.0, 0.0, dropped=True)             # fails at once
                break
    assert a.failed_ups == 4, a.failed_ups
    assert a.UP_AFTER_S[a.failed_ups] == 300.0
    a.trouble_free_since = t
    lvl = enc.level
    run(299, 0.0)
    assert enc.level == lvl                             # waits the full 5 minutes
    run(2, 0.0)
    assert enc.level == lvl - 1
    run(600, 0.0)
    assert a.failed_ups == 3                            # 10 quiet minutes: one try forgiven
    b = sh.QualityAdapter(FakeLdac(1), 0.0)             # chosen 660: never 990
    for k in range(2000):
        b.update(k * 0.25, 0.0, False)
    assert b.encoder.level == 1 and b.steps == 0
    b.set_ceiling(2, 600.0)                             # the user picks 330 while streaming
    assert b.encoder.level == 2 and b.ceiling == 2
    print(f"adaptive quality: {a.steps} steps, failed ups {a.failed_ups}")
    sbc_aac_check()


def sbc_aac_check():
    """SBC and AAC change their bitrate mid-stream too; the audio stays a clean tone
    (750 Hz: exactly 2 cycles per 128-sample push, so the repeated block is continuous)."""
    import array, math, shutil, struct, subprocess
    lib = sh.CodecLib()
    tone = b"".join(struct.pack("<hh", v, v) for v in
                    (int(8000 * math.sin(2 * math.pi * 750 * i / sh.RATE)) for i in range(128)))
    sbc = sh.SbcEncoder(lib, 53, 679)
    assert sbc.levels[0] > sbc.levels[1] > sbc.levels[2] and len(sbc.levels) == 3, sbc.levels
    frames = []
    for level in (0, 1, 2, 1, 0):
        assert sbc.set_level(level) and sbc.kbps == sbc.levels[level]
        for _ in range(200):
            for payload, _n in sbc.push(tone):
                frames.append(payload[1:])
    sbc.close()
    stream = b"".join(frames)
    if shutil.which("ffmpeg"):
        pcm = subprocess.run(["ffmpeg", "-v", "error", "-f", "sbc", "-i", "-", "-f", "s16le", "-ac", "1", "-"],
                             input=stream, capture_output=True, check=True).stdout
        x = array.array("h"); x.frombytes(pcm)
        crossings, low = 0, False          # with hysteresis: codec noise near zero is not a cycle
        for v in x:
            if v < -2000:
                low = True
            elif v > 2000 and low:
                crossings, low = crossings + 1, False
        freq = crossings * sh.RATE / len(x)
        peak = max(abs(v) for v in x[len(x) // 10:])
        print(f"SBC levels {sbc.levels}: decoded {len(x) / sh.RATE:.2f} s, {freq:.0f} Hz")
        assert 745 < freq < 755 and 7000 < peak < 9500 and len(x) > 0.9 * 1000 * 128, (freq, peak, len(x))
    aac = sh.AacEncoder(lib, 320000, 2048)
    assert aac.levels == (320, 240, 160), aac.levels
    sizes = {}
    payloads = []
    for level in (0, 2, 0):
        assert aac.set_level(level)
        got = []
        for _ in range(1500):
            out = aac.push(tone)
            payloads += [p for p, _n in out]
            got += [len(p) for p, _n in out]
        sizes.setdefault(level, []).append(sum(got[20:]) / max(1, len(got) - 20))
    aac.close()
    if shutil.which("ffmpeg"):
        from bumble.codecs import AacAudioRtpPacket
        adts = b"".join(AacAudioRtpPacket.from_bytes(pl).to_adts() for pl in payloads)
        pcm = subprocess.run(["ffmpeg", "-v", "error", "-f", "aac", "-i", "-", "-f", "s16le", "-ac", "1", "-"],
                             input=adts, capture_output=True, check=True)
        x = array.array("h"); x.frombytes(pcm.stdout)
        x = x[4096:]
        crossings, low = 0, False
        for v in x:
            if v < -2000:
                low = True
            elif v > 2000 and low:
                crossings, low = crossings + 1, False
        freq = crossings * sh.RATE / len(x)
        print(f"AAC across bitrate changes: decoded {len(x) / sh.RATE:.2f} s, {freq:.1f} Hz, "
              f"errors {pcm.stderr[:80]!r}")
        assert 745 < freq < 755 and not pcm.stderr, (freq, pcm.stderr[:200])
    print(f"AAC levels {aac.levels}: bytes/frame {sizes}")
    assert sizes[2][0] < sizes[0][0] * 0.75, sizes

    # the real encoder changes quality mid-stream (packets get smaller, nothing breaks)
    import math
    import struct
    enc = sh.LdacEncoder(sh.CodecLib(), 0, 679)
    tone = b"".join(struct.pack("<hh", v, v) for v in
                    (int(8000 * math.sin(2 * math.pi * 1000 * i / sh.RATE)) for i in range(128)))

    def bytes_per_s(pushes):
        total = 0
        for _ in range(pushes):
            total += sum(len(p) for p, _n in enc.push(tone))
        return total * sh.RATE / (pushes * 128) * 8 / 1000
    hq = bytes_per_s(1500)
    assert enc.levels == (990, 660, 330) and enc.set_level(2) and enc.kbps == 330
    bytes_per_s(400)                                    # libldac moves over a few frames
    mq = bytes_per_s(1500)
    enc.close()
    print(f"LDAC mid-stream: {hq:.0f} -> {mq:.0f} kbit/s")
    assert hq > 800 and mq < 400, (hq, mq)


asyncio.run(main())
