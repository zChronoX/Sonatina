"""Settings service: watcher on the Trigger value, debounce, cooldown, apply."""

from __future__ import annotations

import threading

from . import codecs
from .reg import Profile, RegistryError, Store
from .watcher import SettingsWatcher, Win32Error, default_log


class Supervisor:
    """Re-applies the stored profiles when the configuration changes."""

    def __init__(self, store: Store, log=default_log) -> None:
        self.store = store
        self.log = log
        self.restarts = 0

    def apply(self) -> list[str]:
        self.restarts += 1
        addresses = self.store.list_addresses()
        if not addresses:
            self.log(
                "apply #%d: no profiles under %s" % (self.restarts, self.store.root)
            )
            return []
        for address in addresses:
            profile = self.store.read_profile(address)
            if profile is None:
                continue
            try:
                profile.sbc.validate()
            except ValueError as exc:
                self.log(
                    "apply #%d: device=%s has an invalid profile: %s"
                    % (self.restarts, address, exc)
                )
                continue
            kbps = estimate(profile)
            self.log(
                "apply #%d: restart device=%s %s estimated_kbps=%d"
                % (self.restarts, address, profile.summary(), round(kbps))
            )
        return addresses


def estimate(profile: Profile) -> float:
    channels = (
        profile.sbc.channels
        if profile.codec == "sbc"
        else (2 if profile.codec != "pcm" else profile.sbc.channels)
    )
    sampling = profile.sbc.sampling if profile.codec == "sbc" else profile.aac_sampling
    return codecs.estimate_kbps(
        profile.codec,
        sampling=sampling,
        channels=channels,
        sbc=profile.sbc,
        aac_bitrate=profile.aac_bitrate,
    )


def run(args) -> int:
    store = Store(args.hive, args.root)
    try:
        store.ensure()
    except RegistryError as exc:
        print("error: %s" % exc)
        return 2

    supervisor = Supervisor(store)
    if args.once:
        supervisor.apply()
        return 0

    watcher = SettingsWatcher(
        store.watch_path,
        hive=args.hive,
        debounce_ms=args.debounce,
        cooldown_ms=args.cooldown,
    )
    timer = None
    if args.duration:
        timer = threading.Timer(args.duration, watcher.stop)
        timer.daemon = True
        timer.start()
    default_log("service started (hive=%s root=%s)" % (args.hive, store.root))
    try:
        watcher.run(supervisor.apply)
    except KeyboardInterrupt:
        pass
    except Win32Error as exc:
        print("error: %s" % exc)
        return 2
    finally:
        if timer is not None:
            timer.cancel()
        watcher.stop()
    default_log("service stopped after %d apply cycle(s)" % supervisor.restarts)
    return 0
