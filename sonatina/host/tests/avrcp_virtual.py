"""AVRCP on virtual controllers: buttons -> media keys, absolute volume both ways.

Headset A opens AVRCP by itself; headset B only listens, so Sonatina must open it."""
import asyncio, logging, os, sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
import sonatina_host as sh
import sonatina_avrcp as sa
from bumble import avdtp, avrcp
from bumble.controller import Controller
from bumble.device import Device
from bumble.host import Host
from bumble.link import LocalLink
from bumble.core import PhysicalTransport
from bumble.transport.common import AsyncPipeSink
logging.basicConfig(level=logging.INFO, format="%(levelname).1s %(name)s: %(message)s")
logging.getLogger("bumble").setLevel(logging.ERROR)

Key = sa.Key


class FakeVolume(sh.EndpointVolume):
    """Windows endpoint volume kept in memory."""
    def __init__(self, lib):
        super().__init__(lib, "CABLE")
        self.state = [False, 0.5]

    def _read(self):
        mute, scalar = self.state
        return mute, scalar, -20.0

    def _write(self, scalar, unmute):
        self.state[1] = scalar
        if unmute:
            self.state[0] = False


class HeadsetDelegate(avrcp.Delegate):
    def __init__(self):
        super().__init__(supported_events=[avrcp.EventId.VOLUME_CHANGED])
        self.volume = 127          # headsets often start loud: we must set it before unity gain
        self.protocol = None

    async def set_absolute_volume(self, volume):
        self.volume = volume
        self.protocol.notify_volume_changed(volume)   # real headsets confirm with a notification


async def until(cond, timeout=3.0):
    for _ in range(int(timeout / 0.05)):
        if cond():
            return True
        await asyncio.sleep(0.05)
    return cond()


async def main():
    addrs = ['F0:F1:F2:F3:F4:F5', 'AA:AA:AA:AA:AA:01', 'BB:BB:BB:BB:BB:02']
    link = LocalLink()
    ctrls = [Controller(f'C{i}', link=link, public_address=a) for i, a in enumerate(addrs)]
    devs = [Device(address=a, host=Host(c, AsyncPipeSink(c))) for a, c in zip(addrs, ctrls)]
    for d in devs:
        d.classic_enabled = True
        await d.power_on()
    for d in devs[1:]:
        avdtp.Listener.for_device(d).on('connection', lambda server: server.add_sink(sh.ldac_config()))

    keys = []
    sa.send_key = keys.append
    host = sh.SonatinaHost(sh.Settings(tone=True, address=addrs[1],
                                       headsets={addrs[1]: {'name': 'A'}, addrs[2]: {'name': 'B'}}))
    host.volume = FakeVolume(host.lib)
    host.volume.poll()
    host.attach(devs[0]); await host.after_power_on()
    host.paired = {addrs[1], addrs[2]}
    async def nosec(c): return None
    host._secure = nosec
    tone = sh.ToneSource(host.ring); tone.start()
    loop = asyncio.get_running_loop(); errors = []
    loop.set_exception_handler(lambda l, ctx: errors.append(ctx.get('exception') or ctx.get('message')))
    poller = asyncio.create_task(host.follow_volume())

    # ---- headset A: opens AVRCP itself
    hd = HeadsetDelegate(); hp = avrcp.Protocol(hd); hd.protocol = hp
    conn = await devs[1].connect(devs[0].public_address, transport=PhysicalTransport.BR_EDR)
    await asyncio.sleep(0.3)
    await hp.connect(conn)
    assert await until(lambda: host.remote.absolute), "absolute volume not enabled"
    print('A: absolute on, headset volume', hd.volume, 'gain target', host.volume.target)
    assert hd.volume == 64 and host.volume.target == sh.EndpointVolume.UNITY

    host.volume.state[1] = 0.25                      # Windows slider moved
    assert await until(lambda: hd.volume == 32), f"headset volume {hd.volume}"
    for v in (40, 50, 60):                           # drag: echoes must not fight it
        host.volume.state[1] = v / 127
        await asyncio.sleep(0.12)
    assert await until(lambda: hd.volume == 60)
    await asyncio.sleep(0.5)
    assert abs(host.volume.state[1] - 60 / 127) < 1e-6, host.volume.state
    print('A: Windows -> headset OK, volume', hd.volume)

    await asyncio.sleep(1.6)                         # past the echo window
    hd.volume = 100; hp.notify_volume_changed(100)   # headset volume buttons
    assert await until(lambda: abs(host.volume.state[1] - 100 / 127) < 1e-6), host.volume.state
    await asyncio.sleep(0.5)
    assert hd.volume == 100
    print('A: headset -> Windows OK, slider', round(host.volume.state[1], 3))

    for k in (Key.PLAY, Key.FORWARD, Key.BACKWARD, Key.VOLUME_UP):
        await hp.send_key_event(k, True); await hp.send_key_event(k, False)
    await asyncio.sleep(0.2)
    print('A: keys', [hex(k) for k in keys])
    assert keys == [sa.VK_MEDIA_PLAY_PAUSE, sa.VK_MEDIA_NEXT_TRACK, sa.VK_MEDIA_PREV_TRACK,
                    sa.VK_VOLUME_UP]

    status = await asyncio.wait_for(anext(hp.monitor_playback_status()), 3)
    print('A: play status', status)
    assert status == avrcp.PlayStatus.PLAYING

    # Windows media sessions available: real commands, real status
    class FakeMedia:
        available = True
        status = 'paused'
        def __init__(self): self.cmds = []
        def command(self, name): self.cmds.append(name); return True
    media = FakeMedia(); host.remote.media = media
    await asyncio.sleep(1.0)                         # status poller: PLAYING -> PAUSED
    status = await asyncio.wait_for(anext(hp.monitor_playback_status()), 3)
    assert status == avrcp.PlayStatus.PAUSED, status
    for k in (Key.PLAY, Key.FORWARD, Key.BACKWARD, Key.PAUSE):
        await hp.send_key_event(k, True); await hp.send_key_event(k, False)
        await asyncio.sleep(0.5)
    print('A: media commands', media.cmds)
    assert media.cmds == ['play', 'skip_next', 'skip_previous', 'pause'], media.cmds
    host.remote.media = None

    await conn.disconnect(); await asyncio.sleep(0.5)
    assert not host.remote.absolute and not host.volume.hardware

    # ---- headset B: only listens, Sonatina opens AVRCP
    hd2 = HeadsetDelegate(); hp2 = avrcp.Protocol(hd2); hd2.protocol = hp2
    hp2.listen(devs[2])
    host.volume.state[1] = 0.5
    await devs[2].connect(devs[0].public_address, transport=PhysicalTransport.BR_EDR)
    assert await until(lambda: host.remote.absolute, 6), "B: absolute volume not enabled"
    print('B: absolute on, headset volume', hd2.volume)
    assert hd2.volume == 64
    before = list(keys)
    await hp2.send_key_event(Key.PLAY, True); await hp2.send_key_event(Key.PLAY, False)
    await asyncio.sleep(0.2)
    assert keys == before, keys                      # auto-play right after connecting: ignored

    poller.cancel(); tone.stop()
    print('errors', errors)
    assert not errors
    print('AVRCP OK')

asyncio.run(main())
