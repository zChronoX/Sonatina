"""AVRCP for SonatinaHost: headset buttons and volume synced with the headset.

Two roles on the same AVCTP channel:
  * Target (TG): the headset's buttons (play/pause, next, previous, volume keys) arrive
    as pass-through commands. Play/pause/next/previous go to the current Windows media
    session (sonatina_media), whose real status is also reported to the headset; without
    it they become media keys and the status is guessed from audio activity.
  * Controller (CT): when the headset supports absolute volume (AVRCP 1.4+), the Windows
    volume of "CABLE Input" is sent to the headset, and the headset's own volume buttons
    move the Windows slider. Our software gain then only does mute, so the encoder always
    gets the full-scale signal. Without absolute volume the software gain stays as before.

One Protocol object per connection: Bumble's Protocol keeps per-link state (transaction
labels, pending commands) that a dropped link would otherwise leave behind.
"""

from __future__ import annotations

import asyncio
import ctypes
import logging
import sys
import time

from bumble import avc, avctp, avrcp, l2cap
from bumble.device import Connection, Device

log = logging.getLogger("sonatina")

Key = avc.PassThroughFrame.OperationId

# Windows virtual-key codes of the media keys
VK_VOLUME_MUTE = 0xAD
VK_VOLUME_DOWN = 0xAE
VK_VOLUME_UP = 0xAF
VK_MEDIA_NEXT_TRACK = 0xB0
VK_MEDIA_PREV_TRACK = 0xB1
VK_MEDIA_STOP = 0xB2
VK_MEDIA_PLAY_PAUSE = 0xB3

MEDIA_COMMANDS = {   # sonatina_media commands, when Windows media sessions are available
    Key.STOP: "stop",
    Key.FORWARD: "skip_next",
    Key.BACKWARD: "skip_previous",
}

KEY_MAP = {
    Key.PLAY: VK_MEDIA_PLAY_PAUSE,
    Key.PAUSE: VK_MEDIA_PLAY_PAUSE,
    Key.STOP: VK_MEDIA_STOP,
    Key.FORWARD: VK_MEDIA_NEXT_TRACK,
    Key.BACKWARD: VK_MEDIA_PREV_TRACK,
    Key.VOLUME_UP: VK_VOLUME_UP,
    Key.VOLUME_DOWN: VK_VOLUME_DOWN,
    Key.MUTE: VK_VOLUME_MUTE,
}

MAX_VOLUME = avrcp.SetAbsoluteVolumeCommand.MAXIMUM_VOLUME  # 127
COMMAND_TIMEOUT_S = 3.0
CONNECT_AFTER_S = 2.0      # headsets usually open AVCTP themselves; if not, we do
ACTIVE_HOLD_S = 1.5        # audio seen this recently = "playing"
PAUSE_IGNORE_SILENT_S = 3.0
PLAY_PAUSE_DEBOUNCE_S = 0.4
ECHO_WINDOW_S = 1.5        # headset notifications of values we just sent are echoes


def send_key(vk: int) -> None:
    """Presses and releases a Windows key (media keys are global, any app gets them)."""
    if sys.platform != "win32":
        return
    keybd_event = ctypes.windll.user32.keybd_event
    keybd_event(vk, 0, 1, 0)        # KEYEVENTF_EXTENDEDKEY
    keybd_event(vk, 0, 1 | 2, 0)    # ... | KEYEVENTF_KEYUP


def sdp_records(target_handle: int, controller_handle: int) -> dict:
    """AVRCP TG (category 1: player) and CT (category 2: amplifier, i.e. volume)."""
    return {
        target_handle: avrcp.TargetServiceSdpRecord(
            target_handle, supported_features=avrcp.TargetFeatures.CATEGORY_1
        ).to_service_attributes(),
        controller_handle: avrcp.ControllerServiceSdpRecord(
            controller_handle, supported_features=avrcp.ControllerFeatures.CATEGORY_2
        ).to_service_attributes(),
    }


class _Delegate(avrcp.Delegate):
    def __init__(self, remote: RemoteControl) -> None:
        super().__init__(supported_events=[avrcp.EventId.PLAYBACK_STATUS_CHANGED])
        self.remote = remote

    async def on_key_event(self, key, pressed: bool, data: bytes) -> None:
        if pressed:
            self.remote.on_key(key)

    async def get_playback_status(self) -> avrcp.PlayStatus:
        status = self.remote.play_status()
        if not self.remote.status_asked:
            self.remote.status_asked = True
            log.info("AVRCP: the headset follows the play status (now %s)", status.name)
        return status


class RemoteControl:
    """AVRCP for the one headset SonatinaHost is connected to.

    host must provide: s (Settings with media_keys, absolute_volume), volume
    (EndpointVolume), audio_age() -> seconds since non-silent audio was streamed.
    media: Windows media sessions for real play/pause and status (else media keys and
    a status guessed from the audio)."""

    def __init__(self, host, media=None) -> None:
        self.host = host
        self.media = media             # sonatina_media.MediaSessions (None: media keys only)
        self.connection: Connection | None = None
        self.protocol: avrcp.Protocol | None = None
        self.tasks: list[asyncio.Task] = []
        self.absolute = False          # headset volume follows Windows (and back)
        self.level: int | None = None  # last volume both sides agree on, 0..127
        self.want: int | None = None   # to send to the headset
        self.sent: dict[int, float] = {}
        self.sender: asyncio.Task | None = None
        self.status = avrcp.PlayStatus.STOPPED
        self.last_play_pause = 0.0
        self.connected_at = 0.0
        self.status_asked = False      # the headset follows our play status

    # -- wiring ----------------------------------------------------------------
    def listen(self, device: Device) -> None:
        device.create_l2cap_server(l2cap.ClassicChannelSpec(avctp.AVCTP_PSM), self._on_channel)

    def _on_channel(self, channel: l2cap.ClassicChannel) -> None:
        protocol = self.protocol
        if protocol is None or channel.connection is not self.connection:
            log.debug("AVRCP: channel from a link we do not serve, closing")
            asyncio.get_running_loop().create_task(self._close(channel))
            return
        channel.on(channel.EVENT_OPEN, lambda: protocol._on_avctp_channel_open(channel))

    @staticmethod
    async def _close(channel) -> None:
        try:
            await channel.disconnect()
        except Exception:
            pass

    def start(self, connection: Connection) -> None:
        """Called as soon as a headset link is accepted (the headset may open AVCTP at once)."""
        self.stop()
        self.connection = connection
        self.connected_at = time.monotonic()
        self.protocol = avrcp.Protocol(_Delegate(self))
        self.protocol.on(self.protocol.EVENT_START, self._on_start)
        self.protocol.on(self.protocol.EVENT_STOP, self._on_stop)

    def on_streaming(self) -> None:
        """A2DP is up: if the headset did not open AVRCP by itself, open it ourselves."""
        if self.protocol is not None:
            self._spawn(self._connect_if_needed(self.protocol))

    def stop(self) -> None:
        for task in self.tasks:
            task.cancel()
        self.tasks = []
        if self.sender is not None:
            self.sender.cancel()
            self.sender = None
        self._set_absolute(False)
        self.protocol = None
        self.connection = None
        self.status = avrcp.PlayStatus.STOPPED
        self.level = None
        self.want = None
        self.sent.clear()
        self.status_asked = False

    def _spawn(self, coro) -> None:
        self.tasks = [t for t in self.tasks if not t.done()]
        self.tasks.append(asyncio.get_running_loop().create_task(coro))

    async def _connect_if_needed(self, protocol: avrcp.Protocol) -> None:
        await asyncio.sleep(CONNECT_AFTER_S)
        if protocol is not self.protocol or protocol.avctp_protocol is not None:
            return
        try:
            await asyncio.wait_for(protocol.connect(self.connection), COMMAND_TIMEOUT_S)
        except Exception as error:
            log.info("AVRCP not available on this headset (%r)", error)

    def _on_start(self) -> None:
        log.info("AVRCP connected")
        protocol = self.protocol
        self._spawn(self._setup(protocol))
        self._spawn(self._track_status(protocol))

    def _on_stop(self) -> None:
        log.info("AVRCP disconnected")
        self._set_absolute(False)

    # -- headset -> Windows: buttons ---------------------------------------------
    def on_key(self, key) -> None:
        vk = KEY_MAP.get(key)
        if vk is None:
            log.info("headset button %s: not mapped", key)
            return
        if not getattr(self.host.s, "media_keys", True):
            return
        now = time.monotonic()
        name = getattr(key, "name", str(key))
        if vk == VK_MEDIA_PLAY_PAUSE:
            if now - self.last_play_pause < PLAY_PAUSE_DEBOUNCE_S:
                return
            if key == Key.PLAY and now - self.connected_at < 3.0:
                log.info("headset button PLAY right after connecting: ignored")
                return
            output = getattr(self.host, "output", None)
            if output is not None and output.own_reconnection():
                log.info("headset button %s right after Sonatina reconnected the headphones "
                         "(codec or option change): ignored", name)
                return
            self.last_play_pause = now
        media = self.media
        if media is not None and media.available:
            # Real player control. A headset that follows our (real) play status sends the
            # right PLAY or PAUSE; one that does not is just a toggle button.
            if vk == VK_MEDIA_PLAY_PAUSE:
                command = (("play" if key == Key.PLAY else "pause") if self.status_asked
                           else "toggle_play_pause")
            else:
                command = MEDIA_COMMANDS.get(key)
            if command is not None:
                log.info("headset button %s -> %s", name, command)
                media.command(command)
                return
        elif vk == VK_MEDIA_PLAY_PAUSE:
            # Only the toggle key: guard against a PAUSE (e.g. a wear sensor) when nothing
            # plays, which would start playback.
            if key == Key.PAUSE and self.host.audio_age() > PAUSE_IGNORE_SILENT_S:
                log.info("headset button PAUSE while nothing plays: ignored")
                return
        log.info("headset button %s", name)
        send_key(vk)

    def play_status(self) -> avrcp.PlayStatus:
        media = self.media
        if media is not None and media.available and media.status is not None:
            return {"playing": avrcp.PlayStatus.PLAYING, "paused": avrcp.PlayStatus.PAUSED
                    }.get(media.status, avrcp.PlayStatus.STOPPED)
        if self.host.audio_age() < ACTIVE_HOLD_S:
            return avrcp.PlayStatus.PLAYING
        return avrcp.PlayStatus.PAUSED

    async def _track_status(self, protocol: avrcp.Protocol) -> None:
        while protocol is self.protocol:
            status = self.play_status()
            if status != self.status:
                self.status = status
                if self.status_asked:
                    log.info("AVRCP: play status -> %s", status.name)
                try:
                    protocol.notify_playback_status_changed(status)
                except Exception as error:
                    log.debug("AVRCP status notify: %r", error)
            await asyncio.sleep(0.5)

    # -- absolute volume ----------------------------------------------------------
    async def _setup(self, protocol: avrcp.Protocol) -> None:
        if not getattr(self.host.s, "absolute_volume", True):
            return
        try:
            events = await asyncio.wait_for(protocol.get_supported_events(), COMMAND_TIMEOUT_S)
        except Exception as error:
            log.info("AVRCP: headset capabilities unavailable (%r): volume stays on the PC", error)
            return
        if avrcp.EventId.VOLUME_CHANGED not in events:
            log.info("headset without absolute volume: volume stays on the PC")
            return
        level = self.windows_level()
        if level is not None:
            # Headset volume first, then drop our gain: never a loud moment.
            if not await self._send(protocol, level):
                return
            self.level = level
        self._set_absolute(True)
        log.info("absolute volume on: %s", f"the headset follows Windows ({level}/127)"
                 if level is not None else "Windows volume unknown, the headset keeps its own")
        await self._monitor(protocol)

    async def _monitor(self, protocol: avrcp.Protocol) -> None:
        try:
            async for value in protocol.monitor_volume():
                if protocol is not self.protocol:
                    return
                self.on_headset_volume(value)
        except asyncio.CancelledError:
            raise
        except Exception as error:
            log.info("AVRCP volume notifications stopped (%r)", error)

    def _set_absolute(self, on: bool) -> None:
        if self.absolute == on:
            return
        self.absolute = on
        volume = self.host.volume
        if volume is not None:
            volume.hardware = on
            volume.poll()

    def windows_level(self) -> int | None:
        volume = self.host.volume
        if volume is None or volume.scalar is None:
            return None
        return max(0, min(MAX_VOLUME, round(volume.scalar * MAX_VOLUME)))

    def on_windows_volume(self) -> None:
        """Called after every poll of the Windows volume."""
        if not self.absolute:
            return
        volume = self.host.volume
        if volume is not None and volume.in_call:
            return      # the voice channel ignores it; sent when the call ends (level kept)
        level = self.windows_level()
        if level is None or level == self.level:
            return
        self.level = level
        self.want = level
        if self.sender is None or self.sender.done():
            self.sender = asyncio.get_running_loop().create_task(self._sender())

    async def _sender(self) -> None:
        while self.absolute and self.protocol is not None and self.want is not None:
            value, self.want = self.want, None
            await self._send(self.protocol, value)

    async def _send(self, protocol: avrcp.Protocol, value: int) -> bool:
        self.sent[value] = time.monotonic()
        try:
            await asyncio.wait_for(
                protocol.send_avrcp_command(avc.CommandFrame.CommandType.CONTROL,
                                            avrcp.SetAbsoluteVolumeCommand(value)),
                COMMAND_TIMEOUT_S)
            log.debug("headset volume -> %d", value)
            return True
        except Exception as error:
            log.info("set headset volume failed (%r)", error)
            return False

    def on_headset_volume(self, value: int) -> None:
        now = time.monotonic()
        sent_at = self.sent.get(value)
        if sent_at is not None and now - sent_at < ECHO_WINDOW_S:
            return   # the headset confirming something we sent (maybe already superseded)
        if value == self.level:
            return
        self.level = value
        log.info("headset volume %d%%", round(value * 100 / MAX_VOLUME))
        volume = self.host.volume
        if volume is not None:
            volume.set_scalar(value / MAX_VOLUME, unmute=value > 0)
