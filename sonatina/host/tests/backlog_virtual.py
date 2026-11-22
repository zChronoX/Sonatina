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
    def __init__(self, eqmid):
        self.eqmid, self.kbps = eqmid, sh.LDAC_KBPS[eqmid]

    def set_eqmid(self, eqmid):
        self.eqmid, self.kbps = eqmid, sh.LDAC_KBPS[eqmid]
        return True


def adapter_check():
    """Adaptive LDAC: down at once when audio waits for the radio, up again after a clean
    stretch, never above the chosen quality; a failed step up makes the next wait longer."""
    enc = FakeLdac(0)                                   # chosen: 990
    a = sh.LdacAdapter(enc, 0.0)
    t = 0.0

    def run(seconds, queued, dropped=False):
        nonlocal t
        end = t + seconds
        while t < end:
            t += 0.25
            a.update(t, queued, dropped)
    run(5, 0.0)
    assert enc.eqmid == 0                               # clean: stays at the ceiling
    run(0.25, 0.1)
    assert enc.eqmid == 1                               # 990 -> 660 at once
    run(1.0, 0.1)
    assert enc.eqmid == 1                               # at most one step per 2 s
    run(1.5, 0.1)
    assert enc.eqmid == 2                               # -> 330
    run(5, 0.0, dropped=True)
    assert enc.eqmid == 2                               # bottom
    run(9.5, 0.0)
    assert enc.eqmid == 2                               # not yet clean for 10 s
    run(1.0, 0.0)
    assert enc.eqmid == 1                               # 330 -> 660
    run(3.0, 0.2)                                       # bad again soon: that try failed
    assert enc.eqmid == 2 and a.failed_ups == 1
    run(20, 0.0)
    assert enc.eqmid == 2                               # now it waits 30 s
    run(15, 0.0)
    assert enc.eqmid == 1
    run(70, 0.0)
    assert enc.eqmid == 0                               # back to the ceiling...
    run(300, 0.0)
    assert enc.eqmid == 0                               # ...and never above it
    b = sh.LdacAdapter(FakeLdac(1), 0.0)                # chosen 660: never 990
    for k in range(2000):
        b.update(k * 0.25, 0.0, False)
    assert b.encoder.eqmid == 1 and b.steps == 0
    print(f"adaptive LDAC: {a.steps} steps, failed ups {a.failed_ups}")
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
    assert enc.set_eqmid(2) and enc.kbps == 330
    bytes_per_s(400)                                    # libldac moves over a few frames
    mq = bytes_per_s(1500)
    enc.close()
    print(f"LDAC mid-stream: {hq:.0f} -> {mq:.0f} kbit/s")
    assert hq > 800 and mq < 400, (hq, mq)


asyncio.run(main())
