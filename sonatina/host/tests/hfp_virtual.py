"""HFP (headset microphone) on virtual controllers.

The virtual headset has an A2DP sink and a Bumble Hands-Free unit. Checked:
  1. Sonatina opens the hands-free channel itself (the headset waits), SLC completes.
  2. The headset opens it itself instead.
  3. An app opens the virtual microphone: A2DP is suspended, mSBC negotiated, eSCO up;
     the headset's 440 Hz "voice" reaches the virtual microphone at 48 kHz, Windows audio
     (1 kHz tone) reaches the headset as mSBC. The app closes the mic: A2DP streams again.
  4. Same with a CVSD-only headset (8 kHz PCM both ways).
  5. Hang up from the headset: music again while the app still holds the microphone.
  6. mSBC that never decodes: the headset is marked and the next call uses CVSD.
The virtual controller carries no SCO data, so the headset side of the link is emulated:
packets go straight into ScoAudio and what Sonatina sends is captured.
"""
import array, asyncio, ctypes, json, logging, math, os, struct, sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
import sonatina_host as sh
import sonatina_hfp as shfp
from bumble import avdtp, hci, hfp, rfcomm
from bumble.controller import Controller
from bumble.core import PhysicalTransport
from bumble.device import Device
from bumble.host import Host
from bumble.link import LocalLink
from bumble.transport.common import AsyncPipeSink

logging.basicConfig(level=logging.INFO, format="%(levelname).1s %(name)s: %(message)s")
logging.getLogger("bumble").setLevel(logging.ERROR)
if os.environ.get("SONT_DEBUG"):
    logging.getLogger("sonatina.hfp").setLevel(logging.DEBUG)
    for name in os.environ["SONT_DEBUG"].split(","):
        logging.getLogger(name).setLevel(logging.DEBUG)

ADDRS = ['F0:F1:F2:F3:F4:F5', '98:80:BB:41:8B:A7']


class FakeWatcher:
    apps = []

    def __init__(self, key):
        self.key = key

    def poll(self):
        return list(FakeWatcher.apps)


class FakeMicOut:
    last = None

    def __init__(self, key):
        self.key = key
        self.name = f'{key} Input (fake)'
        self.pcm = bytearray()
        FakeMicOut.last = self

    def start(self):
        pass

    def write(self, pcm):
        self.pcm += pcm

    def stop(self):
        pass


def freq_of(samples, rate):
    a = samples[len(samples) // 4:]
    crossings = sum(1 for x, y in zip(a, a[1:]) if x < 0 <= y)
    return crossings * rate / max(1, len(a)), max((abs(v) for v in a), default=0)


class Headset:
    """A2DP sink + HF unit on a virtual controller."""

    def __init__(self, device, msbc=True, opens_hfp=False):
        self.device = device
        self.msbc = msbc
        self.opens_hfp = opens_hfp
        self.hf = None
        self.sco_links = []
        self.cfg = hfp.HfConfiguration(
            supported_hf_features=[hfp.HfFeature.CODEC_NEGOTIATION, hfp.HfFeature.ESCO_S4_SETTINGS_SUPPORTED,
                                   hfp.HfFeature.REMOTE_VOLUME_CONTROL],
            supported_hf_indicators=[],
            supported_audio_codecs=[hfp.AudioCodec.CVSD] + ([hfp.AudioCodec.MSBC] if msbc else []))
        avdtp.Listener.for_device(device).on('connection', lambda server: server.add_sink(sh.ldac_config()))
        server = rfcomm.Server(device)
        channel = server.listen(self._on_dlc)
        device.sdp_service_records = {0x00010001: hfp.make_hf_sdp_records(0x00010001, channel, self.cfg)}
        device.on('sco_request', self._on_sco_request)
        device.on('sco_connection', self.sco_links.append)

    def _on_dlc(self, dlc):
        self.hf = hfp.HfProtocol(dlc, self.cfg)
        asyncio.get_running_loop().create_task(self._serve(self.hf))

    @staticmethod
    async def _serve(hf):
        await hf.initiate_slc()
        await hf.run()          # unsolicited results (+BCS) are handled here

    async def open_hfp(self, connection):
        record = await hfp.find_ag_sdp_record(connection)
        assert record, 'Sonatina has no AG record'
        channel = record[0]
        mux = await rfcomm.Client(connection).start()
        dlc = await mux.open_dlc(channel)
        self.hf = hfp.HfProtocol(dlc, self.cfg)
        await self.hf.initiate_slc()
        asyncio.get_running_loop().create_task(self.hf.run())

    def _on_sco_request(self, connection, link_type):
        params = (shfp.msbc_transparent_parameters() if self.hf and self.hf.active_codec == hfp.AudioCodec.MSBC
                  else hfp.ESCO_PARAMETERS[hfp.DefaultCodecParameters.ESCO_CVSD_S4])
        asyncio.get_running_loop().create_task(self.device.send_command(
            hci.HCI_Enhanced_Accept_Synchronous_Connection_Request_Command(
                bd_addr=connection.peer_address, **params.asdict())))


class LinkEmulator:
    """The headset end of the SCO link: sends 'voice' (440 Hz) at the real rate and
    collects what Sonatina sends back."""

    def __init__(self, host, lib):
        self.host = host
        self.lib = lib
        self.tx = bytearray()          # what Sonatina sent
        self.task = None
        self.garbage = False
        host.hfp.device.host.send_sco_sdu = lambda connection_handle, sdu: self.tx.extend(sdu)

    def start(self, codec):
        self.tx.clear()
        self.task = asyncio.get_running_loop().create_task(self._run(codec))

    def stop(self):
        if self.task:
            self.task.cancel()

    async def _run(self, codec):
        rate = 16000 if codec == 'msbc' else 8000
        enc = self.lib.sont_msbc_create() if codec == 'msbc' else None
        n = 0
        pending = bytearray()
        t0 = asyncio.get_running_loop().time()
        sent = 0
        chunk = 48
        bytes_per_s = 8000 if codec == 'msbc' else 16000
        while True:
            await asyncio.sleep(0.003)
            due = int((asyncio.get_running_loop().time() - t0) * bytes_per_s)
            while sent + chunk <= due:
                while len(pending) < chunk:
                    if codec == 'msbc':
                        pcm = struct.pack('<120h', *[int(8000 * math.sin(2 * math.pi * 440 * (n + i) / rate))
                                                    for i in range(120)])
                        n += 120
                        frame = ctypes.create_string_buffer(60)
                        self.lib.sont_msbc_encode(enc, pcm, frame)
                        pending += bytes([0x55, 0x38, 0x12]) * 20 if self.garbage else frame.raw
                    else:
                        pending += struct.pack('<24h', *[int(8000 * math.sin(2 * math.pi * 440 * (n + i) / rate))
                                                         for i in range(24)])
                        n += 24
                data = bytes(pending[:chunk])
                del pending[:chunk]
                audio = self.host.hfp.audio
                if audio is not None:
                    audio.on_packet(hci.HCI_SynchronousDataPacket(
                        connection_handle=self.host.hfp.sco.handle if self.host.hfp.sco else 0,
                        packet_status=0, data_total_length=len(data), data=data))
                sent += chunk

    def decode_tx(self, codec):
        if codec == 'cvsd':
            a = array.array('h'); a.frombytes(bytes(self.tx[: len(self.tx) // 2 * 2]))
            return a, 8000
        dec = self.lib.sont_msbc_create()
        a = array.array('h')
        data = bytes(self.tx)
        pcm = ctypes.create_string_buffer(240)
        frames = 0
        for i in range(0, len(data) - 59, 60):
            f = data[i:i + 60]
            assert f[0] == 0x01 and f[1] in shfp.H2_SYNC and f[2] == 0xAD, f[:4].hex()
            assert self.lib.sont_msbc_decode(dec, f[2:59], pcm) == 0
            a.frombytes(pcm.raw)
            frames += 1
        return a, 16000


async def scenario(msbc=True, opens_hfp=False, hangup=False, broken_msbc=False):
    print(f'--- headset msbc={msbc} opens_hfp={opens_hfp} hangup={hangup} broken_msbc={broken_msbc}', flush=True)
    link = LocalLink()
    ctrls = [Controller(f'C{i}', link=link, public_address=a) for i, a in enumerate(ADDRS)]
    devs = [Device(address=a, host=Host(c, AsyncPipeSink(c))) for a, c in zip(ADDRS, ctrls)]
    for d in devs:
        d.classic_enabled = True
    headset = Headset(devs[1], msbc=msbc, opens_hfp=opens_hfp)
    settings = sh.Settings(tone=True, address=ADDRS[1], headsets={ADDRS[1]: {'name': 'Air5'}},
                           hfp_mode='auto', mic_device='Hi-Fi Cable')
    host = sh.SonatinaHost(settings)
    host.hfp.make_watcher = FakeWatcher
    host.hfp.make_mic_out = FakeMicOut
    host.hfp.ENTER_AFTER_S = 0.2
    host.hfp.LEAVE_AFTER_S = 0.6
    host.attach(devs[0])
    for d in devs:
        await d.power_on()
    await host.after_power_on()
    host.paired = {ADDRS[1]}

    async def nosec(c):
        return None
    host._secure = nosec
    loop = asyncio.get_running_loop()
    errors = []
    loop.set_exception_handler(lambda l, ctx: errors.append(ctx.get('exception') or ctx.get('message')))
    tone = sh.ToneSource(host.ring)
    tone.start()
    FakeWatcher.apps = []
    runner = loop.create_task(host.hfp.run())
    emu = LinkEmulator(host, host.lib.lib)

    # A2DP + SLC
    if opens_hfp:
        conn = await devs[1].connect(devs[0].public_address, transport=PhysicalTransport.BR_EDR)
        for _ in range(30):
            await asyncio.sleep(0.1)
            if host.state == 'streaming':
                break
        await headset.open_hfp(conn)
    else:
        await devs[0].connect(ADDRS[1], transport=PhysicalTransport.BR_EDR)
    for _ in range(150):
        await asyncio.sleep(0.1)
        if host.hfp.ready:
            break
    assert host.state == 'streaming', host.state
    assert host.hfp.ready, 'SLC not up'
    print('SLC ready, A2DP', host.pump.encoder.name, flush=True)

    async def call(expected_codec, seconds=2.0):
        FakeWatcher.apps = ['Discord.exe']
        for _ in range(50):
            await asyncio.sleep(0.1)
            if host.hfp.in_call:
                break
        assert host.hfp.in_call, ('no call mode', host.hfp.last_error)
        assert host.hfp.codec == expected_codec, (host.hfp.codec, expected_codec)
        assert host.stream.state == avdtp.State.OPEN, host.stream.state     # A2DP suspended
        assert host.pump.task is None
        assert headset.sco_links, 'headset saw no SCO link'
        assert headset.hf.active_codec == (hfp.AudioCodec.MSBC if expected_codec == 'msbc' else hfp.AudioCodec.CVSD)
        print('summary:', host.summary(), flush=True)
        emu.start(expected_codec)
        await asyncio.sleep(seconds)
        snap = json.loads(json.dumps(host.snapshot()))      # what the dashboard gets
        mic = snap['mic']
        assert mic['in_call'] and mic['codec'] == expected_codec and mic['level'] > 0.2, mic
        assert snap['stream'] is None, snap['stream']                    # A2DP suspended
        assert ('mSBC' if expected_codec == 'msbc' else 'CVSD') in snap['summary'], snap['summary']

    await call('msbc' if msbc else 'cvsd')
    codec = host.hfp.codec
    if broken_msbc:
        emu.stop()
        emu.garbage = True
        emu.start(codec)
        for _ in range(60):
            await asyncio.sleep(0.1)
            if not host.hfp.in_call:
                break
        assert host.s.headsets[ADDRS[1]].get('msbc_broken'), 'mSBC breakage not detected'
        emu.stop()
        emu.garbage = False
        for _ in range(60):          # the app still holds the mic: call again, now CVSD
            await asyncio.sleep(0.1)
            if host.hfp.in_call:
                break
        assert host.hfp.in_call and host.hfp.codec == 'cvsd', (host.hfp.in_call, host.hfp.codec)
        codec = 'cvsd'
        emu.start(codec)
        await asyncio.sleep(2.0)
    stats = host.hfp.audio.stats()
    print('call:', stats, flush=True)
    emu.stop()
    # microphone: 440 Hz at 48 kHz stereo
    mic = FakeMicOut.last.pcm
    a = array.array('h'); a.frombytes(bytes(mic))
    left = a[0::2]
    f, p = freq_of(left, 48000)
    print(f'mic: {len(left) / 48000:.2f} s, {f:.0f} Hz, peak {p}', flush=True)
    assert len(left) > 48000 * 1.2 and 430 < f < 450 and p > 8000, (len(left), f, p)   # +6 dB gain
    # headset speaker: the 1 kHz Windows tone
    pcm, rate = emu.decode_tx(codec)
    f, p = freq_of(pcm, rate)
    print(f'to headset: {len(pcm) / rate:.2f} s of {codec}, {f:.0f} Hz, peak {p}', flush=True)
    assert len(pcm) > rate * 1.2 and 980 < f < 1020 and p > 3000, (len(pcm), f, p)

    if hangup:
        headset.hf.dlc.write(b'AT+CHUP\r')
        for _ in range(30):
            await asyncio.sleep(0.1)
            if not host.hfp.in_call:
                break
        assert not host.hfp.in_call and host.hfp.dismissed, 'hang up ignored'
        await asyncio.sleep(1.5)
        assert not host.hfp.in_call, 'call mode came back while dismissed'
        assert host.stream.state == avdtp.State.STREAMING
        FakeWatcher.apps = []
        await asyncio.sleep(1.0)
        assert not host.hfp.dismissed
    else:
        packets = host.pump.packets
        FakeWatcher.apps = []
        for _ in range(40):
            await asyncio.sleep(0.1)
            if not host.hfp.in_call:
                break
        assert not host.hfp.in_call, 'call mode stuck'
        await asyncio.sleep(1.0)
        assert host.stream.state == avdtp.State.STREAMING, host.stream.state
        assert host.pump.task is not None and host.pump.packets > packets, 'A2DP did not resume'
        assert not any(l.handle in devs[1].sco_links for l in headset.sco_links), 'SCO link left open'
    print('back to', host.summary(), flush=True)
    assert not errors, errors
    tone.stop()
    runner.cancel()
    for c in list(devs[0].connections.values()):
        await c.disconnect()
    await asyncio.sleep(0.3)


class FakeEndpoint:
    def getAddress(self): return 0x03
    def getMaxPacketSize(self): return 17


class FakeTransfer:
    """An isochronous OUT transfer on a full-speed bus: 1 packet per 1 ms frame, one
    continuous stream when the next transfer is already queued."""
    bus_free_at = 0.0
    lock = __import__('threading').Lock()

    def __init__(self):
        self.submitted = False

    def setIsochronous(self, address, payload, callback, iso_transfer_length_list):
        assert not self.submitted
        assert sum(iso_transfer_length_list) == len(payload) and max(iso_transfer_length_list) <= 17
        self.frames, self.callback = len(iso_transfer_length_list), callback

    def submit(self):
        import threading, time
        self.submitted = True
        with FakeTransfer.lock:
            start = max(time.monotonic(), FakeTransfer.bus_free_at)
            FakeTransfer.bus_free_at = start + self.frames / 1000
            done = FakeTransfer.bus_free_at
        def finish():
            self.submitted = False
            self.callback(self)
        threading.Timer(max(0, done - time.monotonic()), finish).start()

    def isSubmitted(self): return self.submitted
    def getStatus(self): return 0
    def cancel(self): pass


class FakeUsbSink:
    def __init__(self, loop):
        self.loop, self.isochronous_out, self.closed, self.other = loop, FakeEndpoint(), False, []
    class device:
        @staticmethod
        def getTransfer(iso_packets): return FakeTransfer()
    def on_packet(self, packet): self.other.append(packet)
    async def terminate(self): self.closed = True


async def lane_check():
    """333 SCO packets/s arriving in bursts of 8: the lane keeps up, ACL goes around it."""
    import time
    sink = FakeUsbSink(asyncio.get_running_loop())
    lane = shfp.ScoOutLane.install(sink)
    packet = bytes([hci.HCI_SYNCHRONOUS_DATA_PACKET, 2, 0, 48]) + bytes(48)
    t0 = time.monotonic()
    for burst in range(125):                     # 3 s
        for _ in range(8):
            sink.on_packet(packet)
        sink.on_packet(bytes([hci.HCI_ACL_DATA_PACKET, 1, 2, 3]))
        await asyncio.sleep(max(0, t0 + (burst + 1) * 0.024 - time.monotonic()))
    await asyncio.sleep(0.1)
    print('lane:', lane.stats(), 'pending', len(lane.pending), 'acl', len(sink.other))
    assert lane.dropped == 0 and lane.sent == 1000 and len(sink.other) == 125, lane.stats()
    await sink.terminate()
    assert lane.closed and sink.closed


class FakeEp:
    def __init__(self, address, mps): self.address, self.mps = address, mps
    def getAddress(self): return self.address
    def getMaxPacketSize(self): return self.mps
    def getAttributes(self): return 0x01


class FakeSetting(list):
    def __init__(self, number, alt, eps): super().__init__(eps); self.number, self.alt = number, alt
    def getNumber(self): return self.number
    def getAlternateSetting(self): return self.alt


class FakeInTransfer:
    """An iso IN transfer: completes 24 ms after submit; Bumble's callback resubmits it."""
    def __init__(self): self.submitted, self.callback, self.length = False, None, 0
    def setCallback(self, cb): self.callback = cb
    def getCallback(self): return self.callback
    def setIsochronous(self, address, length, callback, user_data):
        assert not self.submitted, 'altered while submitted'
        self.address, self.length, self.callback = address, length, callback
    def submit(self):
        import threading
        assert not self.submitted
        self.submitted = True
        def done():
            self.submitted = False
            self.callback(self)
        timer = threading.Timer(0.024, done)
        timer.daemon = True
        timer.start()
    def isSubmitted(self): return self.submitted


async def alt_check():
    """mSBC call: the SCO interface goes to alternate 1 and back to 2 for CVSD while
    the IN transfers keep running and are never cancelled."""
    loop = asyncio.get_running_loop()
    shfp.tune_usb_transport()                   # 24-packet SCO IN transfers, as in Sonatina
    settings = [FakeSetting(1, a, [FakeEp(0x83, m), FakeEp(0x03, m)]) for a, m in ((0, 0), (1, 9), (2, 17), (5, 49))]
    resubmits = []

    class Source:
        isochronous_in_transfers = [FakeInTransfer() for _ in range(4)]
        done = {}
        isochronous_in = settings[2][0]
        def transfer_callback(self, transfer):
            resubmits.append(transfer.length)
            transfer.submit()
    source = Source()
    source.transfer_callback = source.transfer_callback      # bound, like Bumble's

    class Handle:
        alt = 2
        def getDevice(self): return [[settings[:1], settings]]
        def setInterfaceAltSetting(self, interface, alt):
            assert interface == 1 and not any(t.isSubmitted() for t in source.isochronous_in_transfers)
            self.alt = alt

    class Transport:
        pass
    t = Transport()
    t.device, t.sco_interface, t.source = Handle(), settings[2], source
    sink = FakeUsbSink(loop)
    sink.isochronous_out = settings[2][1]
    t.sink = sink
    for tr in source.isochronous_in_transfers:
        tr.setIsochronous(0x83, 24 * 17, source.transfer_callback, 2)
        tr.submit()
    lane = shfp.ScoOutLane.install(sink)
    sw = shfp.UsbScoAlt(t)
    assert sorted(sw.endpoints) == [1, 2, 5] and sw.alt == 2
    await asyncio.sleep(0.1)
    assert await sw.switch(1, lane) and t.device.alt == 1 and sw.alt == 1
    assert lane.mps == 9 and sink.isochronous_out.getMaxPacketSize() == 9
    assert all(tr.length == 24 * 9 and tr.callback == source.transfer_callback for tr in source.isochronous_in_transfers)
    resubmits.clear()
    await asyncio.sleep(0.1)
    assert resubmits and set(resubmits) == {24 * 9}, set(resubmits)   # running again, new size
    assert await sw.switch(1, lane) and t.device.alt == 1   # already there: nothing to do
    assert await sw.switch(2, lane) and t.device.alt == 2 and lane.mps == 17
    assert not await sw.switch(3, lane)            # does not exist
    print('alt switch: ok (2 -> 1 -> 2, transfers parked and resubmitted)')
    for tr in source.isochronous_in_transfers:
        tr.setCallback(lambda tr: None)
    await asyncio.sleep(0.05)


def unit_checks():
    """mSBC parser: resync after junk, concealment for a lost frame."""
    lib = sh.CodecLib()
    got = []

    class Ring:
        def read(self, n):
            return bytes(n)
    audio = shfp.ScoAudio(lib.lib, 'msbc', Ring(), None, got.append, 0.0, lambda b: None)
    enc = lib.lib.sont_msbc_create()
    frames = []
    for k in range(10):
        f = ctypes.create_string_buffer(60)
        lib.lib.sont_msbc_encode(enc, bytes(240), f)
        frames.append(f.raw)
    # frame 3 lost (a lost SCO packet arrives as zeros), then 17 bytes of misalignment
    stream = b''.join(frames[:3]) + bytes(60) + b''.join(frames[4:6]) + b'\x55' * 17 + b''.join(frames[6:])
    for i in range(0, len(stream), 48):
        audio.on_packet(hci.HCI_SynchronousDataPacket(connection_handle=1, packet_status=0,
                                                      data_total_length=len(stream[i:i + 48]), data=stream[i:i + 48]))
    print('parser:', audio.stats())
    # zeros = link not sending (empty, concealed), junk = sync lost
    assert audio.frames_ok == 9 and audio.frames_empty == 1 and audio.sync_lost == 1, audio.stats()
    # capture device: the Hi-Fi Cable must not be taken for VB-CABLE (pops, 2026-11-20)
    apis = [{'name': 'MME'}, {'name': 'Windows WASAPI'}]
    devs = [{'name': 'CABLE Output (VB-Audio Virtual Cable)', 'max_input_channels': 2, 'hostapi': 0},
            {'name': 'Hi-Fi Cable Output (VB-Audio Hi-Fi Cable)', 'max_input_channels': 2, 'hostapi': 1},
            {'name': 'CABLE-A Output (VB-Audio Cable A)', 'max_input_channels': 2, 'hostapi': 1},
            {'name': 'CABLE Output (VB-Audio Virtual Cable)', 'max_input_channels': 2, 'hostapi': 1}]
    assert sh.find_capture_device('CABLE Output', devs, apis)[0] == 3
    assert sh.find_capture_device('Hi-Fi Cable Output', devs, apis)[0] == 1
    # microphone limiter: +6 dB, but a shout near full scale must not clip
    loud = shfp.ScoAudio(lib.lib, 'cvsd', Ring(), None, got.append, 6.0, lambda b: None)
    got.clear()
    for k in range(200):
        amp = 3000 if k < 100 else 30000
        pcm = struct.pack('<24h', *[int(amp * math.sin(2 * math.pi * 440 * (24 * k + i) / 8000)) for i in range(24)])
        loud.on_packet(hci.HCI_SynchronousDataPacket(connection_handle=1, packet_status=0, data_total_length=48, data=pcm))
    a = array.array('h'); a.frombytes(b''.join(got))
    quiet, shout = max(abs(v) for v in a[: len(a) // 2 - 2000]), max(abs(v) for v in a[len(a) // 2 + 2000:])
    print(f'limiter: quiet {quiet} (x{quiet / 3000:.2f}), shout {shout}, limited {loud.limited}')
    assert 5600 < quiet < 6400 and shout <= 29500 and loud.limited > 0
    assert shfp.transport_spec('usb:0BDA:8771', 2) == 'usb:0BDA:8771+sco=2'
    assert shfp.transport_spec('usb:0BDA:8771+sco=1', 2) == 'usb:0BDA:8771+sco=1'
    assert shfp.transport_spec('android-netsim', 2) == 'android-netsim'


async def main():
    unit_checks()
    await lane_check()
    await alt_check()
    await scenario(msbc=True)
    await scenario(msbc=False)
    await scenario(msbc=True, opens_hfp=True, hangup=True)
    await scenario(msbc=True, broken_msbc=True)

asyncio.run(main())
print('HFP OK')
