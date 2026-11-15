"""Headset simulator: AVDTP acceptor, RTP receiver, WAV writer and statistics."""

from __future__ import annotations

import time
import wave
from dataclasses import dataclass, field

import numpy as np

from . import avdtp, codecs, rtp
from . import EXIT_ENCODER, EXIT_NO_DEVICE, EXIT_OK
from .bdaddr import normalize
from .reg import Store
from .sbc import SbcParams
from .transport import UdpTransport

KNOWN_CODECS = ("pcm", "sbc", "aac", "aptx")


class SinkError(Exception):
    pass


def log(message: str) -> None:
    print("sink: %s" % message, flush=True)


@dataclass
class SinkSep:
    sep: avdtp.SepInfo
    capability: avdtp.CodecCapability


@dataclass
class SinkSession:
    seps: list[SinkSep]
    configured: tuple[SinkSep, avdtp.CodecCapability] | None = None
    started: bool = False

    def _log(self, message: str) -> None:
        log(message)

    def _find(self, seid: int) -> SinkSep | None:
        for item in self.seps:
            if item.sep.seid == seid:
                return item
        return None

    def handle(self, message: avdtp.Message) -> avdtp.Message | None:
        if message.packet_type != avdtp.PT_SINGLE:
            return message.reject(avdtp.ERR_BAD_HEADER_FORMAT)
        if message.message_type != avdtp.MT_COMMAND:
            return None

        if message.signal == avdtp.SIG_DISCOVER:
            payload = avdtp.encode_sep_list([item.sep for item in self.seps])
            return message.accept(payload)

        if message.signal == avdtp.SIG_GET_CAPABILITIES:
            if len(message.payload) < 1:
                return message.reject(avdtp.ERR_BAD_LENGTH)
            item = self._find(message.payload[0] & 0x3F)
            if item is None:
                return message.reject(avdtp.ERR_BAD_ACP_SEID)
            return message.accept(avdtp.encode_capabilities(item.capability))

        if message.signal == avdtp.SIG_SET_CONFIGURATION:
            if len(message.payload) < 2:
                return message.reject(avdtp.ERR_BAD_LENGTH)
            acp_seid = message.payload[0] & 0x3F
            item = self._find(acp_seid)
            if item is None:
                return message.reject(avdtp.ERR_BAD_ACP_SEID)
            try:
                requested = avdtp.parse_capabilities(message.payload[2:])
            except (avdtp.AvdtpError, ValueError):
                return message.reject(avdtp.ERR_INVALID_CAPABILITIES)
            if requested.codec != item.capability.codec:
                self._log(
                    "reject SetConfiguration: sink SEP %d offers %s, source asked %s"
                    % (acp_seid, item.capability.codec, requested.codec)
                )
                return message.reject(avdtp.ERR_INVALID_CAPABILITIES)
            if requested.codec == "sbc":
                advertised = item.capability.sbc
                chosen = requested.sbc
                if advertised is None or chosen is None:
                    return message.reject(avdtp.ERR_INVALID_CAPABILITIES)
                if (
                    chosen.sampling != advertised.sampling
                    or chosen.channel_mode != advertised.channel_mode
                    or chosen.blocks != advertised.blocks
                    or chosen.subbands != advertised.subbands
                    or chosen.allocation != advertised.allocation
                    or chosen.min_bitpool < 2
                    or chosen.max_bitpool > advertised.max_bitpool
                ):
                    self._log(
                        "reject SetConfiguration: SBC parameters outside the advertised set"
                    )
                    return message.reject(avdtp.ERR_INVALID_CAPABILITIES)
            item.sep.in_use = True
            self.configured = (item, requested)
            detail = requested.codec
            if requested.sbc:
                detail += " sampling=%d mode=%s bitpool=%d" % (
                    requested.sbc.sampling,
                    requested.sbc.channel_mode,
                    requested.sbc.max_bitpool,
                )
            self._log("configured: codec=%s" % detail)
            return message.accept()

        if message.signal in (avdtp.SIG_OPEN, avdtp.SIG_START):
            if self.configured is None:
                return message.reject(avdtp.ERR_SEP_NOT_IN_USE)
            if len(message.payload) < 1:
                return message.reject(avdtp.ERR_BAD_LENGTH)
            item = self._find(message.payload[0] & 0x3F)
            if item is None:
                return message.reject(avdtp.ERR_BAD_ACP_SEID)
            if message.signal == avdtp.SIG_START:
                self.started = True
                self._log("streaming started")
            else:
                self._log("stream open")
            return message.accept()

        if message.signal in (avdtp.SIG_CLOSE, avdtp.SIG_SUSPEND, avdtp.SIG_ABORT):
            if self.configured is not None:
                self.configured[0].sep.in_use = False
            self.started = False
            return message.accept()

        return message.reject(avdtp.ERR_BAD_SERVICE_CATEGORY)


def parse_caps(value: str) -> list[str]:
    names = [item.strip().lower() for item in value.split(",") if item.strip()]
    if not names:
        raise SinkError(
            "--caps must list at least one codec (%s)" % ", ".join(KNOWN_CODECS)
        )
    unknown = [name for name in names if name not in KNOWN_CODECS]
    if unknown:
        raise SinkError("unknown codec(s) in --caps: %s" % ", ".join(unknown))
    ordered = []
    for name in names:
        if name not in ordered:
            ordered.append(name)
    return ordered


def build_seps(
    names: list[str], sampling: int, max_bitpool: int, aac_bitrate: int
) -> list[SinkSep]:
    seps = []
    for seid, name in enumerate(names, start=1):
        if name == "sbc":
            params = SbcParams(
                sampling=sampling,
                channel_mode="joint",
                min_bitpool=2,
                max_bitpool=max_bitpool,
            )
            capability = avdtp.CodecCapability("sbc", sbc=params)
        elif name == "aac":
            capability = avdtp.CodecCapability(
                "aac",
                aac=avdtp.AacInfo(sampling=sampling, channels=2, bitrate=aac_bitrate),
            )
        else:
            capability = avdtp.CodecCapability(name)
        seps.append(
            SinkSep(
                avdtp.SepInfo(seid=seid, in_use=False, tsep=avdtp.TSEP_SNK), capability
            )
        )
    return seps


def write_wav(path: str, pcm: np.ndarray, sampling: int) -> None:
    channels = pcm.shape[1] if pcm.ndim > 1 else 1
    with wave.open(path, "wb") as handle:
        handle.setnchannels(channels)
        handle.setsampwidth(2)
        handle.setframerate(sampling)
        handle.writeframes(pcm.astype("<i2").tobytes())


def run(args) -> int:
    try:
        names = parse_caps(args.caps)
    except SinkError as exc:
        print("error: %s" % exc)
        return 2

    seps = build_seps(names, args.sampling, args.max_bitpool, args.aac_bitrate)
    session = SinkSession(seps)
    transport = UdpTransport(
        host=args.host,
        signal_port=args.signal_port,
        media_port=args.media_port,
        bind_signal=True,
        bind_media=True,
    )
    log(
        "listening on %s:%d (signalling) and %s:%d (media), caps=%s"
        % (args.host, args.signal_port, args.host, args.media_port, ",".join(names))
    )

    try:
        handshake_deadline = time.monotonic() + args.wait
        while not session.started and time.monotonic() < handshake_deadline:
            got = transport.recv_signal(0.2)
            if got is None:
                continue
            data, addr = got
            try:
                message = avdtp.Message.decode(data)
            except avdtp.AvdtpError as exc:
                log("ignoring malformed signalling packet: %s" % exc)
                continue
            log("<- %s (tx=%d)" % (message.name, message.transaction))
            reply = session.handle(message)
            if reply is not None:
                transport.reply_signal(reply.encode(), addr)

        if not session.started or session.configured is None:
            log("no source connected within %.1fs" % args.wait)
            return EXIT_NO_DEVICE

        item, capability = session.configured
        codec = capability.codec
        sbc = capability.sbc if codec == "sbc" else None
        bitpool = sbc.max_bitpool if sbc else None
        sampling = (
            sbc.sampling
            if sbc
            else (capability.aac.sampling if capability.aac else args.sampling)
        )
        channels = (
            sbc.channels if sbc else (capability.aac.channels if capability.aac else 2)
        )

        try:
            decoder = codecs.make_decoder(
                codec, sampling, channels, sbc=sbc, bitpool=bitpool
            )
        except codecs.CodecError as exc:
            print("error: %s" % exc)
            return EXIT_ENCODER

        if args.device:
            try:
                store = Store(args.hive, args.root)
                store.write_capability(
                    normalize(args.device),
                    codecs=names,
                    sbc=next(
                        (
                            sep.capability.sbc
                            for sep in seps
                            if sep.capability.codec == "sbc"
                        ),
                        None,
                    ),
                    aac_bitrate=args.aac_bitrate,
                )
            except Exception as exc:  # registry is optional for the demo
                log("warning: cannot write Capability to the registry: %s" % exc)

        tracker = rtp.SequenceTracker()
        packets = 0
        errors = 0
        payload_bytes = 0
        wire_bytes = 0
        first_time = None
        last_time = None
        media_deadline = time.monotonic() + args.duration
        aborted = False

        while time.monotonic() < media_deadline:
            if packets and time.monotonic() - last_time > args.idle:
                break
            got = transport.recv_media(0.2)
            if got is None:
                control = transport.recv_signal(0.0)
                if control is not None:
                    data, addr = control
                    try:
                        message = avdtp.Message.decode(data)
                    except avdtp.AvdtpError:
                        continue
                    reply = session.handle(message)
                    if reply is not None:
                        transport.reply_signal(reply.encode(), addr)
                    if message.signal == avdtp.SIG_ABORT:
                        aborted = True
                        break
                continue
            data, _addr = got
            wire_bytes += len(data)
            try:
                packet = rtp.RtpPacket.decode(data)
                tracker.feed(packet.seq)
                decoder.push(packet.payload)
            except (rtp.RtpError, avdtp.AvdtpError, ValueError):
                errors += 1
                continue
            now = time.monotonic()
            if first_time is None:
                first_time = now
            last_time = now
            packets += 1
            payload_bytes += len(packet.payload)

        try:
            pcm = decoder.finish()
        except codecs.CodecError as exc:
            print("error: %s" % exc)
            return EXIT_ENCODER

        duration = len(pcm) / float(sampling) if sampling else 0.0
        rms = (
            float(np.sqrt(np.mean((pcm.astype(np.float64) / 32768.0) ** 2)))
            if len(pcm)
            else 0.0
        )
        if args.out:
            write_wav(args.out, pcm, sampling)
        bitrate = payload_bytes * 8.0 / duration / 1000.0 if duration > 0 else 0.0
        wall = (last_time - first_time) if (first_time and last_time) else 0.0
        wire = wire_bytes * 8.0 / wall / 1000.0 if wall > 0 else 0.0
        log(
            "codec=%s packets=%d lost=%d errors=%d bytes=%d bitrate_kbps=%.1f wire_kbps=%.1f "
            "duration=%.3f rms=%.3f out=%s%s"
            % (
                codec,
                packets,
                tracker.lost,
                errors,
                payload_bytes,
                bitrate,
                wire,
                duration,
                rms,
                args.out or "-",
                " aborted" if aborted else "",
            )
        )
        if packets == 0:
            log("the session started but no media packet arrived")
            return EXIT_NO_DEVICE
    finally:
        transport.close()
    return EXIT_OK
