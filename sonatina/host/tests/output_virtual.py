"""Automatic Windows output (1.1.0, A1), with a fake Windows (endpoints + default) and a
fake clock. Checked:
  1. Start without headphones: after the startup grace Windows goes to the fallback.
  2. Headphones ready: Windows goes to "Sonatina Audio".
  3. Headphones turned off while playing: player paused at once, Windows to the fallback
     after the grace (not before).
  4. A short dropout (reconnect within the grace): no switch.
  5. Sonatina's own disconnection (codec change): no pause, no switch.
  6. A device picked by hand while the headphones are off stays.
  7. The fallback chosen by the user (the monitor) wins over the last used output; a
     missing fallback falls back to the last used one; the microphone cable as default
     output is corrected.
  8. leave_cable (Sonatina quits / adapter unplugged).
"""
import asyncio
import os
import sys
import types

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import sonatina_host as sh            # noqa: E402
import sonatina_output as so          # noqa: E402

CABLE = so.Endpoint("{cable}", "Sonatina Audio (VB-Audio Virtual Cable)")
CABLE16 = so.Endpoint("{c16}", "CABLE In 16 Ch (VB-Audio Virtual Cable)")
MIC = so.Endpoint("{mic}", "Sonatina Microfono (VB-Audio Hi-Fi Cable)")
SPEAKERS = so.Endpoint("{spk}", "Altoparlanti (Realtek(R) Audio)")
MONITOR = so.Endpoint("{mon}", "LG ULTRAGEAR (NVIDIA High Definition Audio)")


class FakeWindows:
    def __init__(self, default):
        self.endpoints = [CABLE16, CABLE, MIC, SPEAKERS, MONITOR]
        self.default = default.id
        self.history = []

    def list(self):
        return list(self.endpoints), self.default

    def set_default(self, endpoint_id):
        self.default = endpoint_id
        self.history.append(endpoint_id)


class Media:
    available = True
    status = "playing"

    def __init__(self):
        self.commands = []

    def command(self, name):
        self.commands.append(name)
        return True


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


def make(default=CABLE, **settings):
    s = sh.Settings(**settings)
    clock = Clock()
    host = types.SimpleNamespace(
        s=s, connection=None, state="idle", hfp=types.SimpleNamespace(in_call=False, com=None),
        remote=types.SimpleNamespace(media=Media()), notices=[], last_audio=-1e9, changed=0)
    host.audio_age = lambda: clock.t - host.last_audio
    host._changed = lambda: setattr(host, "changed", host.changed + 1)
    host.on_notice = host.notices.append
    win = FakeWindows(default)
    sw = so.OutputSwitcher(host, backend=win, clock=clock)
    return host, win, sw, clock


async def run_for(sw, clock, seconds, step=0.5):
    end = clock.t + seconds
    while clock.t < end:
        await sw.tick()
        clock.t += step


def connect(host, clock):
    host.connection = types.SimpleNamespace(handle=1)
    host.state = "streaming"
    host.last_audio = clock.t


def disconnect(host):
    host.connection = None
    host.state = "idle"


async def main():
    # 1 + 2 + 3: start without headphones, monitor chosen as fallback
    host, win, sw, clock = make(fallback_output=MONITOR.id, fallback_output_name=MONITOR.name)
    await run_for(sw, clock, 10)
    assert win.default == CABLE.id, "switched before the startup grace"
    await run_for(sw, clock, 6)
    assert win.default == MONITOR.id, win.default
    assert host.notices and "LG ULTRAGEAR" in host.notices[-1], host.notices
    print("1. start without headphones -> monitor after the grace: OK")
    connect(host, clock)
    await run_for(sw, clock, 1)
    assert win.default == CABLE.id, win.default
    print("2. headphones ready -> Sonatina Audio: OK")
    await run_for(sw, clock, 5)
    host.last_audio = clock.t           # music playing
    disconnect(host)
    await run_for(sw, clock, 1)
    assert host.remote.media.commands == ["pause"], host.remote.media.commands
    assert win.default == CABLE.id, "switched before the grace"
    await run_for(sw, clock, 3)
    assert win.default == MONITOR.id, win.default
    assert "pausa" in host.notices[-1] or "paused" in host.notices[-1], host.notices[-1]
    print("3. headphones off while playing -> paused at once, monitor after 3 s: OK")

    # 4: short dropout
    connect(host, clock)
    await run_for(sw, clock, 2)
    assert win.default == CABLE.id
    switches = len(win.history)
    disconnect(host)
    await run_for(sw, clock, 1.5)
    connect(host, clock)
    await run_for(sw, clock, 5)
    assert win.default == CABLE.id and len(win.history) == switches, win.history
    print("4. dropout of 1.5 s -> no switch: OK")

    # 5: our own disconnection (codec change)
    host.remote.media.commands.clear()
    host.last_audio = clock.t
    sw.expect_reconnect()
    disconnect(host)
    await run_for(sw, clock, 8)
    connect(host, clock)
    await run_for(sw, clock, 2)
    assert not host.remote.media.commands and len(win.history) == switches, (host.remote.media.commands, win.history)
    print("5. codec change -> no pause, no switch: OK")
    # ... and a play/pause button the headset sends right after that reconnection is ignored
    import sonatina_avrcp as sa
    assert sw.own_reconnection()
    host.output = sw
    remote = sa.RemoteControl(host, host.remote.media)
    remote.connected_at = -1e9
    host.remote.media.commands.clear()
    remote.on_key(sa.Key.PAUSE)
    assert host.remote.media.commands == [], host.remote.media.commands
    clock.t += 10
    assert not sw.own_reconnection()
    remote.status_asked = True
    remote.on_key(sa.Key.PAUSE)
    assert host.remote.media.commands == ["pause"], host.remote.media.commands
    print("   headset PAUSE right after our reconnection ignored, later obeyed: OK")

    # 6: a device picked by hand while the headphones are off stays
    disconnect(host)
    await run_for(sw, clock, 4)
    assert win.default == MONITOR.id
    win.default = SPEAKERS.id           # the user picks the speakers
    await run_for(sw, clock, 30)
    assert win.default == SPEAKERS.id, win.default
    connect(host, clock)                # headphones back: they take the sound again
    await run_for(sw, clock, 1)
    assert win.default == CABLE.id
    print("6. manual choice kept while off, headphones take over again: OK")

    # 7: automatic fallback = the last real output used; missing chosen device
    host, win, sw, clock = make(default=SPEAKERS)
    await run_for(sw, clock, 1)
    assert host.s.last_output == SPEAKERS.id and host.changed == 1
    connect(host, clock)
    await run_for(sw, clock, 1)
    assert win.default == CABLE.id
    assert host.s.last_output == SPEAKERS.id      # the cable is not a "real" output
    disconnect(host)
    await run_for(sw, clock, 4)
    assert win.default == SPEAKERS.id, win.default
    snap = sw.snapshot()
    assert snap["target"] == SPEAKERS.name and snap["automatic_name"] == SPEAKERS.name, snap
    assert [o["name"] for o in snap["outputs"]] == [SPEAKERS.name, MONITOR.name], snap["outputs"]
    host.s.fallback_output, host.s.fallback_output_name = "{gone}", "Cuffie USB"
    assert sw.fallback().id == SPEAKERS.id
    host.s.fallback_output = MONITOR.id
    assert sw.fallback().id == MONITOR.id
    host.s.fallback_output = "{new-id}"           # driver reinstalled: same name, new id
    host.s.fallback_output_name = MONITOR.name
    assert sw.fallback().id == MONITOR.id
    win.default = MIC.id                          # the microphone cable as Windows output
    sw.next_refresh = 0
    await run_for(sw, clock, 1)
    assert win.default == MONITOR.id, win.default
    connect(host, clock)
    await run_for(sw, clock, 1)
    win.default = MIC.id
    sw.next_refresh = 0
    await run_for(sw, clock, 1)
    assert win.default == CABLE.id, win.default   # stereo cable, not the 16-channel one
    print("7. fallback choice, renamed ids, microphone cable fixed: OK")

    # auto_output off: nothing moves (except the microphone cable)
    host, win, sw, clock = make(auto_output=False)
    await run_for(sw, clock, 20)
    assert win.history == []
    print("   auto_output off -> untouched: OK")

    # 8: leave_cable
    s = sh.Settings(fallback_output=MONITOR.id, fallback_output_name=MONITOR.name)
    win = FakeWindows(CABLE)
    assert so.leave_cable(s, win) == MONITOR.name and win.default == MONITOR.id
    win = FakeWindows(SPEAKERS)
    assert so.leave_cable(s, win) is None and win.default == SPEAKERS.id
    s.auto_output = False
    win = FakeWindows(CABLE)
    assert so.leave_cable(s, win) is None and win.default == CABLE.id
    print("8. leave_cable: OK")

    # 9 (N4, 1.3.0): the cable's name, and the hint to rename it
    host, win, sw, clock = make()
    await run_for(sw, clock, 1)
    snap = sw.snapshot()
    assert snap["cable_name"] == CABLE.name and snap["cable_renamed"] and not snap["cable_hint"]
    old = so.Endpoint("{cable}", "Altoparlanti (2- VB-Audio Virtual Cable)")
    win.endpoints = [CABLE16, old, MIC, SPEAKERS, MONITOR]
    sw.next_refresh = 0
    await run_for(sw, clock, 1)
    snap = sw.snapshot()
    assert snap["cable_name"] == old.name and snap["cable_hint"], snap
    host.s.cable_rename_hint = False
    assert not sw.snapshot()["cable_hint"], "toggle off: no hint"
    assert so.cable_renamed(None) and so.cable_renamed("Sonatina Audio (VB-Audio Virtual Cable)")
    assert not so.cable_renamed("CABLE Input (VB-Audio Virtual Cable)")
    print("9. cable name and rename hint: OK")
    print("OUTPUT OK")


asyncio.run(main())
