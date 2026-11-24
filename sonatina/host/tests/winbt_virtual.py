"""N2 (1.3.0): headphones held by Windows' own Bluetooth -> banner + balloon, once."""
import asyncio, logging, os, sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
import sonatina_host as sh
import sonatina_winbt as wb
from bumble.controller import Controller
from bumble.device import Device
from bumble.host import Host
from bumble.link import LocalLink
from bumble.transport.common import AsyncPipeSink
logging.basicConfig(level=logging.INFO, format="%(levelname).1s %(name)s: %(message)s")
logging.getLogger("bumble").setLevel(logging.ERROR)


async def unit():
    t = [0.0]
    held = {'v': True}
    calls = []
    def probe(addr):
        calls.append(addr)
        return held['v']
    c = wb.WindowsLinkCheck(probe=probe, clock=lambda: t[0])
    found = []
    c.on_found = found.append
    await c.after_failure('AA:BB:CC:DD:EE:FF', 2, True)
    assert not calls, 'not before 3 failures'
    await c.after_failure('AA:BB:CC:DD:EE:FF', 3, False)
    assert not calls, 'only page timeouts count'
    await c.after_failure('AA:BB:CC:DD:EE:FF', 3, True)
    assert c.conflict == 'AA:BB:CC:DD:EE:FF' and found == ['AA:BB:CC:DD:EE:FF']
    t[0] = 10
    await c.after_failure('AA:BB:CC:DD:EE:FF', 4, True)
    assert len(calls) == 1, 'at most once every 30 s'
    t[0] = 31
    await c.after_failure('AA:BB:CC:DD:EE:FF', 5, True)
    assert len(calls) == 2 and found == ['AA:BB:CC:DD:EE:FF'], 'one balloon per episode'
    held['v'] = False
    t[0] = 62
    await c.after_failure('AA:BB:CC:DD:EE:FF', 6, True)
    assert c.conflict is None, 'released by Windows: banner gone'
    held['v'] = None
    t[0] = 93
    await c.after_failure('AA:BB:CC:DD:EE:FF', 7, True)
    assert c.conflict is None, 'cannot tell: nothing shown'
    assert wb.address_to_int('2C:BE:EE:4E:ED:2E') == 0x2CBEEE4EED2E
    print('unit OK')


async def host_side():
    link = LocalLink()
    ctrl = Controller('C0', link=link, public_address='F0:F1:F2:F3:F4:F5')
    dev = Device(address='F0:F1:F2:F3:F4:F5', host=Host(ctrl, AsyncPipeSink(ctrl)))
    dev.classic_enabled = True
    await dev.power_on()
    target = '2C:BE:EE:4E:ED:2E'
    host = sh.SonatinaHost(sh.Settings(address=target, headsets={target: {'name': 'CMF Headphone Pro'}}))
    host.attach(dev)
    notices = []
    host.on_notice = notices.append
    host.winlink.probe = lambda a: True
    host.winlink.clock = lambda: 0.0
    for n in range(1, 4):
        await host.winlink.after_failure(target, n, True)
    snap = host.snapshot()
    assert snap['conflict'] == {'address': target, 'name': 'CMF Headphone Pro'}, snap['conflict']
    assert len(notices) == 1 and 'Windows' in notices[0], notices
    host.s.bt_conflict_check = False
    assert host.snapshot()['conflict'] is None, 'toggle off: no banner'
    host.s.bt_conflict_check = True
    host.winlink.reset()                       # what a connection does
    assert host.snapshot()['conflict'] is None
    print('host OK:', notices[0])


async def main():
    await unit()
    await host_side()
    print('WINBT OK')

asyncio.run(main())
