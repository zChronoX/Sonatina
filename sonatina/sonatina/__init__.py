"""Sonatina - a small, self-contained Bluetooth audio source and sink playground.

Nothing here talks to a real headset: the data plane runs on UDP loopback and the
headset simulator negotiates codecs with AVDTP and measures the stream.
"""

__version__ = "0.1.0"

CODECS = ("pcm", "sbc", "aac", "aptx")

REG_ROOT = r"Software\Sonatina"

SIGNAL_PORT = 5005
MEDIA_PORT = 5004

# Exit codes (documented in README.md)
EXIT_OK = 0
EXIT_ERROR = 1
EXIT_ARGS = 2
EXIT_UNSUPPORTED_CODEC = 3
EXIT_NO_DEVICE = 4
EXIT_ENCODER = 5
