"""Regenerate the synthetic engine fixtures if they are missing (fresh clone, or someone cleaned tests/fixtures)."""
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
FIX = HERE / "fixtures"


def pytest_sessionstart(session):
    if (FIX / "market.json").exists():
        return
    # the engine set builds on the v2.0 histories; without those, regenerate everything
    only = ["--engine-only"] if (FIX / "UPCO_history.csv").exists() else []
    subprocess.run([sys.executable, str(HERE / "make_fixtures.py"), str(FIX), *only], check=True)
