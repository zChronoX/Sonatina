"""Command line interface for Sonatina."""

from __future__ import annotations

import argparse
import json
from dataclasses import replace

from . import (
    CODECS,
    EXIT_ARGS,
    EXIT_ENCODER,
    EXIT_ERROR,
    EXIT_NO_DEVICE,
    EXIT_OK,
    REG_ROOT,
)
from . import codecs, devices, sink, source, svc
from .bdaddr import normalize
from .reg import Profile, RegistryError, Store
from .sbc import SbcParams, bitrate_table


def _add_registry_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--hive",
        choices=("HKCU", "HKLM"),
        default="HKCU",
        help="registry hive (HKLM needs administrator rights)",
    )
    parser.add_argument("--root", default=REG_ROOT, help="registry path under the hive")


def _add_port_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--signal-port", type=int, default=5005)
    parser.add_argument("--media-port", type=int, default=5004)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="sonatina",
        description="Sonatina - user-space Bluetooth audio source, sink, service and GUI",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    cmd = sub.add_parser("devices", help="list Bluetooth audio devnodes")
    _add_registry_options(cmd)
    cmd.add_argument("--json", action="store_true", help="machine readable output")

    profile = sub.add_parser("profile", help="read and write per-device profiles")
    actions = profile.add_subparsers(dest="action", required=True)

    for name in ("list", "dump"):
        item = actions.add_parser(name)
        _add_registry_options(item)

    get = actions.add_parser("get")
    get.add_argument("--device", required=True)
    _add_registry_options(get)

    delete = actions.add_parser("delete")
    delete.add_argument("--device", required=True)
    _add_registry_options(delete)

    set_parser = actions.add_parser("set")
    set_parser.add_argument("--device", required=True)
    set_parser.add_argument("--codec", choices=CODECS)
    set_parser.add_argument(
        "--sbc-sampling", type=int, choices=(16000, 32000, 44100, 48000)
    )
    set_parser.add_argument(
        "--sbc-channel-mode", choices=("mono", "dual", "stereo", "joint")
    )
    set_parser.add_argument("--sbc-blocks", type=int, choices=(4, 8, 12, 16))
    set_parser.add_argument("--sbc-subbands", type=int, choices=(4, 8))
    set_parser.add_argument("--sbc-allocation", choices=("snr", "loudness"))
    set_parser.add_argument("--sbc-min-bitpool", type=int)
    set_parser.add_argument("--sbc-max-bitpool", type=int)
    set_parser.add_argument("--aac-channel-mode", choices=("mono", "stereo"))
    set_parser.add_argument("--aac-sampling", type=int, choices=(44100, 48000))
    set_parser.add_argument("--aac-bitrate", type=int)
    set_parser.add_argument(
        "--no-trigger",
        action="store_true",
        help="do not bump the service Trigger value",
    )
    _add_registry_options(set_parser)

    sink_parser = sub.add_parser(
        "sink", help="headset simulator (AVDTP acceptor + RTP receiver)"
    )
    sink_parser.add_argument(
        "--caps",
        default="sbc,pcm",
        help="comma separated codecs advertised by the headset",
    )
    sink_parser.add_argument("--sampling", type=int, default=48000)
    sink_parser.add_argument("--max-bitpool", type=int, default=53)
    sink_parser.add_argument("--aac-bitrate", type=int, default=320)
    sink_parser.add_argument(
        "--wait",
        type=float,
        default=10.0,
        help="seconds to wait for a source to connect",
    )
    sink_parser.add_argument(
        "--duration",
        type=float,
        default=8.0,
        help="maximum media capture time after Start",
    )
    sink_parser.add_argument(
        "--idle",
        type=float,
        default=1.5,
        help="stop after this many seconds without media packets",
    )
    sink_parser.add_argument(
        "--out", default="out.wav", help="WAV file to write ('' to skip)"
    )
    sink_parser.add_argument("--device", help="write Capability for this BD_ADDR")
    _add_port_options(sink_parser)
    _add_registry_options(sink_parser)

    source_parser = sub.add_parser("source", help="Bluetooth audio source (PCM -> codec -> RTP)")
    source_parser.add_argument("--device", required=True)
    source_parser.add_argument("--codec", choices=CODECS)
    source_parser.add_argument("--profile-from-registry", action="store_true")
    source_parser.add_argument("--duration", type=float, default=5.0)
    source_parser.add_argument(
        "--pcm", choices=("sine", "wav", "loopback"), default="sine"
    )
    source_parser.add_argument("--wav")
    source_parser.add_argument("--sampling", type=int, default=48000)
    source_parser.add_argument("--channels", type=int, choices=(1, 2), default=2)
    source_parser.add_argument(
        "--frames-per-packet",
        type=int,
        default=3,
        help="SBC frames per RTP packet (1..15)",
    )
    _add_port_options(source_parser)
    _add_registry_options(source_parser)

    svc_parser = sub.add_parser(
        "svc", help="settings service (registry watcher + cooldown)"
    )
    svc_parser.add_argument(
        "--console", action="store_true", help="log to the console (default)"
    )
    svc_parser.add_argument("--debounce", type=int, default=1000, help="debounce in ms")
    svc_parser.add_argument(
        "--cooldown", type=int, default=20000, help="cooldown in ms"
    )
    svc_parser.add_argument(
        "--duration",
        type=float,
        default=0.0,
        help="stop after N seconds (0 = run until Ctrl+C)",
    )
    svc_parser.add_argument("--once", action="store_true", help="apply once and exit")
    _add_registry_options(svc_parser)

    gui_parser = sub.add_parser("gui", help="Tkinter configuration UI")
    gui_parser.add_argument("--selftest", action="store_true")
    _add_registry_options(gui_parser)

    bench_parser = sub.add_parser("bench", help="codec bitrate model")
    bench_parser.add_argument("--codec", choices=CODECS, default="sbc")
    bench_parser.add_argument("--bitpool", default="24,53")
    bench_parser.add_argument("--sampling", type=int, default=48000)
    bench_parser.add_argument(
        "--channel-mode", choices=("mono", "dual", "stereo", "joint"), default="joint"
    )
    bench_parser.add_argument("--blocks", type=int, choices=(4, 8, 12, 16), default=16)
    bench_parser.add_argument("--subbands", type=int, choices=(4, 8), default=8)
    bench_parser.add_argument(
        "--allocation", choices=("snr", "loudness"), default="loudness"
    )
    bench_parser.add_argument("--aac-bitrate", type=int, default=256)
    _add_registry_options(bench_parser)

    return parser


# --------------------------------------------------------------------------- handlers


def _cmd_devices(args) -> int:
    found = devices.enumerate_sinks()
    if args.json:
        print(json.dumps([device.__dict__ for device in found], indent=2))
        return EXIT_OK
    if not found:
        print("no Bluetooth audio devnode found under HKLM\\%s" % devices.BTHENUM_PATH)
        return EXIT_OK
    print("%-12s  %-24s  %-20s %s" % ("ADDRESS", "NAME", "SERVICE", "DRIVER"))
    for device in found:
        print(device.summary())
    return EXIT_OK


def _print_profile(profile: Profile) -> None:
    sbc = profile.sbc
    print("Device: %s" % profile.device)
    print("Codec: %s" % profile.codec)
    print("SbcChannelMode: %s" % sbc.channel_mode)
    print("SbcSamplingFrequency: %d" % sbc.sampling)
    print("SbcSubbands: %d" % sbc.subbands)
    print("SbcBlockLength: %d" % sbc.blocks)
    print("SbcAllocationMethod: %s" % sbc.allocation)
    print("SbcMinimumBitpool: %d" % sbc.min_bitpool)
    print("SbcMaximumBitpool: %d" % sbc.max_bitpool)
    print("SbcEstimatedBitrate: %.1f kbit/s" % sbc.bitrate_kbps())
    print("AacChannelMode: %s" % ("stereo" if profile.aac_channel_mode else "mono"))
    print("AacSamplingFrequency: %d" % profile.aac_sampling)
    print("AacBitrate: %d" % profile.aac_bitrate)


def _cmd_profile(args) -> int:
    store = Store(args.hive, args.root)

    if args.action in ("list", "dump"):
        addresses = store.list_addresses()
        if not addresses:
            print("no profiles under %s\\%s" % (args.hive, store.devices_path))
            return EXIT_OK
        for address in addresses:
            profile = store.read_profile(address)
            if profile is None:
                continue
            if args.action == "list":
                print("%s  %s" % (address, profile.summary()))
            else:
                print("[%s]" % address)
                _print_profile(profile)
                capability = store.read_capability(address)
                if capability:
                    print("Capability: %s" % json.dumps(capability, sort_keys=True))
                current = store.read_current(address)
                if current:
                    print("Current: %s" % json.dumps(current, sort_keys=True))
                print()
        return EXIT_OK

    try:
        address = normalize(args.device)
    except ValueError as exc:
        print("error: %s" % exc)
        return EXIT_ARGS

    if args.action == "get":
        profile = store.read_profile(address)
        if profile is None:
            print("error: no profile for %s" % address)
            return EXIT_NO_DEVICE
        _print_profile(profile)
        capability = store.read_capability(address)
        if capability:
            print("Capability: %s" % json.dumps(capability, sort_keys=True))
        current = store.read_current(address)
        if current:
            print("Current: %s" % json.dumps(current, sort_keys=True))
        return EXIT_OK

    if args.action == "delete":
        removed = store.delete_profile(address)
        print("%s %s" % ("removed" if removed else "not found:", address))
        return EXIT_OK

    # set
    store.ensure()
    profile = store.read_profile(address) or Profile(device=address)
    if args.codec:
        profile.codec = args.codec
    overrides = {
        "sampling": args.sbc_sampling,
        "channel_mode": args.sbc_channel_mode,
        "blocks": args.sbc_blocks,
        "subbands": args.sbc_subbands,
        "allocation": args.sbc_allocation,
        "min_bitpool": args.sbc_min_bitpool,
        "max_bitpool": args.sbc_max_bitpool,
    }
    sbc = profile.sbc
    for name, value in overrides.items():
        if value is not None:
            sbc = replace(sbc, **{name: value})
    profile.sbc = sbc
    if args.aac_channel_mode:
        profile.aac_channel_mode = 1 if args.aac_channel_mode == "stereo" else 0
    if args.aac_sampling:
        profile.aac_sampling = args.aac_sampling
    if args.aac_bitrate:
        profile.aac_bitrate = args.aac_bitrate
    try:
        profile.sbc.validate()
    except ValueError as exc:
        print("error: %s" % exc)
        return EXIT_ARGS
    store.write_profile(profile)
    print("profile written: %s  %s" % (address, profile.summary()))
    if not args.no_trigger:
        print("trigger=%d" % store.bump_trigger())
    return EXIT_OK


def _cmd_bench(args) -> int:
    if args.codec != "sbc":
        kbps = codecs.estimate_kbps(
            args.codec, sampling=args.sampling, aac_bitrate=args.aac_bitrate
        )
        available = codecs.ffmpeg_path()
        print(
            "codec=%s sampling=%d estimated_kbps=%.1f ffmpeg=%s"
            % (args.codec, args.sampling, kbps, available or "not found")
        )
        if args.codec in ("aac", "aptx") and not available:
            print("error: %s needs FFmpeg" % args.codec)
            return EXIT_ENCODER
        return EXIT_OK

    try:
        bitpools = [int(part) for part in str(args.bitpool).split(",") if part.strip()]
    except ValueError:
        print("error: --bitpool must be a comma separated list of integers")
        return EXIT_ARGS
    if not bitpools:
        bitpools = [53]
    try:
        params = SbcParams(
            sampling=args.sampling,
            channel_mode=args.channel_mode,
            blocks=args.blocks,
            subbands=args.subbands,
            allocation=args.allocation,
            min_bitpool=2,
            max_bitpool=max(2, min(250, max(bitpools))),
        )
        params.validate()
    except ValueError as exc:
        print("error: %s" % exc)
        return EXIT_ARGS
    print(
        "codec=sbc sampling=%d mode=%s blocks=%d subbands=%d alloc=%s"
        % (
            params.sampling,
            params.channel_mode,
            params.blocks,
            params.subbands,
            params.allocation,
        )
    )
    for bitpool, frame_len, kbps in bitrate_table(params, bitpools):
        print("bitpool=%d frame_len=%d kbps=%.3f" % (bitpool, frame_len, kbps))
    return EXIT_OK


def _cmd_gui(args) -> int:
    from . import gui  # lazy: tkinter may be missing

    return gui.run(args)


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        if args.command == "devices":
            return _cmd_devices(args)
        if args.command == "profile":
            return _cmd_profile(args)
        if args.command == "sink":
            return sink.run(args)
        if args.command == "source":
            return source.run(args)
        if args.command == "svc":
            return svc.run(args)
        if args.command == "gui":
            return _cmd_gui(args)
        if args.command == "bench":
            return _cmd_bench(args)
    except RegistryError as exc:
        print("error: %s" % exc)
        return EXIT_ERROR
    except KeyboardInterrupt:
        return EXIT_OK
    except BrokenPipeError:
        return EXIT_OK
    print("error: unknown command %r" % args.command)
    return EXIT_ARGS
