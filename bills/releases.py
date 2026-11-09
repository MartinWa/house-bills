"""Public release metadata only; checks run separately from page rendering."""

import json
import re
from http.client import HTTPException
from threading import Lock
from urllib.request import Request, urlopen

from django.conf import settings
from django.core.cache import cache

REPOSITORY_URL = "https://github.com/MartinWa/house-bills"
RELEASES_URL = f"{REPOSITORY_URL}/releases"
LATEST_RELEASE_API = "https://api.github.com/repos/MartinWa/house-bills/releases/latest"
CACHE_KEY = "house-bills:latest-stable-release"
CHECK_INTERVAL = 6 * 60 * 60
FAILURE_INTERVAL = 60 * 60
MAX_RESPONSE_BYTES = 64 * 1024
_check_lock = Lock()


def version_number(value):
    """Compare stable numeric tags, including the repository's legacy v prefix."""
    if not isinstance(value, str) or len(value) > 40:
        return None
    match = re.fullmatch(r"v?(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)", value)
    return tuple(map(int, match.groups())) if match else None


def latest_version():
    saved = cache.get(CACHE_KEY)
    if saved is not None:
        return saved["version"]
    # Concurrent checks must not both wait on GitHub in the two-thread server.
    if not _check_lock.acquire(blocking=False):
        return None
    try:
        saved = cache.get(CACHE_KEY)
        if saved is not None:
            return saved["version"]
        version = None
        request = Request(
            LATEST_RELEASE_API,
            headers={
                "Accept": "application/vnd.github+json",
                "User-Agent": "house-bills-update-check",
            },
        )
        try:
            with urlopen(request, timeout=3) as response:
                payload = response.read(MAX_RESPONSE_BYTES + 1)
            if len(payload) <= MAX_RESPONSE_BYTES:
                release = json.loads(payload)
                if (
                    isinstance(release, dict)
                    and release.get("draft") is False
                    and release.get("prerelease") is False
                ):
                    version = version_number(release.get("tag_name"))
        except OSError, ValueError, HTTPException:
            # Offline installations, rate limits and invalid responses keep the links usable.
            pass
        cache.set(
            CACHE_KEY,
            {"version": version},
            CHECK_INTERVAL if version is not None else FAILURE_INTERVAL,
        )
        return version
    finally:
        _check_lock.release()


def update_available():
    current = version_number(settings.APP_VERSION)
    if not settings.UPDATE_CHECK_ENABLED or current is None:
        return None
    latest = latest_version()
    return latest > current if latest is not None else None
