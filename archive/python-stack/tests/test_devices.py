import unittest

from sonatina import devices


class TestFriendlyName(unittest.TestCase):
    def test_extracts_the_last_parenthesised_group(self):
        raw = r"@System32\drivers\btav.sys,#1;%1%0;(SOUNDPEATS Air5)"
        self.assertEqual(devices.friendly_name(raw), "SOUNDPEATS Air5")

    def test_plain_value_is_returned_trimmed(self):
        self.assertEqual(devices.friendly_name("  My Headset  "), "My Headset")

    def test_empty_input(self):
        self.assertEqual(devices.friendly_name(None), "")
        self.assertEqual(devices.friendly_name(""), "")


class TestEnumeration(unittest.TestCase):
    def test_enumeration_returns_valid_addresses(self):
        found = devices.enumerate_sinks()
        self.assertIsInstance(found, list)
        for device in found:
            self.assertRegex(device.address, r"^[0-9A-F]{12}$")
            self.assertIsInstance(device.name, str)
            self.assertIsInstance(device.driver, str)

    def test_find_roundtrip(self):
        found = devices.enumerate_sinks()
        if not found:
            self.skipTest("no Bluetooth audio devnode on this machine")
        device = devices.find(found[0].address.lower())
        self.assertIsNotNone(device)
        self.assertEqual(device.address, found[0].address)

    def test_find_missing_address(self):
        self.assertIsNone(devices.find("000000000000"))


if __name__ == "__main__":
    unittest.main()
