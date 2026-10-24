#!/usr/bin/env python3
"""Run fixture tests with an isolated message/policy directory, never the operator's store."""
import os
from pathlib import Path
import subprocess
import sys
import tempfile


root = Path(__file__).resolve().parent.parent
with tempfile.TemporaryDirectory(prefix="agentpeers-tests-") as state:
    result = subprocess.run([sys.executable, "-m", "unittest", "discover", "-s", "tests", "-v"],
                            cwd=root, env={**os.environ, "AGENTPEERS_HOME": state})
raise SystemExit(result.returncode)
