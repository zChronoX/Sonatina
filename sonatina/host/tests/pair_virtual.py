import asyncio, logging, os, sys, tempfile
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
import sonatina_host as sh
from bumble import a2dp, avdtp
from bumble.controller import Controller
from bumble.device import Device
from bumble.host import Host
from bumble.link import LocalLink
from bumble.keys import JsonKeyStore
from bumble.core import DeviceClass
from bumble.transport.common import AsyncPipeSink
logging.basicConfig(level=logging.INFO, format="%(levelname).1s %(name)s: %(message)s")
logging.getLogger("bumble").setLevel(logging.ERROR)

async def main():
    addrs=['F0:F1:F2:F3:F4:F5','2C:BE:EE:4E:ED:2E']
    link=LocalLink()
    ctrls=[Controller('C1',link=link,public_address=addrs[0]),Controller('C2',link=link,public_address=addrs[1])]
    devs=[Device(address=a,host=Host(c,AsyncPipeSink(c))) for a,c in zip(addrs,ctrls)]
    for d in devs: d.classic_enabled=True
    tmp=tempfile.mkdtemp()
    devs[0].keystore=JsonKeyStore(namespace='host', filename=os.path.join(tmp,'keys.json'))
    devs[1].name='Fake Headphones'
    devs[1].class_of_device=DeviceClass.pack_class_of_device(DeviceClass.AUDIO_SERVICE_CLASS, DeviceClass.AUDIO_VIDEO_MAJOR_DEVICE_CLASS, 6)
    await devs[0].power_on(); await devs[1].power_on()
    await devs[1].set_discoverable(True); await devs[1].set_connectable(True)
    rx=[]
    def on_avdtp(server):
        s=server.add_sink(sh.ldac_config()); s.on('rtp_packet', rx.append)
    avdtp.Listener.for_device(devs[1]).on('connection', on_avdtp)
    saved=[]
    from bumble.device import Connection
    from bumble.keys import PairingKeys
    async def fake_auth(self):
        k=PairingKeys(); k.link_key=PairingKeys.Key(value=bytes(16), authenticated=False)
        await self.device.keystore.update(str(self.peer_address), k)
    async def fake_enc(self, enable=True): return None
    Connection.authenticate=fake_auth; Connection.encrypt=fake_enc
    host=sh.SonatinaHost(sh.Settings(tone=True))
    host.on_change=lambda s: saved.append(dict(s.headsets))
    host.attach(devs[0]); await host.after_power_on()
    print('paired before', host.paired, 'preferred', repr(host.s.address))
    tone=sh.ToneSource(host.ring); tone.start()
    found={addrs[1]:'Fake Headphones'}
    ok=await host.pair(addrs[1], found.get(addrs[1])); print('pair ok', ok, 'paired', host.paired, 'preferred', host.s.address, 'saved', saved[-1:] )
    await asyncio.sleep(2); print('state', host.state, 'rx', len(rx), 'summary', host.summary())
    await host.forget(addrs[1]); await asyncio.sleep(0.5); print('after forget', host.paired, repr(host.s.address), host.state)
    tone.stop()
asyncio.run(main())
