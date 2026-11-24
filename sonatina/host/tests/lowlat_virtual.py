"""M3 (1.3.0): low latency live (no reconnection) and only while chosen apps are open."""
import asyncio, logging, os, sys, types
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
import sonatina_host as sh
import sonatina_lowlat as ll
from bumble import avdtp
from bumble.controller import Controller
from bumble.device import Device
from bumble.host import Host
from bumble.link import LocalLink
from bumble.core import PhysicalTransport
from bumble.transport.common import AsyncPipeSink
from e2e_virtual import sbc_sink_caps

logging.basicConfig(level=logging.INFO, format="%(levelname).1s %(name)s: %(message)s")
logging.getLogger("bumble").setLevel(logging.ERROR)


async def decisions():
    t = [0.0]
    apps = {'v': []}
    applied = []
    s = sh.Settings()
    host = types.SimpleNamespace(s=s, hfp=None, apply_low_latency=lambda: applied.append(1))
    lo = ll.LowLatency(host, lister=lambda: apps['v'], clock=lambda: t[0])
    assert lo.mode == 'off' and not lo.active
    s.low_latency = True                         # settings saved by 1.2
    assert lo.mode == 'on'
    s.low_latency_mode, s.low_latency_apps = 'apps', ['RocketLeague.exe', 'vlc.exe']
    apps['v'] = ['chrome.exe', 'Discord.exe']
    await lo.tick()
    assert not lo.active and not applied
    apps['v'] = ['chrome.exe', 'rocketleague.exe']           # case does not matter
    t[0] = 3; await lo.tick()
    assert lo.active and lo.snapshot()['because'] == 'rocketleague.exe' and len(applied) == 1
    apps['v'] = ['chrome.exe']
    t[0] = 6; await lo.tick()
    assert lo.active, 'stays on for 10 s after the app closes'
    t[0] = 14; await lo.tick()
    assert not lo.active and len(applied) == 2
    snap = lo.snapshot()
    assert snap['running'] == ['chrome.exe'] and snap['apps'] == ['RocketLeague.exe', 'vlc.exe']
    s.low_latency_mode = 'on'
    await lo.update()
    assert lo.active
    s.low_latency_mode = 'off'
    await lo.update()
    assert not lo.active and len(applied) == 4
    print('decisions OK')


async def live():
    addrs = ['F0:F1:F2:F3:F4:F5', '2C:BE:EE:4E:ED:2E']
    link = LocalLink()
    ctrls = [Controller(f'C{i}', link=link, public_address=a) for i, a in enumerate(addrs)]
    devs = [Device(address=a, host=Host(c, AsyncPipeSink(c))) for a, c in zip(addrs, ctrls)]
    for d in devs:
        d.classic_enabled = True
        await d.power_on()
    rx = []
    avdtp.Listener.for_device(devs[1]).on('connection', lambda server: server.add_sink(sbc_sink_caps()).on('rtp_packet', rx.append))
    host = sh.SonatinaHost(sh.Settings(codec='sbc', address=addrs[1], tone=True, allow_unknown=True))
    async def nosec(c): return None
    host._secure = nosec
    host.lowlat.lister = lambda: ['game.exe']
    host.attach(devs[0])
    tone = sh.ToneSource(host.ring); tone.start()
    await asyncio.gather(devs[0].connect(addrs[1], transport=PhysicalTransport.BR_EDR),
                         devs[1].accept(devs[0].public_address))
    for _ in range(50):
        await asyncio.sleep(0.1)
        if host.state == 'streaming':
            break
    conn, pump = host.connection, host.pump
    assert pump.target_ms == sh.NORMAL_TARGET_MS
    await host.set_option('low_latency', True)
    assert host.connection is conn and host.pump is pump and pump.target_ms == sh.LOW_LATENCY_TARGET_MS
    await host.set_low_latency('apps', ['Game.exe', 'game.exe', ' '])
    assert host.s.low_latency_apps == ['Game.exe'], host.s.low_latency_apps
    await host.lowlat.tick()
    assert host.lowlat.active and pump.target_ms == sh.LOW_LATENCY_TARGET_MS
    host.lowlat.lister = lambda: []
    host.lowlat.clock = lambda: 1e9
    await host.lowlat.tick()
    assert not host.lowlat.active and pump.target_ms == sh.NORMAL_TARGET_MS
    before = len(rx)
    await asyncio.sleep(1.0)
    assert host.connection is conn and len(rx) - before > 20, 'still streaming'
    snap = host.snapshot()['low_latency']
    assert snap['mode'] == 'apps' and snap['apps'] == ['Game.exe'] and not snap['active']
    tone.stop()
    print('live OK')


async def main():
    await decisions()
    await live()
    print('LOW LATENCY OK')

asyncio.run(main())
