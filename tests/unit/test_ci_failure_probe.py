import os


def test_ci_rejects_failing_fixture() -> None:
    assert False, "Temporary CI acceptance probe; remove before peer review."
