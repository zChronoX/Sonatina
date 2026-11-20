import unittest

from sonatina import rtp


class TestRtpHeader(unittest.TestCase):
    def test_roundtrip(self):
        packet = rtp.RtpPacket(
            seq=65535,
            timestamp=4294967295,
            ssrc=0xDEADBEEF,
            marker=True,
            payload=b"\x01\x02\x03",
        )
        decoded = rtp.RtpPacket.decode(packet.encode())
        self.assertEqual(decoded.seq, 65535)
        self.assertEqual(decoded.timestamp, 4294967295)
        self.assertEqual(decoded.ssrc, 0xDEADBEEF)
        self.assertTrue(decoded.marker)
        self.assertEqual(decoded.payload_type, 96)
        self.assertEqual(decoded.payload, b"\x01\x02\x03")

    def test_sequence_wraps(self):
        packet = rtp.RtpPacket(seq=rtp.MAX_SEQ + 5, timestamp=0, payload=b"x")
        self.assertEqual(rtp.RtpPacket.decode(packet.encode()).seq, 5)

    def test_rejects_bad_version(self):
        raw = bytearray(rtp.RtpPacket(payload=b"").encode())
        raw[0] = 0x40
        with self.assertRaises(rtp.RtpError):
            rtp.RtpPacket.decode(bytes(raw))

    def test_rejects_short_packet(self):
        with self.assertRaises(rtp.RtpError):
            rtp.RtpPacket.decode(b"\x80\x60\x00")

    def test_timestamp_advance(self):
        self.assertEqual(rtp.timestamp_after(128, 48000), 240)
        self.assertEqual(rtp.timestamp_after(1024, 44100), 2089)


class TestPayloads(unittest.TestCase):
    def test_sbc_roundtrip(self):
        frames = [bytes([i]) * 68 for i in range(3)]
        payload = rtp.pack_sbc(frames)
        self.assertEqual(payload[0], 0x03)  # frame count in bits 3..0 (A2DP spec)
        self.assertEqual(rtp.unpack_sbc(payload, 68), frames)

    def test_sbc_rejects_empty_and_truncated(self):
        with self.assertRaises(rtp.RtpError):
            rtp.pack_sbc([])
        with self.assertRaises(rtp.RtpError):
            rtp.unpack_sbc(b"\x00" + bytes(10), 68)
        with self.assertRaises(rtp.RtpError):
            rtp.unpack_sbc(b"\x20" + bytes(10), 68)

    def test_aac_roundtrip(self):
        au = bytes(range(256)) * 2
        payload = rtp.pack_aac(au, index=1)
        self.assertEqual(rtp.unpack_aac(payload), au)

    def test_aac_rejects_truncated(self):
        with self.assertRaises(rtp.RtpError):
            rtp.unpack_aac(b"\x00\x10")

    def test_raw_roundtrip(self):
        self.assertEqual(rtp.unpack_raw(rtp.pack_raw(b"abc")), b"abc")

    def test_ldac_roundtrip_and_rejects_empty(self):
        body = bytes([0xAA, 0x10, 0x20, 0x30]) * 2
        payload = rtp.pack_ldac(body, frame_count=2)
        self.assertEqual(payload[0], 0x02)
        count, raw = rtp.unpack_ldac(payload)
        self.assertEqual(count, 2)
        self.assertEqual(raw, body)
        with self.assertRaises(rtp.RtpError):
            rtp.unpack_ldac(b"\x00\xAA")


class TestSequenceTracker(unittest.TestCase):
    def test_no_loss(self):
        tracker = rtp.SequenceTracker()
        for seq in (10, 11, 12, 13):
            self.assertEqual(tracker.feed(seq), 0)
        self.assertEqual(tracker.lost, 0)
        self.assertEqual(tracker.seen, 4)

    def test_gap_counts_lost(self):
        tracker = rtp.SequenceTracker()
        tracker.feed(1)
        self.assertEqual(tracker.feed(4), 2)
        self.assertEqual(tracker.lost, 2)

    def test_wrap_around(self):
        tracker = rtp.SequenceTracker()
        tracker.feed(65535)
        self.assertEqual(tracker.feed(0), 0)
        self.assertEqual(tracker.lost, 0)

    def test_out_of_order_is_ignored(self):
        tracker = rtp.SequenceTracker()
        tracker.feed(10)
        tracker.feed(9)
        self.assertEqual(tracker.lost, 0)


if __name__ == "__main__":
    unittest.main()
