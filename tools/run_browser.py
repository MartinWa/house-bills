"""Run the synthetic Chromium scenario against a fresh, temporary Linux instance."""

import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from .check_http import wait_for_server

BROKEN_PARSER = "raise RuntimeError('synthetic browser parser')\n"
LONG_REJECTED = "rejected_" + "x" * 60 + ".py"


def _configured_parser_directory():
    """The reviewed examples mounted by the browser service or CI.

    The image excludes `docs/`, so the source has to be the configured directory:
    `/app/docs/examples/parsers` under the Compose bind mount and `/parsers` in CI.
    """
    configured = os.environ.get("HOUSE_BILLS_PARSER_PLUGINS", "")
    directory = Path(configured) if configured else None
    if directory is None or not directory.is_dir():
        raise RuntimeError(
            "Set HOUSE_BILLS_PARSER_PLUGINS to a directory of reviewed example parsers "
            "before running the browser check; the image ships none."
        )
    return directory


def main():
    if os.geteuid() == 0:
        raise RuntimeError("Run browser checks as the non-root browser image user.")
    state = Path(os.environ.get("HOUSE_BILLS_STATE_DIR", "/state"))
    artifacts = state / "browser-artifacts"
    artifacts.mkdir(parents=True, exist_ok=True)
    run_artifacts = Path(tempfile.mkdtemp(prefix="run-", dir=artifacts))
    # The mount is read-only, so the scenario installs its own writable parser
    # directory: the reviewed examples plus rejected files with short and long names.
    parsers = run_artifacts / "parsers"
    shutil.copytree(_configured_parser_directory(), parsers)
    (parsers / "broken.py").write_text(BROKEN_PARSER, encoding="utf-8")
    (parsers / LONG_REJECTED).write_text(BROKEN_PARSER, encoding="utf-8")
    environment = {
        **os.environ,
        "DJANGO_SETTINGS_MODULE": "house_bills.settings.development",
        "HOUSE_BILLS_PARSER_PLUGINS": str(parsers),
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
