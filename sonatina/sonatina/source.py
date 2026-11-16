"""Bluetooth audio source: PCM -> codec -> RTP -> transport, with AVDTP negotiation first."""

from __future__ import annotations

import random
import time
import wave
from dataclasses import dataclass

import numpy as np

from . import avdtp, codecs, rtp
from . import EXIT_ENCODER, EXIT_NO_DEVICE, EXIT_OK, EXIT_UNSUPPORTED_CODEC
from .bdaddr import normalize
from .reg import Profile, Store, now_iso
from .sbc import SbcParams
from .transport import UdpTransport


class SourceError(Exception):
    pass


def log(message: str) -> None:
    print("source: %s" % message, flush=True)


@dataclass
class Session:
    sep: avdtp.SepInfo
    capability: avdtp.CodecCapability
    codec: str
    sampling: int
    channels: int
    sbc: SbcParams | None = None
    bitpool: int | None = None


def _exchange(
    transport: UdpTransport,
    message: avdtp.Message,
    retry: float = 0.5,
    total: float = 6.0,
) -> avdtp.Message:
    """Send a command until a response with the same transaction label arrives."""
    deadline = time.monotonic() + total
    next_send = time.monotonic()
    while time.monotonic() < deadline:
        now = time.monotonic()
        if now >= next_send:
            transport.send_signal(message.encode())
            next_send = now + retry
        got = transport.recv_signal(0.2)
        if got is None:
            continue
        data, _addr = got
        try:
            reply = avdtp.Message.decode(data)
        except avdtp.AvdtpError:
            continue
        if reply.transaction == message.transaction and reply.signal == message.signal:
            return reply
    raise avdtp.SignalTimeout("no response to %s" % message.name)


def negotiate(transport: UdpTransport, profile: Profile) -> Session:
    tx = 0

    reply = _exchange(transport, avdtp.command(avdtp.SIG_DISCOVER, transaction=tx))
    if reply.message_type != avdtp.MT_ACCEPT:
        raise avdtp.AvdtpError("Discover rejected")
    seps = avdtp.parse_sep_list(reply.payload)
    candidates = [sep for sep in seps if sep.tsep == avdtp.TSEP_SNK and not sep.in_use]
    if not candidates:
        raise avdtp.UnsupportedCodec("the sink advertises no free sink SEP")
    log("discover: %d SEP(s), %d usable sink SEP(s)" % (len(seps), len(candidates)))

    offered: list[tuple[avdtp.SepInfo, avdtp.CapabilitySet]] = []
    for sep in candidates:
        tx = (tx + 1) & 0x0F
        reply = _exchange(
            transport,
            avdtp.command(
                avdtp.SIG_GET_CAPABILITIES, avdtp.single_seid(sep.seid), transaction=tx
            ),
        )
        if reply.message_type != avdtp.MT_ACCEPT:
            continue
        try:
            caps = avdtp.parse_capabilities(reply.payload)
        except avdtp.AvdtpError:
            continue
        log("capabilities: seid=%d codec=%s" % (sep.seid, caps.codec))
        offered.append((sep, caps))

    if not offered:
        raise avdtp.UnsupportedCodec("the sink returned no usable capability set")

    found = avdtp.find_codec(offered, profile.codec)
    if found is None:
        names = ", ".join(sorted({caps.codec for _sep, caps in offered}))
        raise avdtp.UnsupportedCodec(
            "codec %r is not offered by the sink (offered: %s)" % (profile.codec, names)
        )
    sep, sink_caps = found

    if profile.codec == "sbc":
        if sink_caps.sbc is None:
            raise avdtp.UnsupportedCodec("sink SBC capability is missing")
        params = profile.sbc.clamped_to(sink_caps.sbc)
        capability = avdtp.CodecCapability("sbc", sbc=params)
        sampling, channels, bitpool = (
            params.sampling,
            params.channels,
            params.max_bitpool,
        )
    elif profile.codec == "aac":
        info = avdtp.AacInfo(
            sampling=profile.aac_sampling,
            channels=2 if profile.aac_channel_mode else 1,
            bitrate=profile.aac_bitrate,
        )
        if sink_caps.aac is not None:
            info.sampling = sink_caps.aac.sampling or info.sampling
            info.channels = min(info.channels, sink_caps.aac.channels or info.channels)
            if sink_caps.aac.bitrate:
                info.bitrate = min(info.bitrate, sink_caps.aac.bitrate)
        capability = avdtp.CodecCapability("aac", aac=info)
        sampling, channels, bitpool = info.sampling, info.channels, None
    elif profile.codec == "ldac":
        ldac_mode = {0: "mono", 1: "dual", 2: "stereo"}.get(
            profile.ldac_channel_mode, "stereo"
        )
        info = avdtp.LdacInfo(
            sampling=profile.ldac_sampling,
            channel_mode=ldac_mode,
            eqmid=profile.ldac_eqmid,
        )
        if sink_caps.ldac is not None:
            info.sampling = sink_caps.ldac.sampling or info.sampling
            info.channel_mode = sink_caps.ldac.channel_mode or info.channel_mode
        capability = avdtp.CodecCapability("ldac", ldac=info)
        sampling, channels, bitpool = info.sampling, info.channels, None
    else:
        capability = avdtp.CodecCapability(profile.codec)
        sampling, channels, bitpool = 48000, 2, None

    tx = (tx + 1) & 0x0F
    reply = _exchange(
        transport,
        avdtp.command(
            avdtp.SIG_SET_CONFIGURATION,
            avdtp.set_configuration(2, sep.seid, capability),
            transaction=tx,
        ),
    )
    if reply.message_type != avdtp.MT_ACCEPT:
        code = reply.payload[0] if reply.payload else 0
        raise avdtp.AvdtpError("SetConfiguration rejected (error 0x%02X)" % code)
    log(
        "configured: seid=%d codec=%s sampling=%d channels=%d%s"
        % (
            sep.seid,
            profile.codec,
            sampling,
            channels,
            " bitpool=%d" % bitpool if bitpool is not None else "",
        )
    )

    for signal in (avdtp.SIG_OPEN, avdtp.SIG_START):
        tx = (tx + 1) & 0x0F
        reply = _exchange(
            transport,
            avdtp.command(signal, avdtp.single_seid(sep.seid), transaction=tx),
        )
        if reply.message_type != avdtp.MT_ACCEPT:
            code = reply.payload[0] if reply.payload else 0
            raise avdtp.AvdtpError(
                "%s rejected (error 0x%02X)" % (avdtp.SIGNAL_NAMES[signal], code)
            )
        log("%s: accepted" % avdtp.SIGNAL_NAMES[signal])

    sbc = capability.sbc if profile.codec == "sbc" else None
    return Session(
        sep=sep,
        capability=capability,
        codec=profile.codec,
        sampling=sampling,
        channels=channels,
        sbc=sbc,
        bitpool=bitpool,
    )


# --------------------------------------------------------------------------- PCM sources


def _tone(duration: float, sampling: int, channels: int) -> np.ndarray:
    count = int(round(duration * sampling))
    t = np.arange(count, dtype=np.float32) / float(sampling)
    left = (np.sin(2.0 * np.pi * 440.0 * t) * 0.35).astype(np.float32)
    if channels == 1:
        pcm = left[:, None]
    else:
        right = (np.sin(2.0 * np.pi * 660.0 * t) * 0.35).astype(np.float32)
        pcm = np.stack([left, right], axis=1)
    return np.clip(pcm * 32767.0, -32768, 32767).astype(np.int16)


def _read_wav(path: str, sampling: int, channels: int) -> np.ndarray:
    with wave.open(path, "rb") as handle:
        source_channels = handle.getnchannels()
        width = handle.getsampwidth()
        rate = handle.getframerate()
        frames = handle.getnframes()
        raw = handle.readframes(frames)
    if width == 1:
        data = (np.frombuffer(raw, dtype=np.uint8).astype(np.int16) - 128) << 8
    elif width == 2:
        data = np.frombuffer(raw, dtype="<i2").astype(np.int16)
    elif width == 3:
        triplets = np.frombuffer(raw, dtype=np.uint8).reshape(-1, 3).astype(np.int32)
        value = triplets[:, 0] | (triplets[:, 1] << 8) | (triplets[:, 2] << 16)
        value = np.where(value & 0x800000, value - 0x1000000, value)
        data = (value >> 8).astype(np.int16)
    elif width == 4:
        data = (np.frombuffer(raw, dtype="<i4") >> 16).astype(np.int16)
    else:
        raise SourceError("unsupported WAV sample width: %d bytes" % width)
    pcm = data.reshape(-1, source_channels)
    if source_channels != channels:
        if channels == 1:
            pcm = pcm.astype(np.float32).mean(axis=1, keepdims=True)
        else:
            pcm = np.repeat(pcm[:, :1].astype(np.float32), channels, axis=1)
    pcm = pcm.astype(np.float32)
    if rate != sampling:
        count = int(round(len(pcm) * sampling / float(rate)))
        old = np.arange(len(pcm), dtype=np.float64)
        new = np.linspace(0.0, len(pcm) - 1.0, count)
        pcm = np.stack(
            [np.interp(new, old, pcm[:, ch]) for ch in range(pcm.shape[1])], axis=1
        )
    return np.clip(pcm, -32768, 32767).astype(np.int16)


def _loopback(duration: float, sampling: int, channels: int) -> np.ndarray:
    try:
        import soundcard as sc
    except ImportError as exc:
        raise codecs.EncoderUnavailable(
            "loopback capture needs the 'soundcard' package (uv sync --extra loopback)"
        ) from exc
    microphones = sc.all_microphones(include_loopback=True)
    if not microphones:
        raise codecs.EncoderUnavailable("no loopback capture device found")
    microphone = microphones[0]
    count = int(round(duration * sampling))
    with microphone.recorder(samplerate=sampling, channels=channels) as recorder:
        data = recorder.record(numframes=count)
    return np.clip(np.asarray(data, dtype=np.float32) * 32767.0, -32768, 32767).astype(
        np.int16
    )


def make_pcm(
    kind: str, duration: float, sampling: int, channels: int, wav_path: str | None
) -> np.ndarray:
    if kind == "wav":
        if not wav_path:
            raise SourceError("--pcm wav needs --wav <file>")
        return _read_wav(wav_path, sampling, channels)
    if kind == "loopback":
        return _loopback(duration, sampling, channels)
    if kind != "sine":
        raise SourceError("unknown PCM source %r" % kind)
    return _tone(duration, sampling, channels)


# --------------------------------------------------------------------------- run


def build_profile(args, store: Store) -> Profile:
    address = normalize(args.device)
    if args.profile_from_registry:
        profile = store.read_profile(address)
        if profile is None:
            raise SourceError(
                "no profile for %s (run: profile set --device %s --codec %s)"
                % (address, address, args.codec or "sbc")
            )
        if args.codec:
            profile.codec = args.codec
        return profile
    sbc = SbcParams(
        sampling=args.sampling,
        channel_mode="joint" if args.channels == 2 else "mono",
    )
    return Profile(device=address, codec=args.codec or "sbc", sbc=sbc)


def run(args) -> int:
    store = Store(args.hive, args.root)
    try:
        profile = build_profile(args, store)
    except (ValueError, SourceError) as exc:
        print("error: %s" % exc)
        return 2

    transport = UdpTransport(
        host=args.host,
        signal_port=args.signal_port,
        media_port=args.media_port,
    )
    started = False
    try:
        try:
            session = negotiate(transport, profile)
            started = True
        except avdtp.UnsupportedCodec as exc:
            print("error: %s" % exc)
            return EXIT_UNSUPPORTED_CODEC
        except avdtp.SignalTimeout as exc:
            print(
                "error: %s (is the sink running on %s:%d?)"
                % (exc, args.host, args.signal_port)
            )
            return EXIT_NO_DEVICE
        except avdtp.AvdtpError as exc:
            print("error: AVDTP negotiation failed: %s" % exc)
            return EXIT_UNSUPPORTED_CODEC

        try:
            pcm = make_pcm(
                args.pcm, args.duration, session.sampling, session.channels, args.wav
            )
        except (SourceError, codecs.CodecError) as exc:
            print("error: %s" % exc)
            return EXIT_ENCODER

        try:
            encoder = codecs.make_encoder(
                session.codec,
                session.sampling,
                session.channels,
                sbc=session.sbc,
                bitpool=session.bitpool,
                aac_bitrate=profile.aac_bitrate,
                frames_per_packet=args.frames_per_packet,
                ldac_eqmid=profile.ldac_eqmid,
            )
            payloads = encoder.payloads(pcm)
        except codecs.EncoderUnavailable as exc:
            print("error: %s" % exc)
            return EXIT_ENCODER
        except codecs.CodecError as exc:
            print("error: %s" % exc)
            return EXIT_ENCODER
        if not payloads:
            print("error: the encoder produced no payloads")
            return EXIT_ENCODER

        log(
            "stream: %s %d payload(s), %.2fs of PCM"
            % (session.codec, len(payloads), len(pcm) / float(session.sampling))
        )

        ssrc = random.getrandbits(32)
        packets_per_second_samples = payloads[0].samples
        delay_ms = int(round(packets_per_second_samples * 1000.0 / session.sampling))
        start = time.monotonic()
        last_report = start
        last_bytes = 0
        sent_samples = 0
        sent_bytes = 0
        tx_total_us = 0.0
        packets = 0

        store.write_current(
            profile.device,
            Codec=session.codec,
            Bitrate=0,
            Packets=0,
            Errors=0,
            TxTimeUs=0,
            DelayMs=delay_ms,
            Opened=1,
            UpdatedAt=now_iso(),
        )

        for index, payload in enumerate(payloads):
            packet = rtp.RtpPacket(
                seq=index & 0xFFFF,
                timestamp=rtp.timestamp_after(sent_samples, session.sampling),
                ssrc=ssrc,
                marker=(index == 0),
                payload=payload.data,
            )
            encoded = packet.encode()
            sent_at = time.perf_counter()
            transport.send_media(encoded)
            tx_total_us += (time.perf_counter() - sent_at) * 1e6
            packets += 1
            sent_bytes += len(encoded)
            sent_samples += payload.samples

            now = time.monotonic()
            if now - last_report >= 1.0:
                kbps = (sent_bytes - last_bytes) * 8.0 / (now - last_report) / 1000.0
                store.write_current(
                    profile.device,
                    Codec=session.codec,
                    Bitrate=int(round(kbps)),
                    Packets=packets,
                    Errors=0,
                    TxTimeUs=int(tx_total_us / max(1, packets)),
                    DelayMs=delay_ms,
                    Opened=1,
                    UpdatedAt=now_iso(),
                )
                last_report = now
                last_bytes = sent_bytes

            target = start + sent_samples / float(session.sampling)
            remaining = target - time.monotonic()
            if remaining > 0:
                time.sleep(remaining)

        elapsed = max(1e-9, time.monotonic() - start)
        kbps = sent_bytes * 8.0 / elapsed / 1000.0
        store.write_current(
            profile.device,
            Codec=session.codec,
            Bitrate=int(round(kbps)),
            Packets=packets,
            Errors=0,
            TxTimeUs=int(tx_total_us / max(1, packets)),
            DelayMs=delay_ms,
            Opened=0,
            UpdatedAt=now_iso(),
        )
        log(
            "sent codec=%s packets=%d bytes=%d bitrate_kbps=%.1f duration=%.2fs tx_avg_us=%.0f"
            % (
                session.codec,
                packets,
                sent_bytes,
                kbps,
                elapsed,
                tx_total_us / max(1, packets),
            )
        )
    finally:
        if started:
            try:
                transport.send_signal(
                    avdtp.command(
                        avdtp.SIG_ABORT, avdtp.single_seid(session.sep.seid)
                    ).encode()
                )
            except OSError:
                pass
        transport.close()
    return EXIT_OK
