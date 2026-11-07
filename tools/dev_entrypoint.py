"""Seed or refresh a Linux virtualenv in the development volume from the image."""

import os
import shutil
import subprocess
import sys
from pathlib import Path

from filelock import FileLock


def main():
    if os.geteuid() == 0:
        raise RuntimeError("Run development commands as a non-root user.")
    state = Path("/state")
    environment = state / "venv"
    fingerprint = Path("/opt/venv-seed.sha256").read_text()
    with FileLock(state / ".venv.lock", timeout=120):
        stamp = environment / ".image-fingerprint"
        if not stamp.exists() or stamp.read_text() != fingerprint:
            if environment.exists():
                shutil.rmtree(environment)
            shutil.copytree("/opt/venv-seed", environment, symlinks=True)
            stamp.write_text(fingerprint)
    (state / "tmp").mkdir(exist_ok=True)
    subprocess.run(
        [str(environment / "bin/python"), "/usr/local/bin/house-bills-check-runtime.py"], check=True
    )
    os.execvp(sys.argv[1], sys.argv[1:])


if __name__ == "__main__":
    main()
