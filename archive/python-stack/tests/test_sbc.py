import unittest

from sonatina.sbc import SbcParams, bitrate, bitrate_table


class TestSbcParams(unittest.TestCase):
    def test_frame_length_matches_the_spec_formula(self):
        # 16 blocks, 8 subbands, joint stereo: 12 header bytes + 8 + blocks*bitpool/8
        self.assertEqual(
            SbcParams(blocks=16, subbands=8, channel_mode="joint").frame_len(53), 126
        )
        self.assertEqual(
            SbcParams(blocks=16, subbands=8, channel_mode="joint").frame_len(24), 68
        )
        # mono: half the payload
        self.assertEqual(
            SbcParams(blocks=16, subbands=8, channel_mode="mono").frame_len(53), 114
        )
        # 4 subbands, 4 blocks
        self.assertEqual(
            SbcParams(blocks=4, subbands=4, channel_mode="mono").frame_len(16), 14
        )

    def test_bitrate_is_monotonic_in_bitpool(self):
        params = SbcParams()
        values = [params.bitrate_kbps(bp) for bp in (2, 10, 24, 40, 53)]
        self.assertEqual(values, sorted(values))
        self.assertAlmostEqual(params.bitrate_kbps(53), 378.0, places=3)
        self.assertAlmostEqual(bitrate(params, 24), params.bitrate_kbps(24), places=9)

    def test_capability_roundtrip(self):
        cases = [
            SbcParams(),
            SbcParams(
                sampling=44100,
                channel_mode="stereo",
                blocks=8,
                subbands=4,
                allocation="snr",
                min_bitpool=2,
                max_bitpool=35,
            ),
            SbcParams(
                sampling=16000,
                channel_mode="mono",
                blocks=12,
                subbands=8,
                allocation="loudness",
                min_bitpool=4,
                max_bitpool=63,
            ),
        ]
        for params in cases:
            with self.subTest(params=params):
                self.assertEqual(
                    SbcParams.from_capability(params.to_capability()), params
                )

    def test_capability_octets(self):
        # Same bytes a real 48 kHz joint-stereo SBC endpoint advertises.
        self.assertEqual(SbcParams().to_capability(), bytes([0x11, 0x15, 0x02, 0x35]))

    def test_capability_mask_picks_the_best_supported_value(self):
        # 44100+48000, mono+joint, 16 blocks, 8/4 subbands, both allocations:
        # the mask must resolve to the highest supported value.
        data = bytes([0x30 | 0x09, 0x10 | 0x0C | 0x03, 2, 53])
        params = SbcParams.from_capability(data)
        self.assertEqual(params.sampling, 48000)
        self.assertEqual(params.channel_mode, "mono")
        self.assertEqual(params.blocks, 16)
        self.assertEqual(params.subbands, 8)

    def test_clamped_to_sink(self):
        requested = SbcParams(sampling=48000, channel_mode="joint", max_bitpool=53)
        sink = SbcParams(
            sampling=44100, channel_mode="stereo", blocks=8, subbands=4, max_bitpool=35
        )
        final = requested.clamped_to(sink)
        self.assertEqual(final.sampling, 44100)
        self.assertEqual(final.channel_mode, "stereo")
        self.assertEqual(final.max_bitpool, 35)
        self.assertEqual(final.blocks, 8)
        self.assertEqual(final.subbands, 4)

    def test_invalid_parameters(self):
        for params in (
            SbcParams(sampling=22050),
            SbcParams(channel_mode="quad"),
            SbcParams(blocks=10),
            SbcParams(subbands=6),
            SbcParams(allocation="water"),
            SbcParams(min_bitpool=0, max_bitpool=10),
            SbcParams(min_bitpool=10, max_bitpool=5),
            SbcParams(min_bitpool=2, max_bitpool=300),
        ):
            with self.subTest(params=params):
                with self.assertRaises(ValueError):
                    params.validate()

    def test_bitrate_table_clamps(self):
        params = SbcParams(min_bitpool=10, max_bitpool=40)
        table = bitrate_table(params, [2, 20, 99])
        self.assertEqual([row[0] for row in table], [10, 20, 40])


if __name__ == "__main__":
    unittest.main()
