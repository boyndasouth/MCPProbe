"""Starts the reference server after a delay, like a cold `npx` download."""

import runpy
import sys
import time
from pathlib import Path

time.sleep(float(sys.argv[1]))
reference = Path(__file__).parents[2] / "fixtures" / "servers" / "reference.py"
sys.argv = [str(reference)]
runpy.run_path(str(reference), run_name="__main__")
