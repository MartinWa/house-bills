"""Create a stable GitHub Release after its image has passed publishing checks."""

import argparse
import json
import os
import re
import subprocess
from pathlib import Path

STABLE_VERSION = re.compile(r"(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)")
EXPECTED_CI_JOBS = {"Checks (Linux)", "Browser (Chromium)", "Container (Linux)"}


def version_number(tag, *, legacy=False):
    # Earlier releases used a v prefix; accept it only when comparing history.
    match = STABLE_VERSION.fullmatch(tag.removeprefix("v") if legacy else tag)
    return tuple(map(int, match.groups())) if match else None


def read_command(*arguments):
    return subprocess.run(arguments, check=True, capture_output=True, text=True).stdout.strip()


def validate_source(tag, *, event_name, tag_created):
    """Require a new stable tag on a tested main commit."""
    if event_name == "push" and tag_created != "true":
        raise ValueError("Release tags must be new; do not move or overwrite an existing tag.")
    if version_number(tag) is None:
        raise ValueError("Release tags must use X.Y.Z without a v prefix or prerelease suffix.")
    commit = read_command("git", "rev-parse", "HEAD")
    tag_commit = read_command("git", "rev-parse", f"refs/tags/{tag}^{{commit}}")
    if commit != tag_commit:
        raise ValueError("The release tag no longer points at the triggering commit.")
    read_command("git", "merge-base", "--is-ancestor", "HEAD", "origin/main")
    runs = json.loads(
        read_command(
            "gh",
            "run",
            "list",
            "--workflow",
            "ci.yml",
            "--commit",
            commit,
            "--branch",
            "main",
            "--event",
            "push",
            "--limit",
            "1",
            "--json",
            "databaseId,conclusion,headSha,headBranch,event",
        )
    )
    if (
        not runs
        or runs[0]["conclusion"] != "success"
        or runs[0]["headSha"] != commit
        or runs[0]["headBranch"] != "main"
        or runs[0]["event"] != "push"
    ):
        raise ValueError("The release commit must have a successful CI push run on main.")
    jobs = json.loads(
        read_command("gh", "run", "view", str(runs[0]["databaseId"]), "--json", "jobs")
    )["jobs"]
    completed = {job["name"] for job in jobs if job["conclusion"] == "success"}
    if not EXPECTED_CI_JOBS <= completed:
        raise ValueError(
            "The release commit lacks successful application, browser or container checks."
        )
    return commit


def release_plan(tag, releases):
    """Preserve existing releases and never promote a lower stable version."""
    version = version_number(tag)
    if version is None:
        raise ValueError("Release tags must use X.Y.Z without a v prefix or prerelease suffix.")
    existing = next((release for release in releases if release["tag_name"] == tag), None)
    if existing and existing["prerelease"]:
        raise ValueError("This tag already has a prerelease; do not publish it as a stable image.")
    published_versions = [
        number
        for release in releases
        if not release["draft"] and not release["prerelease"]
        if (number := version_number(release["tag_name"], legacy=True)) is not None
    ]
    latest = not published_versions or version >= max(published_versions)
    if existing and not existing["draft"]:
        return [], latest
    latest_option = f"--latest={str(latest).lower()}"
    if existing:
        return ["edit", tag, "--draft=false", "--verify-tag", latest_option], latest
    return [
        "create",
        tag,
        "--verify-tag",
        "--title",
        tag,
        "--generate-notes",
        latest_option,
    ], latest


def publish_release(repository, tag, *, check_only=False):
    if version_number(tag) is None:
        raise ValueError("Release tags must use X.Y.Z without a v prefix or prerelease suffix.")
    result = subprocess.run(
        ["gh", "api", "--paginate", "--slurp", f"repos/{repository}/releases"],
        check=True,
        capture_output=True,
        text=True,
    )
    releases = [release for page in json.loads(result.stdout) for release in page]
    arguments, latest = release_plan(tag, releases)
    if arguments and not check_only:
        subprocess.run(["gh", "release", *arguments, "--repo", repository], check=True)
    return latest


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="Validate without changing a release.")
    arguments = parser.parse_args()
    tag = os.environ["RELEASE_TAG"]
    repository = os.environ["GITHUB_REPOSITORY"]
    commit = validate_source(
        tag,
        event_name=os.environ.get("EVENT_NAME", "release"),
        tag_created=os.environ.get("TAG_CREATED", ""),
    )
    latest = publish_release(repository, tag, check_only=arguments.check)
    with Path(os.environ["GITHUB_OUTPUT"]).open("a", encoding="utf-8") as output:
        if arguments.check:
            output.write(f"image=ghcr.io/{repository.lower()}\nversion={tag}\ncommit={commit}\n")
        else:
            output.write(f"latest={str(latest).lower()}\n")


if __name__ == "__main__":
    main()
