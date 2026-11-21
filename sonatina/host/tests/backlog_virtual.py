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

    await radio(1.0, True)
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
    print("BACKLOG OK")


asyncio.run(main())
