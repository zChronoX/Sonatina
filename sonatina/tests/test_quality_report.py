import importlib.util
import os
import unittest

TESTS_DIR = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "driver", "tests")
)


def _load_quality():
    path = os.path.join(TESTS_DIR, "quality_report.py")
    spec = importlib.util.spec_from_file_location("sonatina_quality_report", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class TestQualityReport(unittest.TestCase):
    def test_parse_and_format_markdown(self):
        mod = _load_quality()
        sample_output = (
            "T2_LOOPBACK codec=sbc seid=2 sampling=48000 init_bp=53 final_bp=41 "
            "abr_steps=2 packets=130 bytes=75540 tx_avg_us=1500 tx_max_us=30000 "
            "measured_kbps=274.6 expected_kbps=306.0 decoded_rms=0.345 trace_events=5\n"
            "sink: listening on 127.0.0.1:5115 (signalling) and 127.0.0.1:5114 (media), caps=pcm,sbc\n"
            "sink: codec=sbc packets=130 lost=0 errors=0 bytes=73980 bitrate_kbps=295.9 "
            "wire_kbps=303.8 duration=2.000 rms=0.483 out=temp.wav\n"
        )
        parsed = mod.parse_run_output("sbc_bp53_congested", 53, True, sample_output)
        self.assertEqual(parsed.init_bp, 53)
        self.assertEqual(parsed.final_bp, 41)
        self.assertEqual(parsed.abr_steps, 2)
        self.assertEqual(parsed.c_packets, 130)
        self.assertEqual(parsed.sink_packets, 130)
        self.assertEqual(parsed.lost, 0)
        self.assertAlmostEqual(parsed.decoded_rms, 0.345)

        md = mod.format_markdown([parsed])
        self.assertIn("# Sonatina T4 Quality Report", md)
        self.assertIn("| sbc_bp53_congested | 53 -> 41 | 2 | 130 / 130 | 0 | 0 |", md)


if __name__ == "__main__":
    unittest.main()
