# -*- coding: utf-8 -*-
"""Run the unittest suite and print only the summary lines.

The shell in this environment has lost coreutils (`grep`, `tail`, `seq`,
`dirname`), so filtering has to happen in Python.
"""
import os
import re
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import repo_config as rc  # noqa: E402

PY = sys.executable  # the interpreter running this script
ROOT = str(rc.project_root())

p = subprocess.run(
    [PY, "-m", "unittest", "discover", "-s", "tests", "-t", "."],
    cwd=ROOT, capture_output=True,
)
out = (p.stdout.decode("utf-8", "replace")
       + p.stderr.decode("utf-8", "replace"))

for line in out.splitlines():
    if re.search(r"^(Ran \d+ tests?|OK|FAILED|ERROR)", line) \
            or re.search(r"^(FAIL|ERROR):", line) \
            or "Traceback" in line:
        print(line)

print("-" * 60)
print("exit_code =", p.returncode)
print("VERDICT:", "PASS" if p.returncode == 0 else "FAIL")
