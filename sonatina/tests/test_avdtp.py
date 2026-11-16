import unittest

from sonatina import avdtp
from sonatina.sbc import SbcParams


class TestMessage(unittest.TestCase):
    def test_roundtrip(self):
        message = avdtp.command(avdtp.SIG_GET_CAPABILITIES, b"\x01", transaction=7)
        decoded = avdtp.Message.decode(message.encode())
        self.assertEqual(decoded.transaction, 7)
        self.assertEqual(decoded.signal, avdtp.SIG_GET_CAPABILITIES)
        self.assertEqual(decoded.message_type, avdtp.MT_COMMAND)
        self.assertEqual(decoded.payload, b"\x01")

    def test_reject_payload(self):
        message = avdtp.command(avdtp.SIG_OPEN, b"\x01", transaction=3)
        reject = message.reject(avdtp.ERR_BAD_ACP_SEID)
        self.assertEqual(reject.message_type, avdtp.MT_REJECT)
        self.assertEqual(reject.payload, bytes([avdtp.ERR_BAD_ACP_SEID]))

    def test_rejects_rfa_bits_and_short_input(self):
        raw = bytearray(avdtp.command(avdtp.SIG_DISCOVER).encode())
        raw[1] |= 0x01
        with self.assertRaises(avdtp.AvdtpError):
            avdtp.Message.decode(bytes(raw))
        with self.assertRaises(avdtp.AvdtpError):
            avdtp.Message.decode(b"\x00")


class TestCapabilities(unittest.TestCase):
    def test_sbc_capability_roundtrip(self):
        params = SbcParams(
            sampling=44100,
            channel_mode="stereo",
            blocks=8,
            subbands=4,
            allocation="snr",
            min_bitpool=4,
            max_bitpool=35,
        )
        encoded = avdtp.encode_capabilities(avdtp.CodecCapability("sbc", sbc=params))
        parsed = avdtp.parse_capabilities(encoded)
        self.assertEqual(parsed.codec, "sbc")
        self.assertEqual(parsed.sbc, params)
        self.assertIn(avdtp.CAT_MEDIA_TRANSPORT, parsed.categories)
        self.assertIn(avdtp.CAT_MEDIA_CODEC, parsed.categories)

    def test_aac_capability_roundtrip(self):
        info = avdtp.AacInfo(sampling=44100, channels=2, bitrate=320, vbr=True)
        encoded = avdtp.encode_capabilities(avdtp.CodecCapability("aac", aac=info))
        parsed = avdtp.parse_capabilities(encoded)
        self.assertEqual(parsed.codec, "aac")
        self.assertEqual(parsed.aac.sampling, 44100)
        self.assertEqual(parsed.aac.channels, 2)
        self.assertEqual(parsed.aac.bitrate, 320)
        self.assertTrue(parsed.aac.vbr)

    def test_non_profile_codecs(self):
        for codec in ("pcm", "aptx"):
            with self.subTest(codec=codec):
                encoded = avdtp.encode_capabilities(avdtp.CodecCapability(codec))
                self.assertEqual(avdtp.parse_capabilities(encoded).codec, codec)

    def test_ldac_capability_roundtrip(self):
        info = avdtp.LdacInfo(sampling=96000, channel_mode="dual")
        encoded = avdtp.encode_capabilities(avdtp.CodecCapability("ldac", ldac=info))
        parsed = avdtp.parse_capabilities(encoded)
        self.assertEqual(parsed.codec, "ldac")
        self.assertIsNotNone(parsed.ldac)
        self.assertEqual(parsed.ldac.sampling, 96000)
        self.assertEqual(parsed.ldac.channel_mode, "dual")

    def test_sep_list(self):
        seps = [avdtp.SepInfo(seid=1), avdtp.SepInfo(seid=2, tsep=avdtp.TSEP_SRC)]
        parsed = avdtp.parse_sep_list(avdtp.encode_sep_list(seps))
        self.assertEqual(parsed, seps)


class TestSinkStateMachine(unittest.TestCase):
    def setUp(self):
        from sonatina.sink import SinkSep, SinkSession

        self.session = SinkSession(
            [
                SinkSep(
                    sep=avdtp.SepInfo(seid=1, tsep=avdtp.TSEP_SNK),
                    capability=avdtp.CodecCapability("sbc", sbc=SbcParams()),
                )
            ]
        )
        self.assert_sink = SinkSep

    def _send(self, message):
        reply = self.session.handle(message)
        self.assertIsNotNone(reply)
        return reply

    def test_discover_lists_one_sink_sep(self):
        reply = self._send(avdtp.command(avdtp.SIG_DISCOVER))
        self.assertEqual(reply.message_type, avdtp.MT_ACCEPT)
        seps = avdtp.parse_sep_list(reply.payload)
        self.assertEqual(len(seps), 1)
        self.assertEqual(seps[0].seid, 1)
        self.assertEqual(seps[0].tsep, avdtp.TSEP_SNK)

    def test_get_capabilities(self):
        reply = self._send(
            avdtp.command(avdtp.SIG_GET_CAPABILITIES, avdtp.single_seid(1))
        )
        self.assertEqual(avdtp.parse_capabilities(reply.payload).codec, "sbc")

    def test_get_capabilities_unknown_seid(self):
        reply = self._send(
            avdtp.command(avdtp.SIG_GET_CAPABILITIES, avdtp.single_seid(9))
        )
        self.assertEqual(reply.message_type, avdtp.MT_REJECT)
        self.assertEqual(reply.payload[0], avdtp.ERR_BAD_ACP_SEID)

    def test_full_handshake(self):
        self.session.handle(avdtp.command(avdtp.SIG_DISCOVER))
        capability = avdtp.CodecCapability("sbc", sbc=SbcParams(max_bitpool=53))
        configure = avdtp.command(
            avdtp.SIG_SET_CONFIGURATION, avdtp.set_configuration(2, 1, capability)
        )
        self.assertEqual(self._send(configure).message_type, avdtp.MT_ACCEPT)
        self.assertEqual(
            self._send(
                avdtp.command(avdtp.SIG_OPEN, avdtp.single_seid(1))
            ).message_type,
            avdtp.MT_ACCEPT,
        )
        self.assertEqual(
            self._send(
                avdtp.command(avdtp.SIG_START, avdtp.single_seid(1))
            ).message_type,
            avdtp.MT_ACCEPT,
        )
        self.assertTrue(self.session.started)

    def test_configuration_with_a_codec_the_sink_does_not_offer(self):
        capability = avdtp.CodecCapability("aac", aac=avdtp.AacInfo())
        reply = self._send(
            avdtp.command(
                avdtp.SIG_SET_CONFIGURATION, avdtp.set_configuration(2, 1, capability)
            )
        )
        self.assertEqual(reply.message_type, avdtp.MT_REJECT)
        self.assertEqual(reply.payload[0], avdtp.ERR_INVALID_CAPABILITIES)

    def test_configuration_with_bitpool_above_the_advertised_maximum(self):
        capability = avdtp.CodecCapability("sbc", sbc=SbcParams(max_bitpool=250))
        reply = self._send(
            avdtp.command(
                avdtp.SIG_SET_CONFIGURATION, avdtp.set_configuration(2, 1, capability)
            )
        )
        self.assertEqual(reply.message_type, avdtp.MT_REJECT)
        self.assertEqual(reply.payload[0], avdtp.ERR_INVALID_CAPABILITIES)

    def test_start_without_configuration(self):
        reply = self._send(avdtp.command(avdtp.SIG_START, avdtp.single_seid(1)))
        self.assertEqual(reply.message_type, avdtp.MT_REJECT)
        self.assertEqual(reply.payload[0], avdtp.ERR_SEP_NOT_IN_USE)

    def test_unknown_signal(self):
        reply = self._send(avdtp.command(0x3F))
        self.assertEqual(reply.message_type, avdtp.MT_REJECT)


class TestFindCodec(unittest.TestCase):
    def test_found_and_not_found(self):
        caps = [
            (avdtp.SepInfo(seid=1), avdtp.CapabilitySet(codec="sbc")),
            (avdtp.SepInfo(seid=2), avdtp.CapabilitySet(codec="aac")),
        ]
        self.assertEqual(avdtp.find_codec(caps, "aac")[1].codec, "aac")
        self.assertIsNone(avdtp.find_codec(caps, "aptx"))


if __name__ == "__main__":
    unittest.main()
