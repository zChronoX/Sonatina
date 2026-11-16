"""Standalone entry point for packaging the Sonatina Tkinter GUI (G2)."""

from __future__ import annotations

import os
import sys

PKG_PARENT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if PKG_PARENT not in sys.path:
    sys.path.insert(0, PKG_PARENT)

from sonatina.cli import main as cli_main


def main(argv: list[str] | None = None) -> int:
    raw = list(sys.argv[1:] if argv is None else argv)
    if "--hive" not in raw and "--root" not in raw and "--driver" not in raw:
        raw = ["--driver"] + raw
    return cli_main(["gui"] + raw)


if __name__ == "__main__":
    raise SystemExit(main())
