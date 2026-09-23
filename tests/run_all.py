r"""Run every test script in this folder; exits 1 if any of them fails.

    .venv\Scripts\python.exe tests\run_all.py

They all run offline: no microphone, no speech recognition, no Ollama, and
no real keyboard or mouse input — key senders are stubbed and clicks are
delivered as window messages to the test's own window. (The pill tests
flash a small test window at the bottom of the screen for a second.)
"""
import os
import subprocess
import sys
from pathlib import Path

here = Path(__file__).resolve().parent
env = dict(os.environ, PYTHONIOENCODING="utf-8")
failed = []
for test in sorted(here.glob("test_*.py")):
    r = subprocess.run([sys.executable, str(test)], capture_output=True,
                       encoding="utf-8", errors="replace", env=env)
    ok = r.returncode == 0
    print(f"{'PASS' if ok else 'FAIL'}  {test.name}")
    if not ok:
        failed.append(test.name)
        print((r.stdout + r.stderr)[-3000:])
print(f"\n{len(failed)} failed: {', '.join(failed)}" if failed
      else "\nall tests passed")
sys.exit(1 if failed else 0)
