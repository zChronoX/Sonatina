"""Bluetooth device address helpers."""

from __future__ import annotations

import re

_SEP = re.compile(r"[:\-.\s]")
_INSTANCE = re.compile(r"&([0-9A-Fa-f]{12})_C[0-9A-Fa-f]{8}")
# Six groups of two hex digits, optionally separated by one of : - . or space.
_ADDR = re.compile(r"^\s*[0-9A-Fa-f]{2}(?:[:\-.\s]?[0-9A-Fa-f]{2}){5}\s*$")


def normalize(addr: str) -> str:
    """Return the 12 hex digits uppercase, no separators. Raise ValueError if invalid."""
    if not isinstance(addr, str):
        raise ValueError("device address must be a string")
    if not _ADDR.match(addr):
        raise ValueError("invalid Bluetooth address: %r" % addr)
    return _SEP.sub("", addr).upper()


def is_valid(addr: str) -> bool:
    try:
        normalize(addr)
        return True
    except ValueError:
        return False


def colon(addr: str) -> str:
    c = normalize(addr)
    return ":".join(c[i : i + 2] for i in range(0, 12, 2))


def from_instance_id(instance: str) -> str | None:
    """Extract the remote address from a BTHENUM instance id like
    'BTHENUM\\{0000110b-...}_LOCALMFG&0b3b\\7&2c1f&0&9880BB418BA7_C00000000'."""
    m = _INSTANCE.search(instance or "")
    return m.group(1).upper() if m else None
