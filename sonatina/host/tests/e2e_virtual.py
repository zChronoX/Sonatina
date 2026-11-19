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

def aac_sink_caps():
    I=a2dp.AacMediaCodecInformation
    return avdtp.MediaCodecCapabilities(media_type=avdtp.MediaType.AUDIO, media_codec_type=a2dp.CodecType.MPEG_2_4_AAC,
        media_codec_information=I(I.ObjectType.MPEG_2_AAC_LC|I.ObjectType.MPEG_4_AAC_LC,
                                  I.SamplingFrequency.SF_44100|I.SamplingFrequency.SF_48000,
                                  I.Channels.MONO|I.Channels.STEREO, 1, 320000))

def aptx_sink_caps(hd):
    info = (a2dp.VendorSpecificMediaCodecInformation(0xD7, 0x24, bytes([0x32, 0, 0, 0, 0])) if hd
            else a2dp.VendorSpecificMediaCodecInformation(0x4F, 0x01, bytes([0x32])))
    return avdtp.MediaCodecCapabilities(media_type=avdtp.MediaType.AUDIO, media_codec_type=a2dp.CodecType.NON_A2DP,
        media_codec_information=info)

# raw L2CAP media packets (aptX has no RTP header, so the parsed ones are meaningless)
RAW = []
_orig_on_packet = avdtp.LocalSink.on_avdtp_packet
def _on_packet(self, packet):
    RAW.append((self, bytes(packet)))
    try:
        _orig_on_packet(self, packet)
    except Exception:
        pass
avdtp.LocalSink.on_avdtp_packet = _on_packet

async def run(codec, with_ldac=True, with_aac=False, with_aptx=False, seconds=3.0):
    RAW.clear()
    addrs=['F0:F1:F2:F3:F4:F5','2C:BE:EE:4E:ED:2E']
    link=LocalLink()
    ctrls=[Controller('C1',link=link,public_address=addrs[0]),Controller('C2',link=link,public_address=addrs[1])]
    devs=[Device(address=a,host=Host(c,AsyncPipeSink(c))) for a,c in zip(addrs,ctrls)]
    for d in devs: d.classic_enabled=True
    await devs[0].power_on(); await devs[1].power_on()
    rx=[]
    aptx_sinks=set()
    def on_avdtp(server):
        if with_ldac:
            s=server.add_sink(ldac_sink_caps()); s.on('rtp_packet', rx.append)
        if with_aac:
            s3=server.add_sink(aac_sink_caps()); s3.on('rtp_packet', rx.append)
        if with_aptx:
            for hd in (True, False):
                s4=server.add_sink(aptx_sink_caps(hd)); s4.on('rtp_packet', rx.append); aptx_sinks.add(s4)
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
    has_rtp = getattr(p.encoder, 'rtp', True)   # aptX: no RTP header, seq/ts meaningless
    assert host.state == 'streaming' and len(rx) > 50 and (
        not has_rtp or seqs == list(range(seqs[0], seqs[0] + len(seqs)))), 'stream check failed'
    chain = sh.CODEC_CHAIN[sh.CODEC_CHAIN.index(codec):]
    if codec == 'ldac' and with_ldac:
        expected = 'LDAC'
    elif with_aptx and 'aptx_hd' in chain:
        expected = 'aptX HD'
    elif with_aptx and 'aptx' in chain:
        expected = 'aptX'
    elif codec in ('ldac', 'aac') and with_aac:
        expected = 'AAC'
    else:
        expected = 'SBC'
    assert p.encoder.name == expected, (p.encoder.name, expected)
    if expected.startswith('aptX'):
        check_aptx([pkt for sink, pkt in RAW if sink in aptx_sinks], expected == 'aptX HD',
                   p.encoder.per_packet * p.encoder.push_bytes)
    elif expected == 'AAC':
        assert sorted(set(b - a for a, b in zip(ts, ts[1:]))) == [1024], 'AAC timestamps'
        check_aac([bytes(x.payload) for x in rx])
    elif rx:
        open(f'rx_{codec}_{with_ldac}.bin','wb').write(b''.join(bytes(x.payload)[1:] for x in rx))


def check_aac(payloads):
    """LATM -> ADTS -> ffmpeg: the 1 kHz tone must come back."""
    import array, shutil, subprocess
    from bumble.codecs import AacAudioRtpPacket
    adts = b''.join(AacAudioRtpPacket.from_bytes(pl).to_adts() for pl in payloads)
    open('rx_aac.aac', 'wb').write(adts)
    if not shutil.which('ffmpeg'):
        print('ffmpeg missing: AAC decode check skipped'); return
    pcm = subprocess.run(['ffmpeg', '-v', 'error', '-i', 'rx_aac.aac', '-f', 's16le', '-ac', '1', '-'],
                         capture_output=True, check=True).stdout
    a = array.array('h'); a.frombytes(pcm)
    a = a[len(a) // 4:]                                  # skip the start
    rate = 48000
    crossings = sum(1 for x, y in zip(a, a[1:]) if x < 0 <= y)
    freq = crossings * rate / len(a)
    peak = max(abs(v) for v in a)
    print(f'AAC decoded: {len(a)/rate:.2f} s, {freq:.0f} Hz, peak {peak}')
    assert 980 < freq < 1020 and peak > 3000, (freq, peak)


def check_aptx(raw, hd, packet_bytes):
    """Raw packets -> ffmpeg aptX decoder: the 1 kHz tone must come back."""
    import array, shutil, subprocess
    header = 12 if hd else 0
    sizes = {len(r) for r in raw}
    assert sizes == {header + packet_bytes}, (sizes, header + packet_bytes)
    if hd:
        assert all(r[0] == 0x80 and r[1] == 96 for r in raw), 'aptX HD needs the RTP header'
    data = b''.join(r[header:] for r in raw)
    name = 'rx_aptx_hd.bin' if hd else 'rx_aptx.bin'
    open(name, 'wb').write(data)
    if not shutil.which('ffmpeg'):
        print('ffmpeg missing: aptX decode check skipped'); return
    pcm = subprocess.run(['ffmpeg', '-v', 'error', '-f', 'aptx_hd' if hd else 'aptx', '-sample_rate', '48000',
                          '-i', name, '-f', 's16le', '-ac', '1', '-'], capture_output=True, check=True).stdout
    a = array.array('h'); a.frombytes(pcm)
    a = a[len(a) // 4:]
    crossings = sum(1 for x, y in zip(a, a[1:]) if x < 0 <= y)
    freq = crossings * 48000 / len(a)
    peak = max(abs(v) for v in a)
    print(f'{"aptX HD" if hd else "aptX"} decoded: {len(a)/48000:.2f} s, {freq:.0f} Hz, peak {peak}, packet {header + packet_bytes} B')
    assert 980 < freq < 1020 and peak > 3000, (freq, peak)


def aac_mtu_check():
    """Loud noise at the CMF's MTU: no access unit may exceed the packet."""
    import random
    lib = sh.CodecLib()
    enc = sh.AacEncoder(lib, 320000, 679)
    rnd = random.Random(1)
    sizes = []
    for _ in range(400):
        pcm = struct.pack('<256h', *[rnd.randint(-32768, 32767) for _ in range(256)])
        sizes += [len(pl) for pl, n in enc.push(pcm)]
    print(f'AAC @MTU 679: {enc.kbps} kbit/s, peak {enc.peak} bit/s, max payload {max(sizes)} / {679-12}, dropped {enc.oversize}')
    assert enc.oversize == 0 and max(sizes) <= 679 - 12
    enc.close()

async def main():
    await run('ldac')
    await run('sbc')
    await run('ldac', with_ldac=False)
    await run('aac', with_aac=True)
    await run('ldac', with_ldac=False, with_aac=True)   # LDAC missing -> AAC
    await run('ldac', with_ldac=False, with_aac=True, with_aptx=True)   # LDAC missing -> aptX HD
    await run('aptx', with_aac=True, with_aptx=True)
    aac_mtu_check()
asyncio.run(main())
print('E2E OK')
