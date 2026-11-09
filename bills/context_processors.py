from django.conf import settings

from .releases import RELEASES_URL, REPOSITORY_URL, version_number


def app_release(request):
    version = settings.APP_VERSION
    release_number = version_number(version.split("-", 1)[0])
    return {
        "app_version": version,
        "app_version_label": ".".join(map(str, release_number)) if release_number else "",
        "app_is_release": version_number(version) is not None,
        "app_repository_url": REPOSITORY_URL,
        "app_releases_url": RELEASES_URL,
        "app_check_updates": settings.UPDATE_CHECK_ENABLED and request.user.is_authenticated,
    }
