"""Clock servo on virtual controllers: the capture clock runs 300 ppm fast (and then slow; real ones: 10-100 ppm);
the jitter buffer must stay near its target instead of growing (latency) or emptying."""
import asyncio, logging, os, sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
import sonatina_host as sh
from bumble import avdtp
from bumble.controller import Controller
from bumble.device import Device
from bumble.host import Host
from bumble.link import LocalLink
from bumble.core import PhysicalTransport
from bumble.transport.common import AsyncPipeSink
logging.basicConfig(level=logging.INFO, format="%(levelname).1s %(name)s: %(message)s")
logging.getLogger("bumble").setLevel(logging.ERROR)


async def run(speed, low_latency, seconds=20):
    addrs = ['F0:F1:F2:F3:F4:F5', 'AA:AA:AA:AA:AA:01']
    link = LocalLink()
    ctrls = [Controller(f'C{i}', link=link, public_address=a) for i, a in enumerate(addrs)]
    devs = [Device(address=a, host=Host(c, AsyncPipeSink(c))) for a, c in zip(addrs, ctrls)]
    for d in devs:
        d.classic_enabled = True
        await d.power_on()
    avdtp.Listener.for_device(devs[1]).on('connection', lambda server: server.add_sink(sh.ldac_config()))
    host = sh.SonatinaHost(sh.Settings(tone=True, address=addrs[1], low_latency=low_latency,
                                       headsets={addrs[1]: {'name': 'A'}}))
    host.attach(devs[0]); await host.after_power_on()
    host.paired = {addrs[1]}
    async def nosec(c): return None
    host._secure = nosec
    tone = sh.ToneSource(host.ring, speed=speed); tone.start()
    await devs[1].connect(devs[0].public_address, transport=PhysicalTransport.BR_EDR)
    levels = []
    for _ in range(seconds):
        await asyncio.sleep(1.0)
        p = host.pump
        levels.append(host.ring.level_ms())
        print(f'  level={levels[-1]:3d} ms margin={p.margin_ms} target={p.target_ms} '
              f'rate={(p.rate - 1) * 1e6:+.0f} ppm latency={host.latency_ms()} ms')
    p = host.pump
    tone.stop()
    for c in list(devs[1].connections.values()):
        await c.disconnect()
    await asyncio.sleep(0.3)
    # 300 ppm fast adds 6 ms in 20 s without the servo
    tail = levels[-4:]
    print(f'speed={speed} low={low_latency}: last levels {tail}, rate {(p.rate - 1) * 1e6:+.0f} ppm, '
          f'underruns {host.ring.underruns}, target {p.target_ms}')
    # instantaneous levels jump by up to a capture block (10 ms) between samples, more on a
    # busy CI runner ([13, 17, 10, 13] failed at 6 ms, 2026-11-20); the servo itself is
    # checked by the margin (lowest level) and the rate sign below
    assert max(tail) - min(tail) <= 10, tail
    assert abs(p.margin_ms - p.target_ms) <= 5, (p.margin_ms, p.target_ms)
    assert (p.rate > 1) == (speed > 1), p.rate   # the servo follows the capture clock
    return p.target_ms


async def main():
    # timing test: a busy machine (CI runner) can starve the pump once and make the buffer
    # jump; each case gets a second try before it counts as a failure
    for speed, low in ((1.0003, False), (0.9997, False), (1.0003, True)):
        try:
            await run(speed, low)
        except AssertionError as error:
            print(f'speed={speed} low={low}: {error!r}, trying again')
            await run(speed, low)
    print('LATENCY OK')

asyncio.run(main())
