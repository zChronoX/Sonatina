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
async def main():
    addrs=['F0:F1:F2:F3:F4:F5','AA:AA:AA:AA:AA:01','BB:BB:BB:BB:BB:02']
    link=LocalLink()
    ctrls=[Controller(f'C{i}',link=link,public_address=a) for i,a in enumerate(addrs)]
    devs=[Device(address=a,host=Host(c,AsyncPipeSink(c))) for a,c in zip(addrs,ctrls)]
    for d in devs: d.classic_enabled=True; await d.power_on()
    for d in devs[1:]:
        avdtp.Listener.for_device(d).on('connection', lambda server: server.add_sink(sh.ldac_config()))
    host=sh.SonatinaHost(sh.Settings(tone=True, address=addrs[1], headsets={addrs[1]:{'name':'A'},addrs[2]:{'name':'B'}}))
    host.attach(devs[0]); await host.after_power_on()
    host.paired={addrs[1],addrs[2]}
    async def nosec(c): return None
    host._secure=nosec
    tone=sh.ToneSource(host.ring); tone.start()
    loop=asyncio.get_running_loop(); errors=[]
    loop.set_exception_handler(lambda l,ctx: errors.append(ctx.get('exception')))
    # B connects by itself while A (preferred) is off
    await devs[2].connect(devs[0].public_address, transport=PhysicalTransport.BR_EDR)
    await asyncio.sleep(1.5)
    print('state', host.state, 'peer', host.peer, 'preferred now', host.s.address, 'summary', host.summary())
    # B drops the link while streaming
    conn=list(devs[2].connections.values())[0]; await conn.disconnect(); await asyncio.sleep(0.5)
    print('after drop', host.state, 'unhandled errors', errors)
    tone.stop()
asyncio.run(main())
