import unittest

import numpy as np

from sonatina import codecs, rtp
from sonatina.sbc import SbcParams


def tone(seconds=0.2, sampling=48000, channels=2, frequency=440.0):
    count = int(seconds * sampling)
    t = np.arange(count, dtype=np.float64) / sampling
    mono = (np.sin(2.0 * np.pi * frequency * t) * 0.4 * 32767.0).astype(np.int16)
    if channels == 2:
        return np.stack([mono, mono], axis=1)
    return mono[:, None]


class TestPcmCodec(unittest.TestCase):
    def test_roundtrip_is_exact(self):
        pcm = tone(0.1)
        encoder = codecs.PcmEncoder()
        payloads = encoder.payloads(pcm)
        decoder = codecs.PcmDecoder()
        for payload in payloads:
            decoder.push(payload.data)
        result = decoder.finish()
        np.testing.assert_array_equal(result, pcm)

    def test_payload_count(self):
        pcm = tone(1.0)
        payloads = codecs.PcmEncoder(samples_per_packet=480).payloads(pcm)
        self.assertEqual(len(payloads), 100)
        self.assertEqual(sum(payload.samples for payload in payloads), len(pcm))


class TestSbcCarrier(unittest.TestCase):
    def test_payloads_have_the_sbc_frame_length(self):
        params = SbcParams()
        payloads = codecs.SbcEncoder(params).payloads(tone())
        self.assertGreater(len(payloads), 0)
        for payload in payloads:
            frames = rtp.unpack_sbc(payload.data, params.frame_len(53))
            self.assertEqual(len(frames), 3)
            for frame in frames:
                self.assertEqual(len(frame), 126)

    def test_roundtrip_keeps_duration_and_signal(self):
        params = SbcParams()
        pcm = tone(0.2)
        encoder = codecs.SbcEncoder(params)
        decoder = codecs.SbcDecoder(params)
        for payload in encoder.payloads(pcm):
            decoder.push(payload.data)
        result = decoder.finish()
        self.assertEqual(result.shape[1], 2)
        self.assertLessEqual(abs(len(result) - len(pcm)), params.samples_per_frame)
        source_rms = float(np.sqrt(np.mean((pcm / 32768.0) ** 2)))
        result_rms = float(np.sqrt(np.mean((result / 32768.0) ** 2)))
        self.assertGreater(result_rms, 0.05)
        self.assertAlmostEqual(result_rms, source_rms, delta=0.05)

    def test_bitpool_changes_frame_length_and_bitrate(self):
        low = codecs.SbcEncoder(SbcParams(max_bitpool=24))
        high = codecs.SbcEncoder(SbcParams(max_bitpool=53))
        self.assertEqual(low.frame_len, 68)
        self.assertEqual(high.frame_len, 126)
        self.assertLess(low.frame_len, high.frame_len)


class TestAacFraming(unittest.TestCase):
    @staticmethod
    def adts_frame(payload, frequency_index=3, channels=2):
        frame_length = 7 + len(payload)
        header = bytearray(7)
        header[0] = 0xFF
        header[1] = 0xF1
        header[2] = (2 << 6) | (frequency_index << 2) | (channels >> 2)
        header[3] = ((channels & 0x03) << 6) | ((frame_length >> 11) & 0x03)
        header[4] = (frame_length >> 3) & 0xFF
        header[5] = ((frame_length & 0x07) << 5) | 0x1F
        header[6] = 0xFC
        return bytes(header) + payload

    def test_split_adts(self):
        first = self.adts_frame(bytes(range(50)))
        second = self.adts_frame(bytes(range(30)))
        frames = codecs.split_adts(first + second)
        self.assertEqual(frames, [first, second])

    def test_split_adts_rejects_garbage(self):
        with self.assertRaises(codecs.CodecError):
            codecs.split_adts(b"not an adts stream")


class TestEstimate(unittest.TestCase):
    def test_estimates(self):
        sbc = SbcParams()
        self.assertAlmostEqual(codecs.estimate_kbps("sbc", sbc=sbc), 378.0, places=3)
        self.assertAlmostEqual(
            codecs.estimate_kbps("pcm", sampling=48000, channels=2), 1536.0, places=3
        )
        self.assertAlmostEqual(
            codecs.estimate_kbps("aac", aac_bitrate=256), 256.0, places=3
        )
        self.assertAlmostEqual(codecs.estimate_kbps("aptx"), 352.0, places=3)
        self.assertAlmostEqual(
            codecs.estimate_kbps("ldac", sampling=48000, ldac_eqmid=0), 990.0, places=3
        )
        self.assertAlmostEqual(
            codecs.estimate_kbps("ldac", sampling=44100, ldac_eqmid=1), 606.0, places=3
        )

    def test_ldac_roundtrip(self):
        pcm = tone(0.1)
        encoder = codecs.make_encoder("ldac", sampling=48000, channels=2, ldac_eqmid=0)
        decoder = codecs.make_decoder("ldac", sampling=48000, channels=2)
        for payload in encoder.payloads(pcm):
            decoder.push(payload.data)
        result = decoder.finish()
        self.assertEqual(result.shape[1], 2)
        self.assertGreater(len(result), 0)

    def test_unknown_codec(self):
        with self.assertRaises(codecs.CodecError):
            codecs.make_encoder("bogus_codec")
        with self.assertRaises(codecs.CodecError):
            codecs.make_decoder("bogus_codec")


@unittest.skipIf(codecs.ffmpeg_path() is not None, "FFmpeg is installed")
class TestFfmpegMissing(unittest.TestCase):
    def test_aac_encoder_reports_the_missing_tool(self):
        with self.assertRaises(codecs.EncoderUnavailable):
            codecs.AacEncoder().payloads(tone(0.1))

    def test_aptx_encoder_reports_the_missing_tool(self):
        with self.assertRaises(codecs.EncoderUnavailable):
            codecs.AptxEncoder().payloads(tone(0.1))


if __name__ == "__main__":
    unittest.main()
