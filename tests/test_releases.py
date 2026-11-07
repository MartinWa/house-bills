import json

import pytest

from tools import publish_release


@pytest.mark.parametrize("tag", ["v1.0.0", "1.0.0-rc1", "01.0.0", "1.2", "1.2.3.4"])
def test_release_rejects_nonstable_or_ambiguous_tags(tag):
    with pytest.raises(ValueError, match="X.Y.Z"):
        publish_release.validate_source(tag, event_name="push", tag_created="true")


def test_release_rejects_moved_tags_before_reading_or_publishing():
    with pytest.raises(ValueError, match="new"):
        publish_release.validate_source("1.0.0", event_name="push", tag_created="false")


@pytest.mark.parametrize(
    "failure", [None, "tag", "no_run", "wrong_commit", "wrong_event", "failed", "missing_browser"]
)
def test_release_requires_the_tagged_commit_and_all_successful_ci_jobs(monkeypatch, failure):
    commit = "a" * 40
    commands = []

    def read_command(*arguments):
        commands.append(arguments)
        if arguments[:2] == ("git", "rev-parse"):
            return "b" * 40 if failure == "tag" and arguments[2] != "HEAD" else commit
        if arguments[:2] == ("git", "merge-base"):
            return ""
        if arguments[:3] == ("gh", "run", "list"):
            return json.dumps(
                []
                if failure == "no_run"
                else [
                    {
                        "databaseId": 123,
                        "conclusion": "failure" if failure == "failed" else "success",
                        "headSha": "b" * 40 if failure == "wrong_commit" else commit,
                        "headBranch": "main",
                        "event": "workflow_dispatch" if failure == "wrong_event" else "push",
                    }
                ]
            )
        if arguments[:3] == ("gh", "run", "view"):
            return json.dumps(
                {
                    "jobs": [
                        {"name": name, "conclusion": "success"}
                        for name in publish_release.EXPECTED_CI_JOBS
                        if failure != "missing_browser" or name != "Browser (Chromium)"
                    ]
                }
            )
        raise AssertionError(arguments)

    monkeypatch.setattr(publish_release, "read_command", read_command)
    if failure:
        with pytest.raises(ValueError):
            publish_release.validate_source("1.0.0", event_name="push", tag_created="true")
    else:
        assert (
            publish_release.validate_source("1.0.0", event_name="push", tag_created="true")
            == commit
        )
    assert all(arguments[:3] != ("gh", "release", "create") for arguments in commands)


def test_release_plan_preserves_drafts_existing_releases_and_latest_order():
    releases = [{"tag_name": "2.0.0", "draft": False, "prerelease": False}]
    arguments, latest = publish_release.release_plan("1.1.0", releases)
    assert arguments[:2] == ["create", "1.1.0"] and not latest
    assert "--latest=false" in arguments
    assert publish_release.release_plan("2.0.0", releases) == ([], True)
    releases.append({"tag_name": "2.1.0", "draft": True, "prerelease": False})
    arguments, latest = publish_release.release_plan("2.1.0", releases)
    assert arguments[:2] == ["edit", "2.1.0"] and latest
    assert "--draft=false" in arguments and "--verify-tag" in arguments
    releases.append({"tag_name": "3.0.0", "draft": False, "prerelease": True})
    with pytest.raises(ValueError, match="prerelease"):
        publish_release.release_plan("3.0.0", releases)
