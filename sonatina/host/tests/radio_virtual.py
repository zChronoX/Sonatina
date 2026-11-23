"""Radio link quality (A4) and Wi-Fi-aware hopping (A5), 1.1.0.

  1. netsh output (English and Italian) parsed into networks; the PC on 5 GHz still
     avoids the 2.4 GHz channel of its own network, then the strongest neighbour.
  2. Wi-Fi channel -> Bluetooth channels -> the 10-byte AFH classification.
  3. On virtual controllers (whose controller is taught Read RSSI, Read Link Quality,
     Read AFH Channel Map and Set AFH Host Channel Classification): the map reaches the
     controller, the headset that connected to us is asked to swap roles, the level is
     "excellent" on a clean link, "weak" with audio lost, and the window gets JSON.
"""
import asyncio
import json
import logging
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
import sonatina_host as sh            # noqa: E402
import sonatina_radio as sr           # noqa: E402
from bumble import avdtp, hci         # noqa: E402
from bumble.controller import Controller   # noqa: E402
from bumble.core import PhysicalTransport  # noqa: E402
from bumble.device import Device      # noqa: E402
from bumble.host import Host          # noqa: E402
from bumble.link import LocalLink     # noqa: E402
from bumble.transport.common import AsyncPipeSink   # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(levelname).1s %(name)s: %(message)s")
logging.getLogger("bumble").setLevel(logging.ERROR)

INTERFACES_EN = """
There is 1 interface on the system:

    Name                   : Wi-Fi 2
    Description            : TP-Link Wireless USB Adapter
    GUID                   : 1234
    Physical address       : 00:11:22:33:44:55
    State                  : connected
    SSID                   : Casa
    BSSID                  : aa:bb:cc:00:00:02
    Network type           : Infrastructure
    Radio type             : 802.11ac
    Authentication         : WPA2-Personal
    Band                   : 5 GHz
    Channel                : 36
    Receive rate (Mbps)    : 433
    Signal                 : 92%
"""

NETWORKS_IT = """
Nome interfaccia: Wi-Fi 2
Attualmente sono visibili 3 reti.

SSID 1 : Casa
    Tipo di rete            : Infrastruttura
    Autenticazione          : WPA2-Personal
    Crittografia            : CCMP
    BSSID 1                 : aa:bb:cc:00:00:01
         Segnale            : 88%
         Tipo frequenza radio : 802.11n
         Banda              : 2,4 GHz
         Canale             : 6
    BSSID 2                 : aa:bb:cc:00:00:02
         Segnale            : 92%
         Tipo frequenza radio : 802.11ac
         Banda              : 5 GHz
         Canale             : 36

SSID 2 : Vicino
    Tipo di rete            : Infrastruttura
    BSSID 1                 : dd:ee:ff:00:00:01
         Segnale            : 60%
         Banda              : 2,4 GHz
         Canale             : 1

SSID 3 : Lontano
    BSSID 1                 : dd:ee:ff:00:00:02
         Segnale            : 20%
         Canale             : 11
"""

NOT_CONNECTED_IT = """
Nel sistema è presente 1 interfaccia:

    Nome                   : Wi-Fi 2
    Descrizione            : TP-Link Wireless USB Adapter
    Stato                  : disconnessa
"""


def parsing_checks():
    pc = sr.parse_interfaces(INTERFACES_EN)
    assert len(pc) == 1 and pc[0].ssid == 'Casa' and pc[0].channel == 36 and pc[0].band == '5', pc
    assert sr.parse_interfaces(NOT_CONNECTED_IT) == []
    nets = sr.parse_networks(NETWORKS_IT)
    got = [(n.ssid, n.channel, n.signal, n.band) for n in nets]
    assert got == [('Casa', 6, 88, '2.4'), ('Casa', 36, 92, '5'), ('Vicino', 1, 60, '2.4'),
                   ('Lontano', 11, 20, '2.4')], got
    for n in nets:
        n.pc = n.ssid == 'Casa'
    assert sr.wifi_channels_to_avoid(nets) == [6, 1]          # own network first, weak one skipped
    assert sr.wifi_channels_to_avoid(nets, manual=11) == [11]
    assert sr.wifi_channels_to_avoid([]) == []
    # channels that overlap one already chosen are not added
    near = [sr.WifiNetwork('a', 6, 90, '2.4'), sr.WifiNetwork('b', 7, 80, '2.4'), sr.WifiNetwork('c', 11, 70, '2.4')]
    assert sr.wifi_channels_to_avoid(near) == [6, 11]
    bad = sr.bad_bt_channels([6])
    assert bad == set(range(24, 47)), sorted(bad)
    assert sr.bad_bt_channels([1]) == set(range(0, 22))
    assert sr.bad_bt_channels([13]) == set(range(59, 79))       # 2472 MHz, clipped at 2480
    m = sr.classification(bad)
    s = sr.map_string(m)
    assert len(m) == 10 and s[:24] == '1' * 24 and s[24:47] == '0' * 23 and s[47:] == '1' * 32, s
    assert m[9] & 0x80 == 0                                       # bit 79 reserved
    # 2026-11-23: the Realtek left 2428-2435 MHz (and 2470-2473) out by itself -> Wi-Fi 5
    seen = ''.join('0' if k in (26, 27, 28, 29, 32, 33, 68, 69, 70, 71) else '1' for k in range(79))
    assert sr.suggest_wifi_channel(seen) == 5, sr.suggest_wifi_channel(seen)
    assert sr.suggest_wifi_channel('1' * 79) is None and sr.suggest_wifi_channel(None) is None
    assert sr.suggest_wifi_channel(''.join('0' if k in (40, 41) else '1' for k in range(79))) is None
    assert sr.suggest_wifi_channel(sr.map_string(sr.classification(sr.bad_bt_channels([11])))) == 11
    print('parsing and maps OK')


def teach(controller, state):
    """The virtual controller learns the four commands (it answers 'unknown' otherwise)."""
    def rssi(command):
        return hci.HCI_Read_RSSI_ReturnParameters(status=hci.HCI_SUCCESS, handle=command.handle,
                                                  rssi=state['rssi'])

    def quality(command):
        return sr._LinkQualityReturn(status=hci.HCI_SUCCESS, handle=command.handle, link_quality=state['lq'])

    def afh_map(command):
        return sr._AfhMapReturn(status=hci.HCI_SUCCESS, connection_handle=command.connection_handle,
                                afh_mode=1, afh_channel_map=state.get('map') or sr.classification(set()))

    def set_map(command):
        state['map'] = bytes(command.afh_host_channel_classification)
        state['sets'] += 1
        return hci.HCI_StatusReturnParameters(status=hci.HCI_SUCCESS)

    controller.on_hci_read_rssi_command = rssi
    controller.on_hci_read_link_quality_command = quality
    controller.on_hci_read_afh_channel_map_command = afh_map
    controller.on_hci_set_afh_host_channel_classification_command = set_map


async def wait_for(cond, seconds=15.0):
    for _ in range(int(seconds / 0.1)):
        if cond():
            return True
        await asyncio.sleep(0.1)
    return False


async def link_checks():
    addrs = ['F0:F1:F2:F3:F4:F5', '2C:BE:EE:4E:ED:2E']
    link = LocalLink()
    ctrls = [Controller(f'C{i}', link=link, public_address=a) for i, a in enumerate(addrs)]
    devs = [Device(address=a, host=Host(c, AsyncPipeSink(c))) for a, c in zip(addrs, ctrls)]
    for d in devs:
        d.classic_enabled = True
        await d.power_on()
    state = {'rssi': -2, 'lq': 250, 'map': None, 'sets': 0}
    teach(ctrls[0], state)
    avdtp.Listener.for_device(devs[1]).on('connection', lambda server: server.add_sink(sh.sbc_config(53)))
    settings = sh.Settings(tone=True, address=addrs[1], headsets={addrs[1]: {'name': 'CMF'}}, hfp_mode='off')
    host = sh.SonatinaHost(settings)
    host.attach(devs[0])
    await host.after_power_on()
    host.paired = {addrs[1]}

    async def nosec(c):
        return None
    host._secure = nosec
    nets = sr.parse_networks(NETWORKS_IT)
    for n in nets:
        n.pc = n.ssid == 'Casa'
    host.radio.read_wifi = lambda: (nets, None)
    tone = sh.ToneSource(host.ring)
    tone.start()
    radio = asyncio.create_task(host.radio.run())

    # the map reaches the controller before any headset
    assert await wait_for(lambda: state['map'] is not None), 'no AFH classification sent'
    assert sr.map_string(state['map']) == sr.map_string(sr.classification(sr.bad_bt_channels([6, 1])))
    print('AFH map sent: Wi-Fi 6 and 1 avoided,', sr.map_string(state['map']).count('0'), 'channels')

    # the headset connects to us: we are peripheral, then ask to be central
    await devs[1].connect(addrs[0], transport=PhysicalTransport.BR_EDR)
    assert await wait_for(lambda: host.state == 'streaming'), host.state
    assert await wait_for(lambda: host.radio.role == 'central', 10), host.radio.role
    print('role switched to central')

    # clean link: excellent
    assert await wait_for(lambda: host.radio.level == 'excellent' and host.radio.link_map is not None, 8), \
        (host.radio.level, host.radio.link_map)
    snap = json.loads(json.dumps(host.snapshot()))['radio']
    assert snap['rssi'] == -2 and snap['link_quality'] == 250, snap
    assert snap['wifi']['suggested'] is not None        # the link map shows our own avoidance
    assert snap['afh']['applied'] and snap['afh']['avoided'] == 45 and snap['wifi']['avoid'] == [6, 1], snap['afh']
    assert snap['afh']['link_map'] == snap['afh']['host_map'], 'link map'
    assert len(snap['history']['rssi']) == 120 and snap['history']['rssi'][-1] == -2, snap['history']['rssi'][-3:]
    print('clean link: level excellent, rssi -2 dB, lq 250')

    # audio lost with the signal as strong as usual: weak + interference
    assert await wait_for(lambda: len(host.radio.samples) >= 12, 15)
    host.pump.radio_dropped += 60            # 60 packets (~0.3-1 s of audio)
    assert await wait_for(lambda: host.radio.level == 'weak', 4), host.radio.level
    assert host.radio.interference(host.radio.clock())
    assert 'segnale debole' in host.summary() or 'weak signal' in host.summary(), host.summary()
    print('audio lost, strong signal: weak, interference suspected')

    # far away: RSSI 18 below its usual value, audio lost -> weak, not interference
    state['rssi'] = -20
    await asyncio.sleep(6.5)
    host.pump.radio_dropped += 60
    await asyncio.sleep(1.5)
    assert host.radio.level == 'weak' and not host.radio.interference(host.radio.clock())
    print('audio lost with RSSI far below its usual value: weak, no interference')
    # an adapter that always says link quality 0 (Realtek): hidden
    state['lq'] = 0
    assert await wait_for(lambda: not host.radio.lq_ok, 15) and host.radio.lq is None

    # Wi-Fi avoidance off: the full map again
    sets = state['sets']
    host.s.wifi_avoid = False
    host.radio.option_changed()
    assert await wait_for(lambda: state['sets'] > sets, 4)
    assert sr.map_string(state['map']) == '1' * 79
    # manual channel
    host.s.wifi_avoid, host.s.wifi_channel = True, 11
    host.radio.option_changed()
    assert await wait_for(lambda: sr.map_string(state['map']) == sr.map_string(sr.classification(sr.bad_bt_channels([11]))), 4)
    print('Wi-Fi avoidance off / manual channel 11: OK')

    print('stats:', host.stats_line())
    radio.cancel()
    tone.stop()
    for c in list(devs[0].connections.values()):
        await c.disconnect()
    await asyncio.sleep(0.3)


async def unsupported_checks():
    """A controller that knows none of the commands: no crash, just no numbers."""
    addrs = ['F0:F1:F2:F3:F4:F5', '2C:BE:EE:4E:ED:2E']
    link = LocalLink()
    ctrls = [Controller(f'C{i}', link=link, public_address=a) for i, a in enumerate(addrs)]
    devs = [Device(address=a, host=Host(c, AsyncPipeSink(c))) for a, c in zip(addrs, ctrls)]
    for d in devs:
        d.classic_enabled = True
        await d.power_on()
    avdtp.Listener.for_device(devs[1]).on('connection', lambda server: server.add_sink(sh.sbc_config(53)))
    settings = sh.Settings(tone=True, address=addrs[1], headsets={addrs[1]: {'name': 'CMF'}}, hfp_mode='off')
    host = sh.SonatinaHost(settings)
    host.attach(devs[0])
    await host.after_power_on()
    host.paired = {addrs[1]}

    async def nosec(c):
        return None
    host._secure = nosec
    host.radio.read_wifi = lambda: ([], 'no Wi-Fi adapter')
    tone = sh.ToneSource(host.ring)
    tone.start()
    radio = asyncio.create_task(host.radio.run())
    await devs[0].connect(addrs[1], transport=PhysicalTransport.BR_EDR)
    assert await wait_for(lambda: host.state == 'streaming')
    await asyncio.sleep(2.5)
    snap = json.loads(json.dumps(host.snapshot()))['radio']
    assert snap['rssi'] is None and not host.radio.rssi_ok and snap['level'] == 'excellent', snap
    assert snap['wifi']['error'] == 'no Wi-Fi adapter' and snap['wifi']['avoid'] == [], snap['wifi']
    print('adapter without the commands: OK')
    radio.cancel()
    tone.stop()
    for c in list(devs[0].connections.values()):
        await c.disconnect()
    await asyncio.sleep(0.3)


parsing_checks()
asyncio.run(link_checks())
asyncio.run(unsupported_checks())
print('RADIO OK')
