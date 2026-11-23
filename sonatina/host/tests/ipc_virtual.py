"""Dashboard channel: the tray's IPC server (own thread) driving a host on virtual controllers.

Checks the token handshake, the status push, and that commands from the dashboard reach the
Bluetooth host on its own loop (options, codec per headset, volume, errors)."""
import asyncio, json, logging, os, sys, tempfile
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
os.environ.setdefault('PYSTRAY_BACKEND', 'dummy')
import sonatina_host as sh
import sonatina_ipc
import sonatina_tray as st
from bumble import avdtp
from bumble.controller import Controller
from bumble.core import PhysicalTransport
from bumble.device import Device
from bumble.host import Host
from bumble.link import LocalLink
from bumble.transport.common import AsyncPipeSink

logging.basicConfig(level=logging.INFO, format="%(levelname).1s %(name)s: %(message)s")
logging.getLogger("bumble").setLevel(logging.ERROR)

ADDRS = ['F0:F1:F2:F3:F4:F5', '2C:BE:EE:4E:ED:2E']


async def handshake_checks(info_path):
    # wrong token: refused
    with open(info_path) as f:
        info = json.load(f)
    reader, writer = await asyncio.open_connection('127.0.0.1', info['port'])
    writer.write(b'{"hello": "nope"}\n')
    await writer.drain()
    reply = json.loads(await reader.readline())
    assert reply == {'ok': False, 'error': 'bad token'}, reply
    assert await reader.readline() == b'', 'connection left open after a bad token'
    writer.close()
    # garbage line after a good hello: ignored, the session goes on
    client = sonatina_ipc.IpcClient(info_path)
    hello = await client.connect()
    assert hello['ok'] and hello['version'] == sonatina_ipc.PROTOCOL, hello
    client.writer.write(b'not json\n')
    assert isinstance(await client.call('ping'), float)
    await client.close()
    print('handshake ok')


async def main():
    tmp = tempfile.mkdtemp()
    info_path = os.path.join(tmp, 'ipc.json')
    link = LocalLink()
    ctrls = [Controller(f'C{i}', link=link, public_address=a) for i, a in enumerate(ADDRS)]
    devs = [Device(address=a, host=Host(c, AsyncPipeSink(c))) for a, c in zip(ADDRS, ctrls)]
    for d in devs:
        d.classic_enabled = True
        await d.power_on()
    avdtp.Listener.for_device(devs[1]).on('connection', lambda server: (server.add_sink(sh.ldac_config()),
                                                                        server.add_sink(sh.sbc_config(53))))
    settings = sh.Settings(tone=True, address=ADDRS[1], headsets={ADDRS[1]: {'name': 'CMF'}}, hfp_mode='off')
    host = sh.SonatinaHost(settings)
    saved = []
    host.on_change = lambda s: saved.append(json.dumps(s.headsets))
    host.attach(devs[0])
    await host.after_power_on()
    host.paired = {ADDRS[1]}

    async def nosec(c):
        return None
    host._secure = nosec
    tone = sh.ToneSource(host.ring)
    tone.start()
    reconnect = asyncio.create_task(host.keep_connected())   # as in the app: reconnects after a codec change

    app = st.TrayApp(settings)
    app.host, app.loop, app.dongle_ok = host, asyncio.get_running_loop(), True
    server = sonatina_ipc.IpcServer(app, info_path, 'test')
    server.start_thread()                                  # its own thread, like in the tray
    assert server.port and os.path.exists(info_path)

    await handshake_checks(info_path)

    client = sonatina_ipc.IpcClient(info_path)
    await client.connect()
    await client.call('subscribe')
    first = await client.next_status()
    assert first['app']['key'] in ('idle', 'streaming') and first['host']['state'] == 'idle', first
    await devs[0].connect(ADDRS[1], transport=PhysicalTransport.BR_EDR)
    for _ in range(40):
        status = await asyncio.wait_for(client.next_status(), 5)
        if status['host']['stream']:
            break
    h = status['host']
    print('streaming:', h['stream'], '| headsets:', h['headsets'])
    assert h['stream']['name'] == 'LDAC' and h['stream']['codec'] == 'ldac'
    assert h['headsets'][0]['connected'] and h['headsets'][0]['supported'] == ['ldac', 'sbc'], h['headsets']
    assert status['app']['key'] == 'streaming'

    # commands go to the host loop
    assert await client.call('set_option', name='media_keys', value=False) is True
    assert host.s.media_keys is False
    await client.call('set_codec', address=ADDRS[1], codec='sbc')
    assert host.s.headsets[ADDRS[1]]['codec'] == 'sbc'
    for _ in range(60):                                    # renegotiates with SBC
        status = await asyncio.wait_for(client.next_status(), 5)
        if status['host']['stream'] and status['host']['stream']['name'] == 'SBC':
            break
    assert status['host']['stream']['name'] == 'SBC', status['host']['stream']
    try:
        await client.call('set_option', name='allow_unknown', value=True)
        raise AssertionError('unknown option accepted')
    except RuntimeError as error:
        assert 'option' in str(error)
    try:
        await client.call('make_coffee')
        raise AssertionError('unknown command accepted')
    except RuntimeError as error:
        assert 'unknown command' in str(error)
    # 1.1.0: automatic output, Wi-Fi channel, diagnostics
    for option in ('auto_output', 'pause_on_disconnect', 'wifi_avoid'):
        assert await client.call('set_option', name=option, value=False) is True
        assert getattr(host.s, option) is False, option
        await client.call('set_option', name=option, value=True)
    try:
        await client.call('set_fallback_output', id='{nope}')
        raise AssertionError('unknown output accepted')
    except RuntimeError as error:
        assert 'unknown output' in str(error)
    host.output.outputs = [sh.sonatina_output.Endpoint('{mon}', 'LG ULTRAGEAR (NVIDIA High Definition Audio)')]
    await client.call('set_fallback_output', id='{mon}')
    assert host.s.fallback_output == '{mon}' and host.s.fallback_output_name.startswith('LG'), host.s
    await client.call('set_wifi_channel', channel=6)
    assert host.s.wifi_channel == 6
    await client.call('set_wifi_channel', channel=0)
    os.environ['SONATINA_EXPORT_DIR'] = tmp
    path = await client.call('export_diagnostics')
    import zipfile
    names = zipfile.ZipFile(path).namelist()
    assert path.startswith(tmp) and 'status.json' in names and 'info.txt' in names, (path, names)
    status = json.loads(zipfile.ZipFile(path).read('status.json'))
    assert status['host']['output']['fallback_name'].startswith('LG'), status['host']['output']
    assert 'radio' in status['host'] and status['host']['options']['auto_output'] is True
    # 1.2.0: microphone cleaning, battery option
    await client.call('set_mic_denoise', mode='strong')
    assert host.s.mic_denoise == 'strong'
    try:
        await client.call('set_mic_denoise', mode='max')
        raise AssertionError('bad mode accepted')
    except RuntimeError as error:
        assert 'mode' in str(error)
    for option in ('mic_agc', 'mic_gate'):
        await client.call('set_option', name=option, value=not getattr(host.s, option))
    assert host.s.mic_agc is False and host.s.mic_gate is True
    menu = list(app.denoise_menu())
    assert [m.checked for m in menu] == [False, False, True], [m.checked for m in menu]
    snap = json.loads(json.dumps(host.snapshot()))
    assert snap['mic']['voice']['denoise'] == 'strong' and snap['mic']['voice']['available'], snap['mic']
    assert snap['options']['mic_gate'] is True and 'battery' in snap
    items = list(app.output_menu())                         # tray submenu
    assert len(items) == 2 and items[1].text.startswith('LG') and items[1].checked, [i.text for i in items]
    print('1.1.0 commands ok:', os.path.basename(path), names)
    json.dumps(host.snapshot())                            # plain JSON all the way down
    print('commands ok, settings saved', len(saved), 'times')

    await client.close()
    await asyncio.sleep(0.2)
    reconnect.cancel()
    tone.stop()
    for c in list(devs[0].connections.values()):
        await c.disconnect()
    await asyncio.sleep(0.3)

asyncio.run(main())
print('IPC OK')
