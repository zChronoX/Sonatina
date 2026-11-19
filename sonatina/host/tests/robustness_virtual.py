"""Robustness pieces without hardware: Barrot spurious-byte splitter, sleep detection,
capture watchdog."""
import asyncio, logging, os, sys, time
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
import sonatina_host as sh
from bumble import hci
logging.basicConfig(level=logging.INFO, format="%(levelname).1s %(name)s: %(message)s")


def splitter_test():
    out = []
    sp = sh.StrictEventSplitter(out.append)
    cmd_complete = bytes([0x0E, 0x04, 0x01, 0x03, 0x0C, 0x00])           # Reset complete
    ext_features = bytes([0x0E, 0x0E, 0x01, 0x04, 0x10, 0x00, 0x01, 0x02]) + bytes(8)
    sp.feed(ext_features + b'\x00')        # Barrot: one extra byte in the same transfer
    sp.feed(b'\x00')                       # ... or in a transfer of its own
    sp.feed(cmd_complete + cmd_complete)   # two events in one transfer
    assert out == [ext_features, cmd_complete, cmd_complete], out
    assert sp.dropped == 2
    # wired into a fake USB source only for Barrot
    class Source:
        def __init__(self):
            self.splitters = {hci.HCI_EVENT_PACKET: type('S', (), {'emit': out.append})()}
    src = Source(); sh.apply_transport_quirks('usb:33FA:0012', src)
    assert isinstance(src.splitters[hci.HCI_EVENT_PACKET], sh.StrictEventSplitter)
    src = Source(); sh.apply_transport_quirks('usb:0BDA:8771', src)
    assert not isinstance(src.splitters[hci.HCI_EVENT_PACKET], sh.StrictEventSplitter)
    print('splitter OK')


async def suspend_test():
    watcher = asyncio.create_task(sh.watch_suspend(interval=0.05, gap=0.3))
    await asyncio.sleep(0.2)
    assert not watcher.done()
    time.sleep(0.6)                        # the whole process stalls, like a suspended PC
    try:
        await asyncio.wait_for(watcher, 1.0)
    except sh.SystemResumed as gap:
        print('suspend detected after', gap)
    else:
        raise AssertionError('suspend not detected')


def watchdog_test():
    cap = sh.CableCapture.__new__(sh.CableCapture)
    cap.ring = sh.PcmRing(); cap.device_name = 'CABLE Output'; cap.stream = None
    cap.running = False; cap.last_callback = 0.0; cap.retry_at = 0.0; cap.restarts = 0
    opened = []
    cap.sd = type('SD', (), {'_terminate': staticmethod(lambda: None),
                             '_initialize': staticmethod(lambda: None)})
    cap._open = lambda: opened.append('open')
    cap._close = lambda: opened.append('close')
    cap.stream = type('St', (), {'start': lambda self: opened.append('start')})()
    cap.start()
    opened.clear()
    cap.check()                            # fresh: nothing to do
    assert opened == []
    cap.last_callback -= 4.0               # 4 s without callbacks
    cap.check()
    assert opened == ['close', 'open', 'start'] and cap.restarts == 1, opened
    cap.last_callback -= 4.0
    cap.check()                            # rate limited: next try in 5 s
    assert cap.restarts == 1
    print('watchdog OK')


splitter_test()
asyncio.run(suspend_test())
watchdog_test()
print('ROBUSTNESS OK')
