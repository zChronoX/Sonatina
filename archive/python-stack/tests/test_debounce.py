import unittest

from sonatina.watcher import ApplyPolicy


class FakeClock:
    def __init__(self, start: float = 0.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class TestApplyPolicy(unittest.TestCase):
    def setUp(self):
        self.clock = FakeClock()
        self.policy = ApplyPolicy(debounce_ms=500, cooldown_ms=3000, clock=self.clock)

    def test_debounce_waits_for_the_timer(self):
        self.policy.notify()
        self.assertIsNone(self.policy.poll())
        self.clock.advance(0.4)
        self.assertIsNone(self.policy.poll())
        self.clock.advance(0.2)
        self.assertEqual(self.policy.poll(), "apply")

    def test_burst_extends_the_debounce(self):
        self.policy.notify()
        self.clock.advance(0.3)
        self.policy.notify()
        self.clock.advance(0.4)
        self.assertIsNone(self.policy.poll())
        self.clock.advance(0.2)
        self.assertEqual(self.policy.poll(), "apply")

    def test_second_apply_inside_cooldown_is_skipped(self):
        self.policy.notify()
        self.clock.advance(0.6)
        self.assertEqual(self.policy.poll(), "apply")
        self.policy.notify()
        self.clock.advance(0.6)
        self.assertEqual(self.policy.poll(), "cooldown")

    def test_apply_after_cooldown(self):
        self.policy.notify()
        self.clock.advance(0.6)
        self.assertEqual(self.policy.poll(), "apply")
        self.clock.advance(3.1)
        self.policy.notify()
        self.clock.advance(0.6)
        self.assertEqual(self.policy.poll(), "apply")

    def test_skip_consumes_the_pending_request(self):
        self.policy.notify()
        self.clock.advance(0.6)
        self.assertEqual(self.policy.poll(), "apply")
        self.policy.notify()
        self.clock.advance(0.6)
        self.assertEqual(self.policy.poll(), "cooldown")
        self.assertIsNone(self.policy.poll())

    def test_elapsed_since_apply(self):
        self.assertIsNone(self.policy.elapsed_since_apply())
        self.policy.notify()
        self.clock.advance(0.6)
        self.policy.poll()
        self.assertAlmostEqual(self.policy.elapsed_since_apply(), 0.0)
        self.clock.advance(2.0)
        self.assertAlmostEqual(self.policy.elapsed_since_apply(), 2.0)

    def test_zero_debounce_and_cooldown(self):
        policy = ApplyPolicy(debounce_ms=0, cooldown_ms=0, clock=self.clock)
        policy.notify()
        self.assertEqual(policy.poll(), "apply")
        policy.notify()
        self.assertEqual(policy.poll(), "apply")


if __name__ == "__main__":
    unittest.main()
