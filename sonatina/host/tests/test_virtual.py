"""pytest entry point (M7, 1.3.0): every *_virtual.py script is one test, run in its own
process (they start their own event loop and virtual controllers).

    cd sonatina/host/tests
    python -m pytest -q            # all
    python -m pytest -q -k codec   # one

The virtual controllers run in real time: under a loaded CI machine e2e, latency and hfp
have missed a deadline now and then, so a failed script gets one more try (logged)."""
import os
import subprocess
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPTS = sorted(f for f in os.listdir(HERE) if f.endswith("_virtual.py"))
TIMEOUT_S = 600


def _run(script: str) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, script], cwd=HERE, capture_output=True, text=True,
                          timeout=TIMEOUT_S)


@pytest.mark.parametrize("script", SCRIPTS)
def test_virtual(script: str) -> None:
    result = _run(script)
    if result.returncode != 0:
        print(f"{script} failed once, retrying:\n{result.stdout[-3000:]}\n{result.stderr[-3000:]}")
        result = _run(script)
    assert result.returncode == 0, f"{script}\n{result.stdout[-4000:]}\n{result.stderr[-4000:]}"
