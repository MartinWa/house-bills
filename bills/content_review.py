"""Content decisions exclude worker waits and storage-only maintenance."""

CONTENT_REVIEW_STATES = ("review", "failed")


def needs_content_review(state):
    return state in CONTENT_REVIEW_STATES
