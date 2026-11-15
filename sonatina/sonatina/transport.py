"""UDP transports for Sonatina.

The default transport is UDP on the loopback interface: the AVDTP signalling
socket (5005) and the RTP media socket (5004) mirror the L2CAP PSM roles of the
real protocol without touching a Bluetooth adapter.

An experimental L2CAP transport is documented in the README; it is not enabled
by default because the inbox Windows Bluetooth audio driver normally owns the connection.
"""

from __future__ import annotations

import select
import socket
from dataclasses import dataclass


def _recvfrom(sock: socket.socket, wait: float):
    """Receive one datagram, treating a Windows ICMP reset as "nothing yet".

    On Windows an unreachable UDP peer surfaces as ``WSAECONNRESET`` on the next
    receive; for a discovery loop that simply means the other side is not up.
    """
    ready, _, _ = select.select([sock], [], [], max(0.0, wait))
    if not ready:
        return None
    try:
        return sock.recvfrom(65535)
    except OSError as exc:
        if (
            isinstance(exc, ConnectionResetError)
            or getattr(exc, "winerror", None) == 10054
        ):
            return None
        raise


@dataclass
class UdpTransport:
    host: str = "127.0.0.1"
    signal_port: int = 5005
    media_port: int = 5004
    bind_signal: bool = False
    bind_media: bool = False
    timeout: float = 2.0

    def __post_init__(self) -> None:
        self.signal_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.media_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        for sock in (self.signal_sock, self.media_sock):
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        if self.bind_signal:
            self.signal_sock.bind((self.host, self.signal_port))
        if self.bind_media:
            self.media_sock.bind((self.host, self.media_port))
        self.signal_sock.setblocking(False)
        self.media_sock.setblocking(False)
        self.sent_signals = 0
        self.sent_media = 0
        self.sent_media_bytes = 0

    # -- signalling ---------------------------------------------------------

    def send_signal(self, data: bytes) -> None:
        try:
            self.signal_sock.sendto(data, (self.host, self.signal_port))
        except ConnectionResetError:
            return
        self.sent_signals += 1

    def reply_signal(self, data: bytes, addr: tuple[str, int]) -> None:
        try:
            self.signal_sock.sendto(data, addr)
        except ConnectionResetError:
            pass

    def recv_signal(
        self, timeout: float | None = None
    ) -> tuple[bytes, tuple[str, int]] | None:
        wait = self.timeout if timeout is None else timeout
        got = _recvfrom(self.signal_sock, wait)
        return got

    # -- media --------------------------------------------------------------

    def send_media(self, data: bytes) -> None:
        try:
            self.media_sock.sendto(data, (self.host, self.media_port))
        except ConnectionResetError:
            return
        self.sent_media += 1
        self.sent_media_bytes += len(data)

    def recv_media(
        self, timeout: float | None = None
    ) -> tuple[bytes, tuple[str, int]] | None:
        wait = self.timeout if timeout is None else timeout
        return _recvfrom(self.media_sock, wait)

    def close(self) -> None:
        for sock in (self.signal_sock, self.media_sock):
            try:
                sock.close()
            except OSError:
                pass

    def __enter__(self) -> "UdpTransport":
        return self

    def __exit__(self, *exc_info) -> None:
        self.close()
