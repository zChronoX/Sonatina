"""T4: Quality metrics sweep and Markdown/JSON report generator.

Runs `loopback_e2e.exe` against `python -m sonatina sink` across multiple SBC
bitpools (and simulated congestion) and emits a structured quality table.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from dataclasses import asdict, dataclass

TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
PKG_ROOT = os.path.abspath(os.path.join(TESTS_DIR, "..", ".."))
DEFAULT_EXE = os.path.join(TESTS_DIR, "build", "loopback_e2e.exe")

_KV_RE = re.compile(r"([A-Za-z0-9_]+)=([^\s]+)")


@dataclass
class QualitySample:
    case: str
    bitpool_req: int
    congested: bool
    init_bp: int
    final_bp: int
    abr_steps: int
    c_packets: int
    sink_packets: int
    lost: int
    errors: int
    tx_avg_us: int
    tx_max_us: int
    measured_kbps: float
    expected_kbps: float
    sink_wire_kbps: float
    decoded_rms: float
    sink_rms: float
    trace_events: int


def parse_kv_line(line: str) -> dict[str, str]:
    return dict(_KV_RE.findall(line))


def parse_run_output(
    case: str, bitpool_req: int, congested: bool, output: str
) -> QualitySample:
    t2_kv: dict[str, str] = {}
    sink_kv: dict[str, str] = {}
    for raw in output.splitlines():
        line = raw.strip()
        if line.startswith("T2_LOOPBACK "):
            t2_kv = parse_kv_line(line)
        elif line.startswith("sink: codec="):
            sink_kv = parse_kv_line(line)

    if not t2_kv:
        raise ValueError("missing T2_LOOPBACK summary line in output")
    if not sink_kv:
        raise ValueError("missing sink summary line in output")

    return QualitySample(
        case=case,
        bitpool_req=bitpool_req,
        congested=congested,
        init_bp=int(t2_kv["init_bp"]),
        final_bp=int(t2_kv["final_bp"]),
        abr_steps=int(t2_kv["abr_steps"]),
        c_packets=int(t2_kv["packets"]),
        sink_packets=int(sink_kv["packets"]),
        lost=int(sink_kv["lost"]),
        errors=int(sink_kv["errors"]),
        tx_avg_us=int(t2_kv["tx_avg_us"]),
        tx_max_us=int(t2_kv["tx_max_us"]),
        measured_kbps=float(t2_kv["measured_kbps"]),
        expected_kbps=float(t2_kv["expected_kbps"]),
        sink_wire_kbps=float(sink_kv["wire_kbps"]),
        decoded_rms=float(t2_kv["decoded_rms"]),
        sink_rms=float(sink_kv["rms"]),
        trace_events=int(t2_kv["trace_events"]),
    )


def format_markdown(samples: list[QualitySample]) -> str:
    lines = [
        "# Sonatina T4 Quality Report",
        "",
        "| Case | Bitpool (init->final) | ABR steps | Packets (C/Sink) | Lost | Errors | Tx Avg/Max (us) | Measured / Expected / Wire (kbps) | Decoded RMS |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    for s in samples:
        lines.append(
            "| %s | %d -> %d | %d | %d / %d | %d | %d | %d / %d | %.1f / %.1f / %.1f | %.3f |"
            % (
                s.case,
                s.init_bp,
                s.final_bp,
                s.abr_steps,
                s.c_packets,
                s.sink_packets,
                s.lost,
                s.errors,
                s.tx_avg_us,
                s.tx_max_us,
                s.measured_kbps,
                s.expected_kbps,
                s.sink_wire_kbps,
                s.decoded_rms,
            )
        )
    return "\n".join(lines) + "\n"


def _ensure_loopback_binary() -> str:
    ps1 = os.path.join(TESTS_DIR, "run_loopback_e2e.ps1")
    if os.path.exists(DEFAULT_EXE):
        return DEFAULT_EXE
    # Compile via run_loopback_e2e.ps1 -Seconds 1 on an unused port if missing
    res = subprocess.run(
        [
            "powershell",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            ps1,
            "-Seconds",
            "1",
            "-SignalPort",
            "5205",
            "-MediaPort",
            "5204",
        ],
        capture_output=True,
        text=True,
    )
    if res.returncode != 0 or not os.path.exists(DEFAULT_EXE):
        raise RuntimeError(
            "failed to build loopback_e2e.exe: %s\n%s" % (res.stdout, res.stderr)
        )
    return DEFAULT_EXE


def run_case(
    exe: str,
    case: str,
    bitpool: int,
    seconds: int,
    congested: bool,
    signal_port: int,
    media_port: int,
) -> QualitySample:
    sink_cmd = [
        sys.executable,
        "-m",
        "sonatina",
        "sink",
        "--caps",
        "pcm,sbc",
        "--wait",
        "15",
        "--duration",
        str(seconds + 4),
        "--idle",
        "1.0",
        "--out",
        os.path.join(os.environ.get("TEMP", "."), f"sonatina-t4-{case}.wav"),
        "--signal-port",
        str(signal_port),
        "--media-port",
        str(media_port),
    ]
    sink = subprocess.Popen(
        sink_cmd,
        cwd=PKG_ROOT,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        # Wait until sink prints listening banner
        sink_lines: list[str] = []
        assert sink.stdout is not None
        for line in sink.stdout:
            sink_lines.append(line)
            if "listening on" in line:
                break

        c_cmd = [
            exe,
            "--signal-port",
            str(signal_port),
            "--media-port",
            str(media_port),
            "--seconds",
            str(seconds),
            "--bitpool",
            str(bitpool),
        ]
        if congested:
            c_cmd.append("--simulate-congestion")
        c_proc = subprocess.run(c_cmd, capture_output=True, text=True)
        rest_out, sink_err = sink.communicate(timeout=20)
        sink_lines.append(rest_out)
        if c_proc.returncode != 0 or sink.returncode != 0:
            raise RuntimeError(
                "case %s failed (c=%d, sink=%d)\n%s\n%s\n%s"
                % (
                    case,
                    c_proc.returncode,
                    sink.returncode or 0,
                    c_proc.stdout,
                    c_proc.stderr,
                    "".join(sink_lines) + sink_err,
                )
            )
        combined = c_proc.stdout + "\n" + "".join(sink_lines)
        return parse_run_output(case, bitpool, congested, combined)
    finally:
        if sink.poll() is None:
            sink.kill()
        wav = os.path.join(os.environ.get("TEMP", "."), f"sonatina-t4-{case}.wav")
        if os.path.exists(wav):
            try:
                os.remove(wav)
            except OSError:
                pass


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Sonatina T4 quality sweep")
    parser.add_argument("--seconds", type=int, default=2)
    parser.add_argument("--base-port", type=int, default=5210)
    parser.add_argument("--out-md", default="")
    parser.add_argument("--out-json", default="")
    args = parser.parse_args(argv)

    exe = _ensure_loopback_binary()
    cases = [
        ("sbc_bp24_clean", 24, False),
        ("sbc_bp35_clean", 35, False),
        ("sbc_bp53_clean", 53, False),
        ("sbc_bp53_congested", 53, True),
    ]
    samples: list[QualitySample] = []
    for idx, (name, bp, cong) in enumerate(cases):
        sig_port = args.base_port + idx * 10 + 1
        med_port = args.base_port + idx * 10
        samples.append(run_case(exe, name, bp, args.seconds, cong, sig_port, med_port))

    md = format_markdown(samples)
    print(md, end="")
    if args.out_md:
        with open(args.out_md, "w", encoding="utf-8") as fh:
            fh.write(md)
    if args.out_json:
        with open(args.out_json, "w", encoding="utf-8") as fh:
            json.dump([asdict(s) for s in samples], fh, indent=2)
    return 0


if __name__ == "__main__":
    sys.exit(main())
