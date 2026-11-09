"""The EOD self-learning review must run even when the scanner cycle never fires.

On 2026-11-09 the feed reported ``last_eod_review: null`` for the whole session:
the review was only reachable from the GitHub-scheduled cycle, so when those
runs were dropped the weights never adapted. These tests pin the window/day
rules that make the API-side supervisor safe to run every few minutes.
"""

import os
import sys
from datetime import datetime

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from api.learning_scheduler import _IST, should_run_review  # noqa: E402


def _ist(y, m, d, hh, mm):
    return datetime(y, m, d, hh, mm, tzinfo=_IST)


def test_runs_inside_the_weekday_window():
    now = _ist(2026, 11, 9, 15, 50)  # Monday
    assert should_run_review(now, None) == (True, "in_window")


def test_skips_before_the_window():
    now = _ist(2026, 11, 9, 15, 30)
    assert should_run_review(now, None) == (False, "before_window")


def test_skips_after_the_window():
    now = _ist(2026, 11, 9, 17, 0)
    assert should_run_review(now, None) == (False, "after_window")


def test_skips_weekends():
    now = _ist(2026, 11, 7, 15, 50)  # Saturday
    assert should_run_review(now, None) == (False, "weekend")


def test_does_not_run_twice_in_a_day():
    now = _ist(2026, 11, 9, 16, 10)
    assert should_run_review(now, "2026-11-09") == (False, "already_ran_today")


def test_runs_again_the_next_day():
    now = _ist(2026, 11, 10, 15, 50)
    assert should_run_review(now, "2026-11-09") == (True, "in_window")
