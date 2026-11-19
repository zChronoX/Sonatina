import asyncio, logging, sys, time, struct
sys.path.insert(0, __import__('os').path.join(__import__('os').path.dirname(__file__), '..'))
import sonatina_host as sh
from bumble import a2dp, avdtp
from bumble.controller import Controller
from bumble.device import Device
from bumble.host import Host
from bumble.link import LocalLink
from bumble.transport.common import AsyncPipeSink
from bumble.core import PhysicalTransport

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname).1s %(name)s: %(message)s")
logging.getLogger("bumble").setLevel(logging.WARNING)

def ldac_sink_caps():
    return avdtp.MediaCodecCapabilities(media_type=avdtp.MediaType.AUDIO, media_codec_type=a2dp.CodecType.NON_A2DP,
        media_codec_information=a2dp.VendorSpecificMediaCodecInformation(0x12D, 0xAA, bytes([0x34, 0x07])))
def sbc_sink_caps():
    I=a2dp.SbcMediaCodecInformation
    return avdtp.MediaCodecCapabilities(media_type=avdtp.MediaType.AUDIO, media_codec_type=a2dp.CodecType.SBC,
        media_codec_information=I(I.SamplingFrequency.SF_44100|I.SamplingFrequency.SF_48000, I.ChannelMode(15), I.BlockLength(15), I.Subbands(3), I.AllocationMethod(3), 2, 39))

async def run(codec, with_ldac=True, seconds=3.0):
    addrs=['F0:F1:F2:F3:F4:F5','2C:BE:EE:4E:ED:2E']
    link=LocalLink()
    ctrls=[Controller('C1',link=link,public_address=addrs[0]),Controller('C2',link=link,public_address=addrs[1])]
    devs=[Device(address=a,host=Host(c,AsyncPipeSink(c))) for a,c in zip(addrs,ctrls)]
    for d in devs: d.classic_enabled=True
    await devs[0].power_on(); await devs[1].power_on()
    rx=[]
    def on_avdtp(server):
        if with_ldac:
            s=server.add_sink(ldac_sink_caps()); s.on('rtp_packet', rx.append)
        s2=server.add_sink(sbc_sink_caps()); s2.on('rtp_packet', rx.append)
    avdtp.Listener.for_device(devs[1]).on('connection', on_avdtp)
    st=sh.Settings(codec=codec, address=addrs[1], tone=True, allow_unknown=True)
    host=sh.SonatinaHost(st)
    async def nosecure(c): return None
    host._secure=nosecure
    host.attach(devs[0])
    tone=sh.ToneSource(host.ring); tone.start()
    print('connecting',flush=True)
    await asyncio.gather(devs[0].connect(addrs[1], transport=PhysicalTransport.BR_EDR), devs[1].accept(devs[0].public_address))
    print('connected',flush=True)
    for i in range(int(seconds*10)):
        await asyncio.sleep(0.1)
        if i%10==0: print('t',i, host.pump.packets if host.pump else None, len(rx), flush=True)
    tone.stop()
    p=host.pump
    sizes=[len(bytes(x.payload)) if hasattr(x,'payload') else 0 for x in rx]
    ts=[x.timestamp for x in rx]; seqs=[x.sequence_number for x in rx]
    print(f'codec={codec} ldac_sink={with_ldac} state={host.state} enc={p.encoder.name if p and p.encoder else None} rx={len(rx)} '
          f'pkts/s={len(rx)/seconds:.1f} max_payload={max(sizes) if sizes else 0} ts_steps={sorted(set(b-a for a,b in zip(ts,ts[1:])))[:5]} '
          f'seq_ok={seqs==list(range(seqs[0],seqs[0]+len(seqs))) if seqs else None} underruns={host.ring.underruns} skips={p.skips if p else None} max_late={p.max_late_ms if p else 0:.1f}')
    if rx: print('first payload hdr', bytes(rx[0].payload)[:4].hex())
    if rx:
        open(f'rx_{codec}_{with_ldac}.bin','wb').write(b''.join(bytes(x.payload)[1:] for x in rx))

async def main():
    await run('ldac')
    await run('sbc')
    await run('ldac', with_ldac=False)
asyncio.run(main())
