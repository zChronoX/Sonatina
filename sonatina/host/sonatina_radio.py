"""Radio link quality (1.1.0, A4) and Wi-Fi-aware channel hopping (A5).

A4: once a second, while a headset is connected: the controller's RSSI and link quality
(HCI Read RSSI / Read Link Quality), the audio waiting for the radio and the audio
dropped because the radio did not keep up (MediaPump). From the last seconds a simple
level for the window and the tooltip: excellent / good / weak. Audio lost with a strong
signal means interference, not distance: the window then suggests a 2.4 GHz Wi-Fi nearby
(2026-11-21: a phone on 2.4 GHz Wi-Fi next to the adapter cost LDAC 990 ~3 s a minute).

A5: Bluetooth hops over 79 channels of 1 MHz (2402-2480 MHz); a 2.4 GHz Wi-Fi network
occupies 22 of them. Adaptive Frequency Hopping (AFH) lets the host mark channels as bad
(HCI Set AFH Host Channel Classification) and the central of the link then stays away.
The channels come from the Wi-Fi networks the PC sees (`netsh wlan`, even when the PC is
on Ethernet or on 5 GHz: phones on the same network use its 2.4 GHz channel), or from a
channel chosen by hand. Since only the central decides the hopping map, Sonatina asks to
become central when a headset connected to it (Switch Role).

BR/EDR RSSI is, per the spec, the distance in dB from the controller's "golden receive
power range" (0 = inside it); some controllers report dBm, and the Realtek RTL8761B does
neither consistently (-18..-31 for a while, then -1..-3 on the same link, 2026-11-22).
So since 1.2.1 the level comes only from how the audio gets through (audio waiting for
the radio, audio lost, LDAC lowered by the adapter); RSSI is shown as a raw number and
only compared with itself: audio lost while it is near its best of the last minutes
means interference, not distance. Link quality is hidden when the adapter reports 0.

Without a Wi-Fi adapter on the PC the channels the controller already leaves out of the
link by itself (Read AFH Channel Map) point at the Wi-Fi channel to avoid: the window
suggests it (2026-11-23: 2428-2435 MHz left out -> Wi-Fi channel 5).
"""

from __future__ import annotations

import asyncio
import collections
import dataclasses
import logging
import re
import subprocess
import sys
import time

from bumble import hci

log = logging.getLogger("sonatina")

BT_CHANNELS = 79
MIN_FREE = 20            # the spec's minimum number of used channels
MAX_WIFI = 2             # at most two Wi-Fi channels avoided (44 of 79 Bluetooth channels)
WIFI_MIN_SIGNAL = 35     # % in netsh: weaker networks hardly disturb


# ----------------------------------------------------------------------------- HCI commands
@dataclasses.dataclass
class _LinkQualityReturn(hci.HCI_StatusReturnParameters):
    handle: int = dataclasses.field(metadata=hci.metadata(2))
    link_quality: int = dataclasses.field(metadata=hci.metadata(1))


@dataclasses.dataclass
class _AfhMapReturn(hci.HCI_StatusReturnParameters):
    connection_handle: int = dataclasses.field(metadata=hci.metadata(2))
    afh_mode: int = dataclasses.field(metadata=hci.metadata(1))
    afh_channel_map: bytes = dataclasses.field(metadata=hci.metadata(10))


@hci.HCI_SyncCommand.sync_command(_LinkQualityReturn)
@dataclasses.dataclass
class HCI_Read_Link_Quality_Command(hci.HCI_SyncCommand[_LinkQualityReturn]):
    """Bluetooth spec 7.5.3 (0-255, vendor specific)."""
    handle: int = dataclasses.field(metadata=hci.metadata(2))


@hci.HCI_SyncCommand.sync_command(_AfhMapReturn)
@dataclasses.dataclass
class HCI_Read_AFH_Channel_Map_Command(hci.HCI_SyncCommand[_AfhMapReturn]):
    """Bluetooth spec 7.5.5: the hopping map the link really uses."""
    connection_handle: int = dataclasses.field(metadata=hci.metadata(2))


@hci.HCI_SyncCommand.sync_command(hci.HCI_StatusReturnParameters)
@dataclasses.dataclass
class HCI_Set_AFH_Host_Channel_Classification_Command(hci.HCI_SyncCommand[hci.HCI_StatusReturnParameters]):
    """Bluetooth spec 7.3.46: bit n = 0 marks channel n as bad, 1 = unknown."""
    afh_host_channel_classification: bytes = dataclasses.field(metadata=hci.metadata(10))


# ----------------------------------------------------------------------------- Wi-Fi
@dataclasses.dataclass
class WifiNetwork:
    ssid: str
    channel: int
    signal: int          # %, as netsh shows it
    band: str            # "2.4" | "5" | "6"
    pc: bool = False     # the network the PC is connected to


_KEYS = {
    "ssid": ("ssid",),
    "channel": ("channel", "canale", "canal", "kanal"),
    "signal": ("signal", "segnale", "señal", "signal (%)", "signalqualität"),
    "band": ("band", "banda", "bande"),
    "state": ("state", "stato", "estado", "état", "status"),
}
_CONNECTED = ("connected", "connessa", "connesso", "conectado", "connecté", "verbunden")


def _split(line: str) -> tuple[str, str] | None:
    if ":" not in line:
        return None
    key, value = line.split(":", 1)
    return key.strip().lower(), value.strip()


def _kind(key: str) -> str | None:
    base = re.sub(r"\s+\d+$", "", key)     # "ssid 1", "bssid 2"
    for kind, names in _KEYS.items():
        if base in names:
            return kind
    if base == "bssid":
        return "bssid"
    return None


def _band(text: str, channel: int) -> str:
    t = text.replace(",", ".")
    if "2.4" in t:
        return "2.4"
    if t.startswith("6"):
        return "6"
    if t.startswith("5"):
        return "5"
    return "2.4" if 1 <= channel <= 14 else "5"


def _int(text: str) -> int | None:
    m = re.search(r"\d+", text)
    return int(m.group()) if m else None


def parse_networks(text: str) -> list[WifiNetwork]:
    """`netsh wlan show networks mode=bssid`, in any of the usual Windows languages:
    one entry per BSSID (a dual-band router shows up once per band)."""
    found: list[WifiNetwork] = []
    ssid = ""
    current: dict | None = None

    def close():
        if current and current.get("channel"):
            ch = current["channel"]
            found.append(WifiNetwork(ssid, ch, current.get("signal") or 0,
                                     _band(current.get("band", ""), ch)))

    for line in text.splitlines():
        kv = _split(line)
        if kv is None:
            continue
        kind = _kind(kv[0])
        if kind == "ssid" and kv[0] != "ssid":       # "SSID 1 : name" starts a network
            close()
            current = None
            ssid = kv[1]
        elif kind == "bssid":
            close()
            current = {}
        elif current is not None and kind in ("channel", "signal"):
            current[kind] = _int(kv[1])
        elif current is not None and kind == "band":
            current["band"] = kv[1]
    close()
    return found


def parse_interfaces(text: str) -> list[WifiNetwork]:
    """`netsh wlan show interfaces`: the network each connected Wi-Fi adapter is on."""
    found: list[WifiNetwork] = []
    block: dict = {}

    def close():
        if block.get("connected") and block.get("channel"):
            ch = block["channel"]
            found.append(WifiNetwork(block.get("ssid", ""), ch, block.get("signal") or 0,
                                     _band(block.get("band", ""), ch), pc=True))

    for line in text.splitlines():
        kv = _split(line)
        if kv is None:
            continue
        key, value = kv
        kind = _kind(key)
        if key in ("name", "nome", "nombre", "nom"):
            close()
            block = {}
        elif kind == "state":
            block["connected"] = value.lower() in _CONNECTED
        elif kind == "ssid" and key == "ssid":
            block["ssid"] = value
        elif kind in ("channel", "signal"):
            block[kind] = _int(value)
        elif kind == "band":
            block["band"] = value
    close()
    return found


def read_wifi() -> tuple[list[WifiNetwork], str | None]:
    """(networks, error). Runs netsh (Windows only, ~0.1 s): call it off the event loop."""
    if sys.platform != "win32":
        return [], "no Wi-Fi on this system"
    out = {}
    for name, args in (("interfaces", ["netsh", "wlan", "show", "interfaces"]),
                       ("networks", ["netsh", "wlan", "show", "networks", "mode=bssid"])):
        try:
            done = subprocess.run(args, capture_output=True, timeout=10,
                                  creationflags=0x08000000)    # CREATE_NO_WINDOW
            raw = done.stdout
        except Exception as error:
            return [], f"netsh: {error!r}"
        for codec in ("utf-8", "cp850", "cp1252"):
            try:
                out[name] = raw.decode(codec)
                break
            except UnicodeDecodeError:
                continue
        else:
            out[name] = raw.decode("utf-8", "replace")
    pc = parse_interfaces(out["interfaces"])
    nets = parse_networks(out["networks"])
    if not pc and not nets:
        # the reason is the networks command's own message: no Wi-Fi adapter, or Windows 11
        # 24H2+ asking for the location permission before showing networks to netsh
        lines = [x.strip() for x in out["networks"].splitlines() if x.strip()]
        return [], lines[0] if lines else "no Wi-Fi adapter"
    pc_ssids = {n.ssid for n in pc}
    for n in nets:
        n.pc = n.ssid in pc_ssids
    # the PC's own link first (it may be on a channel the scan does not list)
    return pc + [n for n in nets if (n.ssid, n.channel) not in {(p.ssid, p.channel) for p in pc}], None


def wifi_channels_to_avoid(networks: list[WifiNetwork], manual: int = 0) -> list[int]:
    """2.4 GHz Wi-Fi channels to keep Bluetooth away from, most important first: the
    network the PC is on (its 2.4 GHz side), then the strongest others."""
    if manual:
        return [manual]
    candidates = [n for n in networks if n.band == "2.4" and 1 <= n.channel <= 14
                  and (n.pc or n.signal >= WIFI_MIN_SIGNAL)]
    candidates.sort(key=lambda n: (not n.pc, -n.signal))
    chosen: list[int] = []
    for n in candidates:
        if n.channel in chosen:
            continue
        if any(abs(n.channel - c) < 3 for c in chosen):    # overlaps one already chosen
            continue
        trial = chosen + [n.channel]
        if BT_CHANNELS - len(bad_bt_channels(trial)) < MIN_FREE:
            break
        chosen = trial
        if len(chosen) >= MAX_WIFI:
            break
    return chosen


def bad_bt_channels(wifi_channels: list[int]) -> set[int]:
    """Bluetooth channels (0-78, 2402 + k MHz) inside the 22 MHz of each Wi-Fi channel."""
    bad: set[int] = set()
    for c in wifi_channels:
        center = 2484 if c == 14 else 2407 + 5 * c
        for k in range(BT_CHANNELS):
            if abs(2402 + k - center) <= 11:
                bad.add(k)
    return bad


def classification(bad: set[int]) -> bytes:
    """The 10-byte map: bit k = 1 for usable channels (bit 79 reserved, 0)."""
    value = 0
    for k in range(BT_CHANNELS):
        if k not in bad:
            value |= 1 << k
    return value.to_bytes(10, "little")


def map_string(channel_map: bytes) -> str:
    value = int.from_bytes(channel_map, "little")
    return "".join("1" if value >> k & 1 else "0" for k in range(BT_CHANNELS))


# ----------------------------------------------------------------------------- monitor
LEVELS = ("none", "weak", "good", "excellent")


def suggest_wifi_channel(link_map: str | None) -> int | None:
    """The Wi-Fi channel behind the largest block of Bluetooth channels the controller left
    out of the link by itself (at least 4 channels; gaps of up to 3 are bridged)."""
    if not link_map:
        return None
    bad = [k for k, c in enumerate(link_map) if c == "0"]
    if not bad:
        return None
    clusters, current = [], [bad[0]]
    for k in bad[1:]:
        if k - current[-1] <= 4:
            current.append(k)
        else:
            clusters.append(current)
            current = [k]
    clusters.append(current)
    best = max(clusters, key=lambda c: (c[-1] - c[0], len(c)))
    if best[-1] - best[0] < 3:
        return None
    center = 2402 + (best[0] + best[-1]) / 2
    return max(1, min(13, round((center - 2407) / 5)))


@dataclasses.dataclass
class Sample:
    t: float
    rssi: int | None
    lq: int | None
    queued_ms: float
    lost_ms: float
    kbps: int | None


class RadioMonitor:
    SAMPLE_S = 1.0
    HISTORY_S = 600
    STEP_S = 5               # history sent to the window: one point per 5 s
    WIFI_REFRESH_S = 60.0
    AFH_READ_S = 10.0

    def __init__(self, host, wifi_reader=None, clock=time.monotonic) -> None:
        self.host = host
        self.s = host.s
        self.clock = clock
        self.read_wifi = wifi_reader or read_wifi
        self.samples: collections.deque[Sample] = collections.deque(maxlen=int(self.HISTORY_S / self.SAMPLE_S))
        self.losses: collections.deque[tuple[float, float]] = collections.deque()   # (t, ms) for an hour
        self.rssi: int | None = None
        self.lq: int | None = None
        self.lq_zero = 0                       # link quality readings of 0 in a row
        self.rssi_ok = True
        self.lq_ok = True
        self.afh_read_ok = True
        self.level = "none"
        self.logged_level = "none"
        self.next_log = 0.0
        self.handle: int | None = None
        self.dropped_seen = 0
        self.role: str | None = None
        self.role_tried: int | None = None
        # A5
        self.networks: list[WifiNetwork] = []
        self.wifi_error: str | None = None
        self.avoid: list[int] = []              # Wi-Fi channels avoided
        self.host_map: bytes = classification(set())
        self.sent_map: bytes | None = None      # last classification the controller took
        self.afh_error: str | None = None
        self.link_map: str | None = None        # what the link really uses
        self.last_link_map: str | None = None   # kept after a disconnection (suggestion)
        self.next_wifi = 0.0
        self.next_afh_read = 0.0
        self.wake = asyncio.Event()

    def option_changed(self) -> None:
        self.next_wifi = 0.0
        self.wake.set()

    # -- loop ---------------------------------------------------------------------
    async def run(self) -> None:
        while True:
            try:
                await self.tick()
            except asyncio.CancelledError:
                raise
            except Exception as error:
                log.warning("radio monitor: %r", error)
            try:
                await asyncio.wait_for(self.wake.wait(), self.SAMPLE_S)
            except asyncio.TimeoutError:
                pass
            self.wake.clear()

    async def tick(self) -> None:
        now = self.clock()
        if now >= self.next_wifi:
            self.next_wifi = now + self.WIFI_REFRESH_S
            await self.refresh_wifi()
        await self.apply_map()
        connection = self.host.connection
        busy = getattr(getattr(self.host, "hfp", None), "busy", None)
        if busy is not None and busy.locked():
            return          # entering or leaving a call: leave the HCI channel alone
        if connection is None:
            self.handle = None
            self.rssi = self.lq = None
            self.link_map = None
            self.role = None
            self._set_level("none", now)
            return
        if connection.handle != self.handle:          # a new link
            self.handle = connection.handle
            self.dropped_seen = self.host.pump.radio_dropped if self.host.pump else 0
            self.role_tried = None
            self.next_afh_read = now + 3.0
        self.role = "central" if connection.role == hci.Role.CENTRAL else "peripheral"
        await self._maybe_become_central(connection)
        await self._sample(connection, now)
        if now >= self.next_afh_read and self.afh_read_ok:
            self.next_afh_read = now + self.AFH_READ_S
            await self._read_link_map(connection)

    async def _command(self, command):
        return await self.host.device.send_sync_command(command)

    async def _sample(self, connection, now: float) -> None:
        if self.rssi_ok:
            try:
                rp = await asyncio.wait_for(self._command(hci.HCI_Read_RSSI_Command(handle=connection.handle)), 2.0)
                self.rssi = int(rp.rssi)
            except Exception as error:
                self.rssi = None
                if _unsupported(error):
                    self.rssi_ok = False
                    log.info("radio: this adapter does not report RSSI (%s)", _describe(error))
        if self.lq_ok:
            try:
                rp = await asyncio.wait_for(self._command(HCI_Read_Link_Quality_Command(handle=connection.handle)), 2.0)
                self.lq = int(rp.link_quality)
                self.lq_zero = self.lq_zero + 1 if self.lq == 0 else 0
                if self.lq_zero >= 10:      # the Realtek always says 0: not a measurement
                    self.lq_ok, self.lq = False, None
                    log.info("radio: this adapter does not report the link quality (always 0)")
            except Exception as error:
                self.lq = None
                if _unsupported(error):
                    self.lq_ok = False
        pump = self.host.pump
        queued_ms = lost_ms = 0.0
        kbps = None
        if pump is not None:
            queued_ms = pump.queued_s() * 1000
            dropped = pump.radio_dropped
            lost_ms = max(0, dropped - self.dropped_seen) * pump.packet_s * 1000
            self.dropped_seen = dropped
            if pump.encoder is not None:
                kbps = getattr(pump.encoder, "kbps", None)
        self.samples.append(Sample(now, self.rssi, self.lq, queued_ms, lost_ms, kbps))
        if lost_ms > 0:
            self.losses.append((now, lost_ms))
        while self.losses and self.losses[0][0] < now - 3600:
            self.losses.popleft()
        self._set_level(self._grade(now), now)

    def _window(self, now: float, seconds: float) -> list[Sample]:
        return [x for x in self.samples if x.t > now - seconds]

    def lost_ms(self, now: float, seconds: float) -> float:
        return sum(ms for t, ms in self.losses if t > now - seconds)

    def _ldac_lowered(self) -> bool:
        pump = self.host.pump
        adapter = getattr(pump, "adapter", None) if pump is not None else None
        encoder = getattr(pump, "encoder", None) if pump is not None else None
        try:
            return adapter is not None and encoder is not None and encoder.eqmid > adapter.ceiling
        except AttributeError:
            return False

    def _grade(self, now: float) -> str:
        """From how the audio gets through, not from RSSI (see the module notes)."""
        recent = self._window(now, 10)
        if sum(x.lost_ms for x in recent) > 0:
            return "weak"
        queued = max((x.queued_ms for x in recent), default=0.0)
        if queued > 60 or self.lost_ms(now, 60) > 0 or self._ldac_lowered():
            return "good"
        return "excellent"

    def interference(self, now: float) -> bool:
        """Audio lost in the last minute while RSSI (last 10 s) stays near its best of the
        last minutes (within 6 dB): not distance but 2.4 GHz noise, usually Wi-Fi."""
        if self.lost_ms(now, 60) < 100:
            return False
        recent = sorted(x.rssi for x in self._window(now, 10) if x.rssi is not None)
        history = sorted(x.rssi for x in self.samples if x.rssi is not None)
        if len(recent) < 5 or len(history) < 10:
            return False
        best = history[int(len(history) * 0.9)]
        return best - recent[len(recent) // 2] <= 6

    def _set_level(self, level: str, now: float) -> None:
        self.level = level
        if level == self.logged_level or level == "none" and self.logged_level == "none":
            return
        if level != "weak" and self.logged_level != "weak":
            self.logged_level = level        # excellent <-> good: not worth a line
            return
        if now < self.next_log and level == "weak":
            return
        self.next_log = now + 30.0
        self.logged_level = level
        log.info("radio: signal %s (rssi %s, lost %.1f s in the last minute%s)",
                 level, self.rssi, self.lost_ms(now, 60) / 1000,
                 ", signal as strong as usual: interference?" if level == "weak" and self.interference(now) else "")

    # -- A5 -------------------------------------------------------------------------
    async def refresh_wifi(self) -> None:
        if not self.s.wifi_avoid:
            self.avoid = []
        elif self.s.wifi_channel:
            self.avoid = wifi_channels_to_avoid([], int(self.s.wifi_channel))
        else:
            networks, error = await asyncio.get_running_loop().run_in_executor(None, self.read_wifi)
            self.networks, self.wifi_error = networks, error
            self.avoid = wifi_channels_to_avoid(networks)
        new_map = classification(bad_bt_channels(self.avoid))
        if new_map != self.host_map:
            self.host_map = new_map
            log.info("radio: avoiding Wi-Fi channel(s) %s (%d of 79 Bluetooth channels)",
                     ", ".join(map(str, self.avoid)) or "none",
                     BT_CHANNELS - map_string(new_map).count("1"))

    async def apply_map(self) -> None:
        if self.host_map == self.sent_map or not getattr(self.host, "ready", False):
            return
        if self.sent_map is None and self.host_map == classification(set()):
            self.sent_map = self.host_map        # nothing to avoid yet: the controller's default
            return
        device = getattr(self.host, "device", None)
        if device is None:
            return
        try:
            await asyncio.wait_for(self._command(
                HCI_Set_AFH_Host_Channel_Classification_Command(
                    afh_host_channel_classification=self.host_map)), 2.0)
            self.sent_map = self.host_map
            self.afh_error = None
        except Exception as error:
            self.afh_error = _describe(error)
            self.sent_map = self.host_map        # do not retry every second
            log.warning("radio: the adapter refused the Wi-Fi channel map (%s)", self.afh_error)

    async def _read_link_map(self, connection) -> None:
        try:
            rp = await asyncio.wait_for(self._command(
                HCI_Read_AFH_Channel_Map_Command(connection_handle=connection.handle)), 2.0)
            self.link_map = map_string(rp.afh_channel_map) if rp.afh_mode else None
            if self.link_map is not None:
                self.last_link_map = self.link_map
        except Exception as error:
            self.link_map = None
            if _unsupported(error):
                self.afh_read_ok = False

    async def _maybe_become_central(self, connection) -> None:
        """Only the central chooses the hopping channels: ask the headset to swap roles
        once per connection when there is something to avoid."""
        if (self.role != "peripheral" or self.role_tried == connection.handle
                or self.host.state != "streaming" or not self.avoid):
            return
        self.role_tried = connection.handle
        try:
            await asyncio.wait_for(self.host.device.switch_role(connection, hci.Role.CENTRAL), 5.0)
            log.info("radio: we are central now (the hopping map is ours)")
        except Exception as error:
            log.info("radio: the headset keeps the central role (%s): it chooses the channels",
                     _describe(error))

    # -- for the window -------------------------------------------------------------
    def snapshot(self) -> dict:
        now = self.clock()
        step = self.STEP_S
        points = int(self.HISTORY_S / step)
        buckets: list[list[Sample]] = [[] for _ in range(points)]
        for x in self.samples:
            i = points - 1 - int((now - x.t) // step)
            if 0 <= i < points:
                buckets[i].append(x)

        def avg(values):
            values = [v for v in values if v is not None]
            return round(sum(values) / len(values), 1) if values else None
        history = {
            "step_s": step,
            "rssi": [avg(x.rssi for x in b) for b in buckets],
            "lost_ms": [round(sum(x.lost_ms for x in b)) if b else None for b in buckets],
            "queued_ms": [round(max(x.queued_ms for x in b)) if b else None for b in buckets],
            "kbps": [min((x.kbps for x in b if x.kbps), default=None) for b in buckets],
        }
        bad = sorted(bad_bt_channels(self.avoid))
        return {
            "level": self.level,
            "rssi": self.rssi,
            "link_quality": self.lq,
            "lost_ms_minute": round(self.lost_ms(now, 60)),
            "lost_ms_hour": round(self.lost_ms(now, 3600)),
            "interference": self.interference(now),
            "role": self.role,
            "history": history,
            "wifi": {
                "enabled": bool(self.s.wifi_avoid),
                "manual": int(self.s.wifi_channel or 0),
                "networks": [dataclasses.asdict(n) for n in self.networks[:12]],
                "avoid": list(self.avoid),
                "error": self.wifi_error,
                "suggested": suggest_wifi_channel(self.last_link_map),
            },
            "afh": {
                "host_map": map_string(self.host_map),
                "link_map": self.link_map,
                "avoided": len(bad),
                "applied": self.sent_map == self.host_map and self.afh_error is None,
                "error": self.afh_error,
            },
        }

    def stats(self) -> str:
        if self.host.connection is None:
            return ""
        return (f" rssi={self.rssi} lq={self.lq} radio={self.level}"
                f" role={self.role}")


def _unsupported(error: BaseException) -> bool:
    """Unknown command (0x01) or unsupported feature (0x11): asking again is pointless."""
    code = getattr(error, "error_code", None)
    if code in (0x01, 0x11):
        return True
    text = (repr(error) + str(error)).upper()
    return "UNKNOWN_HCI_COMMAND" in text or "UNSUPPORTED" in text


def _describe(error: BaseException) -> str:
    text = str(error)
    return f"{type(error).__name__}: {text}" if text else repr(error)
