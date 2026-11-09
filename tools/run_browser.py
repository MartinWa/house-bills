"""Run the synthetic Chromium scenario against a fresh, temporary Linux instance."""

import os
import subprocess
import sys
import tempfile
from pathlib import Path

from .check_http import wait_for_server


def main():
    if os.geteuid() == 0:
        raise RuntimeError("Run browser checks as the non-root browser image user.")
    state = Path(os.environ.get("HOUSE_BILLS_STATE_DIR", "/state"))
    artifacts = state / "browser-artifacts"
    artifacts.mkdir(parents=True, exist_ok=True)
    run_artifacts = Path(tempfile.mkdtemp(prefix="run-", dir=artifacts))
    environment = {
        **os.environ,
        "DJANGO_SETTINGS_MODULE": "house_bills.settings.development",
        # Synthetic release identity; the browser scenario intercepts status requests.
        "HOUSE_BILLS_VERSION": "0.0.1",
        "HOUSE_BILLS_UPDATE_CHECK": "true",
        "HOUSE_BILLS_BROWSER_CHECK": "true",
        "HOUSE_BILLS_BROWSER_ARTIFACT_DIR": str(run_artifacts),
    }
    with tempfile.TemporaryDirectory(prefix="browser-instance-", dir=state / "tmp") as scratch:
        environment["HOUSE_BILLS_DATA_DIR"] = scratch
        for command in (
            [sys.executable, "manage.py", "migrate", "--noinput"],
            [sys.executable, "-m", "tests.browser.setup"],
        ):
            subprocess.run(command, env=environment, check=True)
        with (run_artifacts / "server.log").open("wb") as log:
            process = subprocess.Popen(
                [sys.executable, "manage.py", "runserver", "127.0.0.1:8000", "--noreload"],
                env=environment,
                stdout=log,
                stderr=log,
            )
            try:
                wait_for_server(process=process)
                subprocess.run(["node", "tests/browser/run.cjs"], env=environment, check=True)
            finally:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)
                print(f"Synthetic browser artifacts: {run_artifacts}", flush=True)


if __name__ == "__main__":
    main()
