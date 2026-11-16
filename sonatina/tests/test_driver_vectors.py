import importlib.util
import os
import unittest

TESTS_DIR = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "driver", "tests")
)


def _load_generator():
    path = os.path.join(TESTS_DIR, "generate_vectors.py")
    spec = importlib.util.spec_from_file_location("sonatina_driver_vectors", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class TestDriverVectors(unittest.TestCase):
    def test_generates_at_least_twenty_vectors(self):
        gen = _load_generator()
        vectors, math_cases = gen.build_vectors()
        self.assertGreaterEqual(len(vectors), 36)
        self.assertGreaterEqual(len(math_cases), 5)
        self.assertEqual(vectors["sbc_cap_default"], "11150235")
        self.assertEqual(vectors["avdtp_discover_cmd"], "0001")
        self.assertEqual(vectors["avdtp_caps_sbc_default"], "01000706000011150235")
        self.assertEqual(vectors["d3_hs_01_discover_cmd"], "0001")
        self.assertEqual(vectors["d3_hs_05_start_cmd"], "400708")
        self.assertEqual(math_cases["sbc_default_bp53"], (126, 378000))


if __name__ == "__main__":
    unittest.main()
