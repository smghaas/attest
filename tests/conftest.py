"""One throwaway gate world for all interface tests (set up before gate.server is imported)."""
import os
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TMP = tempfile.mkdtemp()
os.environ.update(ATTEST_STATE_DIR=f"{TMP}/state", ATTEST_LOG_DIR=f"{TMP}/logs", ATTEST_DEMO="1")
os.environ.pop("ATTEST_SANDBOX", None)
subprocess.run([sys.executable, "setup_demo.py"], cwd=ROOT, check=True, stdout=subprocess.DEVNULL)
sys.path.insert(0, str(ROOT))
