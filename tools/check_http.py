"""Small HTTP startup probes shared by local and Linux CI checks."""

import os
import time
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, Request, build_opener


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def get(path, *, host="localhost", secure=False):
    headers = {"Host": host}
    if secure:
        headers["X-Forwarded-Proto"] = "https"
    request = Request(f"http://127.0.0.1:8000{path}", headers=headers)
    try:
        return build_opener(NoRedirect).open(request, timeout=2)
    except HTTPError as response:
        return response


def wait_for_server(*, host="localhost", secure=False, process=None):
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        if process is not None and process.poll() is not None:
            raise RuntimeError(f"Server exited during startup: {process.returncode}")
        try:
            with get("/", host=host, secure=secure) as response:
                expected = 302 if secure or host == "localhost" else 301
                assert response.status == expected, response.status
            return
        except URLError, TimeoutError:
            time.sleep(0.25)
    raise RuntimeError("Server did not become ready within 20 seconds.")


if __name__ == "__main__":
    assert os.geteuid() != 0, "Development must run as non-root."
    wait_for_server()
    with get("/") as response:
        assert response.headers["Location"].startswith("/login/")
    with get("/login/") as response:
        assert response.status == 200
        assert b"Sign in" in response.read()
    print("Development HTTP startup and anonymous login redirect passed.")
