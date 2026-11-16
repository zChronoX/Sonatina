import unittest

from sonatina.reg import Profile, Store
from sonatina.sbc import SbcParams

ROOT = r"Software\Sonatina-Test"


def delete_tree(hive, path):
    """Delete a registry key and everything below it."""
    import winreg

    try:
        key = winreg.OpenKey(hive, path, 0, winreg.KEY_READ | winreg.KEY_WRITE)
    except OSError:
        return
    try:
        while True:
            try:
                child = winreg.EnumKey(key, 0)
            except OSError:
                break
            delete_tree(hive, path + "\\" + child)
    finally:
        winreg.CloseKey(key)
    try:
        winreg.DeleteKey(hive, path)
    except OSError:
        pass


class TestRegistryStore(unittest.TestCase):
    def setUp(self):
        import winreg

        delete_tree(winreg.HKEY_CURRENT_USER, ROOT)
        self.store = Store("HKCU", ROOT)
        self.store.ensure()

    def tearDown(self):
        import winreg

        delete_tree(winreg.HKEY_CURRENT_USER, ROOT)

    def test_ensure_creates_the_tree_and_trigger(self):
        self.assertTrue(self.store.exists())
        self.assertEqual(self.store.read_trigger(), 0)
        self.assertEqual(self.store.list_addresses(), [])

    def test_profile_roundtrip(self):
        profile = Profile(
            device="001122AABBCC",
            codec="sbc",
            sbc=SbcParams(
                sampling=44100,
                channel_mode="stereo",
                blocks=8,
                subbands=4,
                allocation="snr",
                min_bitpool=4,
                max_bitpool=35,
            ),
            aac_channel_mode=0,
            aac_sampling=44100,
            aac_bitrate=192,
            ldac_sampling=96000,
            ldac_channel_mode=1,
            ldac_eqmid=1,
        )
        self.store.write_profile(profile)
        loaded = self.store.read_profile("001122aabbcc")
        self.assertIsNotNone(loaded)
        self.assertEqual(loaded, profile)
        self.assertEqual(self.store.list_addresses(), ["001122AABBCC"])

    def test_missing_profile_returns_none(self):
        self.assertIsNone(self.store.read_profile("AABBCCDDEEFF"))

    def test_delete_profile(self):
        self.store.write_profile(Profile(device="001122AABBCC"))
        self.assertTrue(self.store.delete_profile("001122AABBCC"))
        self.assertIsNone(self.store.read_profile("001122AABBCC"))
        self.assertEqual(self.store.list_addresses(), [])

    def test_capability_roundtrip(self):
        self.store.write_capability(
            "001122AABBCC",
            codecs=["sbc", "aac"],
            sbc=SbcParams(max_bitpool=53),
            aac_bitrate=320,
        )
        capability = self.store.read_capability("001122AABBCC")
        self.assertTrue(capability["Sbc"])
        self.assertTrue(capability["Aac"])
        self.assertFalse(capability["Aptx"])
        self.assertFalse(capability["Pcm"])
        self.assertEqual(capability["SbcMaxBitpool"], 53)
        self.assertEqual(capability["AacMaxBitrate"], 320)
        self.assertTrue(capability["UpdatedAt"])

    def test_current_roundtrip(self):
        self.store.write_current(
            "001122AABBCC",
            Codec="sbc",
            Bitrate=378,
            Packets=12,
            Errors=1,
            TxTimeUs=75,
            DelayMs=8,
            Opened=1,
            UpdatedAt="2026-11-15T18:00:00+00:00",
        )
        current = self.store.read_current("001122AABBCC")
        self.assertEqual(current["Codec"], "sbc")
        self.assertEqual(current["Bitrate"], 378)
        self.assertEqual(current["Packets"], 12)
        self.assertEqual(current["Errors"], 1)
        self.assertEqual(current["Opened"], 1)

    def test_trigger_increments(self):
        self.assertEqual(self.store.bump_trigger(), 1)
        self.assertEqual(self.store.bump_trigger(), 2)
        self.assertEqual(self.store.read_trigger(), 2)

    def test_invalid_profile_is_rejected_before_writing(self):
        profile = Profile(device="001122AABBCC", sbc=SbcParams(sampling=12345))
        with self.assertRaises(ValueError):
            self.store.write_profile(profile)

    def test_watch_path_points_at_control(self):
        self.assertEqual(self.store.watch_path, ROOT + r"\Control")


if __name__ == "__main__":
    unittest.main()
