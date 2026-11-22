"""Small HTTP startup probes shared by local and Linux CI checks."""

import os
import time
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, Request, build_opener

# Startup and request budgets in seconds for a native image. The release workflow
# also probes an emulated arm64 image, where interpreter startup, migrations and
# request handling are much slower, so every budget is scaled by TIMEOUT_SCALE
# (1 unless the caller sets it).
READY_SECONDS = 20
POLL_SECONDS = 2
REQUEST_SECONDS = 10


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def budget(seconds):
    """Scale a probe budget for a slower, emulated image."""
    return seconds * float(os.environ.get("TIMEOUT_SCALE", "1"))


def get(path, *, host="localhost", secure=False, timeout=None):
    headers = {"Host": host}
    if secure:
        headers["X-Forwarded-Proto"] = "https"
    request = Request(f"http://127.0.0.1:8000{path}", headers=headers)
    if timeout is None:
        timeout = budget(REQUEST_SECONDS)
    try:
        return build_opener(NoRedirect).open(request, timeout=timeout)
    except HTTPError as response:
        return response


def wait_for_server(*, host="localhost", secure=False, process=None):
    timeout = budget(READY_SECONDS)
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if process is not None and process.poll() is not None:
            raise RuntimeError(f"Server exited during startup: {process.returncode}")
        try:
            with get("/", host=host, secure=secure, timeout=budget(POLL_SECONDS)) as response:
                expected = 302 if secure or host == "localhost" else 301
                assert response.status == expected, response.status
            return
        except URLError, TimeoutError:
            time.sleep(0.25)
    raise RuntimeError(f"Server did not become ready within {timeout:g} seconds.")


if __name__ == "__main__":
    assert os.geteuid() != 0, "Development must run as non-root."
    wait_for_server()
    with get("/") as response:
        assert response.headers["Location"].startswith("/login/")
    with get("/login/") as response:
        assert response.status == 200
        assert b"Sign in" in response.read()
    print("Development HTTP startup and anonymous login redirect passed.")
