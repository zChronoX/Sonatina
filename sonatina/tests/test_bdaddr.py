import unittest

from sonatina.bdaddr import colon, from_instance_id, is_valid, normalize


class TestBluetoothAddress(unittest.TestCase):
    def test_normalize_accepts_common_formats(self):
        self.assertEqual(normalize("001122aabbcc"), "001122AABBCC")
        self.assertEqual(normalize("00:11:22:AA:BB:CC"), "001122AABBCC")
        self.assertEqual(normalize("00-11-22-aa-bb-cc"), "001122AABBCC")
        self.assertEqual(normalize(" 00 11 22 AA BB CC "), "001122AABBCC")

    def test_normalize_rejects_invalid_input(self):
        for value in (
            "",
            "001122AABB",
            "001122AABBCCDD",
            "GG1122AABBCC",
            "0011-22AA-BBCC-",
        ):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    normalize(value)
        with self.assertRaises(ValueError):
            normalize(None)

    def test_is_valid(self):
        self.assertTrue(is_valid("9880bb418ba7"))
        self.assertFalse(is_valid("not-an-address"))

    def test_colon(self):
        self.assertEqual(colon("9880bb418ba7"), "98:80:BB:41:8B:A7")

    def test_from_instance_id(self):
        instance = (
            r"BTHENUM\{0000110b-0000-1000-8000-00805f9b34fb}_LOCALMFG&0b3b"
            r"\a&53731ab&0&9880BB418BA7_C00000000"
        )
        self.assertEqual(from_instance_id(instance), "9880BB418BA7")
        self.assertIsNone(from_instance_id("BTHENUM\\something-else"))
        self.assertIsNone(from_instance_id(""))


if __name__ == "__main__":
    unittest.main()
