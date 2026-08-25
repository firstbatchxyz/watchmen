"""Safety defaults for unattended daemon scheduling."""

from datetime import datetime

from watchmen import daemon


def test_full_curator_schedule_is_opt_in_by_default():
    assert daemon.DEFAULT_FULL_CURATOR_HOURS == ""


def test_empty_schedule_never_runs_full_curator():
    assert not daemon._should_run_full_curator(
        datetime(2026, 8, 25, 14, 0),
        last_run=None,
        scheduled_hours=[],
        min_age_seconds=0,
    )


def test_explicit_schedule_remains_available():
    assert daemon._should_run_full_curator(
        datetime(2026, 8, 25, 14, 0),
        last_run=None,
        scheduled_hours=[14],
        min_age_seconds=0,
    )
