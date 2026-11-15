"""Generate and optionally verify AVDTP/RTP/SBC byte vectors from sonatina/.

Usage:
    python generate_vectors.py                 # print all vectors
    python generate_vectors.py --write out.txt # write vectors to a text file
    python generate_vectors.py --verify host_test.exe
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from sonatina import avdtp  # noqa: E402
from sonatina.rtp import RtpPacket, pack_sbc  # noqa: E402
from sonatina.sbc import SbcParams  # noqa: E402


def _pattern(length: int, seed: int) -> bytes:
    return bytes((seed + i * 7) & 0xFF for i in range(length))


def build_vectors() -> tuple[dict[str, str], dict[str, tuple[int, int]]]:
    sbc_default = SbcParams(48000, "joint", 16, 8, "loudness", 2, 53)
    sbc_44k_stereo = SbcParams(44100, "stereo", 8, 4, "snr", 4, 35)
    sbc_16k_mono = SbcParams(16000, "mono", 12, 8, "loudness", 2, 63)
    sbc_32k_dual = SbcParams(32000, "dual", 4, 4, "snr", 8, 48)

    cap_sbc_default = avdtp.encode_capabilities(
        avdtp.CodecCapability("sbc", sbc=sbc_default)
    )
    cap_sbc_44k = avdtp.encode_capabilities(
        avdtp.CodecCapability("sbc", sbc=sbc_44k_stereo)
    )
    cap_pcm = avdtp.encode_capabilities(avdtp.CodecCapability("pcm"))

    set_cfg_default_payload = avdtp.set_configuration(
        2, 1, avdtp.CodecCapability("sbc", sbc=sbc_default)
    )
    set_cfg_44k_payload = avdtp.set_configuration(
        1, 3, avdtp.CodecCapability("sbc", sbc=sbc_44k_stereo)
    )

    seps_payload = avdtp.encode_sep_list(
        [
            avdtp.SepInfo(seid=1, tsep=avdtp.TSEP_SNK),
            avdtp.SepInfo(seid=2, tsep=avdtp.TSEP_SRC),
        ]
    )

    rtp_basic = RtpPacket(seq=1, timestamp=240, ssrc=0x12345678, marker=False).encode()
    rtp_max = RtpPacket(
        seq=65535, timestamp=0xFFFFFFFF, ssrc=0xDEADBEEF, marker=True
    ).encode()

    frames_1 = [_pattern(126, 0x00)]
    frames_3_raw = _pattern(3 * 68, 0xA0)
    frames_3 = [frames_3_raw[i * 68 : (i + 1) * 68] for i in range(3)]
    frames_15_raw = _pattern(15 * 14, 0x55)
    frames_15 = [frames_15_raw[i * 14 : (i + 1) * 14] for i in range(15)]

    payload_3 = pack_sbc(frames_3)
    full_rtp_sbc = (
        RtpPacket(
            seq=1, timestamp=240, ssrc=0x12345678, marker=False, payload=payload_3
        ).encode()
    )

    vectors: dict[str, bytes] = {
        "sbc_cap_default": sbc_default.to_capability(),
        "sbc_cap_44k_stereo": sbc_44k_stereo.to_capability(),
        "sbc_cap_16k_mono": sbc_16k_mono.to_capability(),
        "sbc_cap_32k_dual": sbc_32k_dual.to_capability(),
        "avdtp_discover_cmd": avdtp.command(avdtp.SIG_DISCOVER, transaction=0).encode(),
        "avdtp_discover_cmd_tx15": avdtp.command(
            avdtp.SIG_DISCOVER, transaction=15
        ).encode(),
        "avdtp_discover_accept_2sep": avdtp.command(
            avdtp.SIG_DISCOVER, transaction=0
        )
        .accept(seps_payload)
        .encode(),
        "avdtp_get_caps_cmd_seid1": avdtp.command(
            avdtp.SIG_GET_CAPABILITIES, avdtp.single_seid(1), transaction=1
        ).encode(),
        "avdtp_get_caps_cmd_seid7": avdtp.command(
            avdtp.SIG_GET_CAPABILITIES, avdtp.single_seid(7), transaction=7
        ).encode(),
        "avdtp_caps_sbc_default": cap_sbc_default,
        "avdtp_get_caps_accept_sbc": avdtp.command(
            avdtp.SIG_GET_CAPABILITIES, transaction=1
        )
        .accept(cap_sbc_default)
        .encode(),
        "avdtp_caps_sbc_44k_stereo": cap_sbc_44k,
        "avdtp_caps_pcm": cap_pcm,
        "avdtp_set_config_payload_default": set_cfg_default_payload,
        "avdtp_set_config_cmd_default": avdtp.command(
            avdtp.SIG_SET_CONFIGURATION, set_cfg_default_payload, transaction=2
        ).encode(),
        "avdtp_set_config_cmd_44k": avdtp.command(
            avdtp.SIG_SET_CONFIGURATION, set_cfg_44k_payload, transaction=3
        ).encode(),
        "avdtp_open_cmd": avdtp.command(
            avdtp.SIG_OPEN, avdtp.single_seid(1), transaction=4
        ).encode(),
        "avdtp_start_cmd": avdtp.command(
            avdtp.SIG_START, avdtp.single_seid(1), transaction=5
        ).encode(),
        "avdtp_suspend_cmd": avdtp.command(
            avdtp.SIG_SUSPEND, avdtp.single_seid(1), transaction=6
        ).encode(),
        "avdtp_close_cmd": avdtp.command(
            avdtp.SIG_CLOSE, avdtp.single_seid(1), transaction=7
        ).encode(),
        "avdtp_abort_cmd": avdtp.command(
            avdtp.SIG_ABORT, avdtp.single_seid(1), transaction=8
        ).encode(),
        "avdtp_reject_bad_seid": avdtp.command(
            avdtp.SIG_GET_CAPABILITIES, transaction=9
        )
        .reject(avdtp.ERR_BAD_ACP_SEID)
        .encode(),
        "rtp_hdr_basic": rtp_basic,
        "rtp_hdr_max_marker": rtp_max,
        "rtp_sbc_payload_1frame": pack_sbc(frames_1),
        "rtp_sbc_payload_3frames": payload_3,
        "rtp_full_packet_sbc": full_rtp_sbc,
        "rtp_sbc_payload_15frames": pack_sbc(frames_15),
    }

    math_cases = {
        "sbc_default_bp53": (
            sbc_default.frame_len(53),
            round(sbc_default.bitrate_kbps(53) * 1000),
        ),
        "sbc_default_bp24": (
            sbc_default.frame_len(24),
            round(sbc_default.bitrate_kbps(24) * 1000),
        ),
        "sbc_44k_stereo_bp35": (
            sbc_44k_stereo.frame_len(35),
            round(sbc_44k_stereo.bitrate_kbps(35) * 1000),
        ),
        "sbc_16k_mono_bp63": (
            sbc_16k_mono.frame_len(63),
            round(sbc_16k_mono.bitrate_kbps(63) * 1000),
        ),
        "sbc_32k_dual_bp48": (
            sbc_32k_dual.frame_len(48),
            round(sbc_32k_dual.bitrate_kbps(48) * 1000),
        ),
    }

    return {k: v.hex() for k, v in vectors.items()}, math_cases


def verify_binary(exe_path: str) -> int:
    expected_vec, expected_math = build_vectors()
    proc = subprocess.run([exe_path], capture_output=True, text=True, check=False)
    if proc.returncode != 0:
        print("C test binary exited with %d" % proc.returncode, file=sys.stderr)
        print(proc.stdout, file=sys.stderr)
        print(proc.stderr, file=sys.stderr)
        return 1

    got_vec: dict[str, str] = {}
    got_math: dict[str, tuple[int, int]] = {}
    saw_ok = False
    for line in proc.stdout.splitlines():
        parts = line.strip().split()
        if not parts:
            continue
        if parts[0] == "VECTOR" and len(parts) == 3:
            got_vec[parts[1]] = parts[2]
        elif parts[0] == "MATH" and len(parts) == 4:
            got_math[parts[1]] = (int(parts[2]), int(parts[3]))
        elif parts[0] == "HOST_TESTS_OK":
            saw_ok = True

    failures = 0
    for name, exp_hex in expected_vec.items():
        got_hex = got_vec.get(name)
        if got_hex != exp_hex:
            print("FAIL %s: python=%s c=%s" % (name, exp_hex, got_hex))
            failures += 1
        else:
            print("PASS %s (%s)" % (name, exp_hex[:32] + ("..." if len(exp_hex) > 32 else "")))

    for name, exp_vals in expected_math.items():
        got_vals = got_math.get(name)
        if got_vals != exp_vals:
            print("FAIL %s: python=%r c=%r" % (name, exp_vals, got_vals))
            failures += 1
        else:
            print("PASS %s %r" % (name, exp_vals))

    if not saw_ok:
        print("FAIL: missing HOST_TESTS_OK marker")
        failures += 1

    print(
        "Summary: %d vectors + %d math cases checked, %d failures"
        % (len(expected_vec), len(expected_math), failures)
    )
    return 1 if failures else 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Sonatina protocol vector generator")
    parser.add_argument("--write", metavar="PATH", help="Write vectors to file")
    parser.add_argument("--verify", metavar="EXE", help="Run host_test and compare")
    args = parser.parse_args(argv)

    if args.verify:
        return verify_binary(args.verify)

    vectors, math_cases = build_vectors()
    lines = [f"VECTOR {k} {v}" for k, v in vectors.items()]
    lines.extend(f"MATH {k} {v[0]} {v[1]}" for k, v in math_cases.items())
    output = "\n".join(lines) + "\n"
    if args.write:
        with open(args.write, "w", encoding="utf-8") as fh:
            fh.write(output)
    else:
        sys.stdout.write(output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
