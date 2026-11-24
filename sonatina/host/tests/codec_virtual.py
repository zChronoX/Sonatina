"""N1 (1.3.0): codec change on the running link, no reconnection."""
import asyncio, logging, os, sys, time
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
import sonatina_host as sh
from bumble import avdtp
from bumble.controller import Controller
from bumble.device import Device
from bumble.host import Host
from bumble.link import LocalLink
from bumble.core import PhysicalTransport
from bumble.transport.common import AsyncPipeSink
from e2e_virtual import ldac_sink_caps, sbc_sink_caps, aac_sink_caps

logging.basicConfig(level=logging.INFO, format="%(levelname).1s %(name)s: %(message)s")
logging.getLogger("bumble").setLevel(logging.ERROR)


async def main():
    addrs = ['F0:F1:F2:F3:F4:F5', '2C:BE:EE:4E:ED:2E']
    link = LocalLink()
    ctrls = [Controller(f'C{i}', link=link, public_address=a) for i, a in enumerate(addrs)]
    devs = [Device(address=a, host=Host(c, AsyncPipeSink(c))) for a, c in zip(addrs, ctrls)]
    for d in devs:
        d.classic_enabled = True
        await d.power_on()
    rx = {'LDAC': [], 'AAC': [], 'SBC': []}
    def on_avdtp(server):
        for name, caps in (('LDAC', ldac_sink_caps()), ('AAC', aac_sink_caps()), ('SBC', sbc_sink_caps())):
            server.add_sink(caps).on('rtp_packet', rx[name].append)
    avdtp.Listener.for_device(devs[1]).on('connection', on_avdtp)
    host = sh.SonatinaHost(sh.Settings(codec='ldac', address=addrs[1], tone=True, allow_unknown=True))
    async def nosec(c): return None
    host._secure = nosec
    host.attach(devs[0])
    tone = sh.ToneSource(host.ring); tone.start()
    drops = []
    await asyncio.gather(devs[0].connect(addrs[1], transport=PhysicalTransport.BR_EDR),
                         devs[1].accept(devs[0].public_address))
    for _ in range(50):
        await asyncio.sleep(0.1)
        if host.state == 'streaming':
            break
    conn = host.connection
    conn.on('disconnection', lambda reason: drops.append(reason))
    await asyncio.sleep(1.0)
    assert host.pump.encoder.name == 'LDAC' and rx['LDAC'], 'LDAC first'

    async def change(codec, quality=None, expect='SBC'):
        t0 = time.perf_counter()
        await host.set_codec(addrs[1], codec, quality)
        took = time.perf_counter() - t0
        await asyncio.sleep(0.2)            # packets already queued for the radio arrive
        before = {k: len(v) for k, v in rx.items()}
        await asyncio.sleep(1.0)
        got = {k: len(v) - before[k] for k, v in rx.items()}
        print(f'-> {codec} {quality or ""}: state={host.state} enc={host.pump.encoder.name} '
              f'{host.pump.encoder.kbps} kbit/s, switch {took:.2f} s, new packets {got}', flush=True)
        assert host.state == 'streaming' and host.connection is conn and not drops, 'link must stay up'
        assert host.pump.encoder.name == expect and got[expect] > 20, (expect, got)
        assert all(n == 0 for k, n in got.items() if k != expect), ('old stream still sending', got)
        assert took < 2.0, took
        return took

    await change('sbc', expect='SBC')
    await change('aac', expect='AAC')
    await change('ldac', 'hq', expect='LDAC')
    assert host.pump.encoder.kbps == 990
    # LDAC quality: in place, no new stream
    stream = host.stream
    await change('ldac', 'mq', expect='LDAC')
    assert host.stream is stream and host.pump.encoder.kbps == 330, 'LDAC quality in place'
    # the switch is visible as our own reconnection: headset buttons ignored for a while
    assert host.output.own_reconnection()
    # turned off: the old way (reconnection)
    host.s.codec_switch_live = False
    await host.set_codec(addrs[1], 'sbc')
    await asyncio.sleep(0.5)
    assert drops, 'toggle off: reconnection expected'
    print('codec_switch_live off -> reconnection, as before')
    # a headset that refuses the new configuration: one retry, then the old way
    host.s.codec_switch_live = True
    await asyncio.sleep(0.5)
    await devs[0].connect(addrs[1], transport=PhysicalTransport.BR_EDR)
    for _ in range(50):
        await asyncio.sleep(0.1)
        if host.state == 'streaming':
            break
    assert host.pump.encoder.name == 'SBC', host.pump.encoder.name
    conn = host.connection; drops.clear()
    conn.on('disconnection', lambda reason: drops.append(reason))
    calls = []
    async def refuse(source, sink):
        calls.append(1)
        raise avdtp.ProtocolError(0x29, 'avdtp')
    host.avdtp.create_stream = refuse
    await host.set_codec(addrs[1], 'aac')
    await asyncio.sleep(0.5)
    assert len(calls) == 2 and drops, (calls, drops)
    print('refused by the headset -> retry, then reconnection')
    tone.stop()
    print('CODEC SWITCH OK')

asyncio.run(main())
