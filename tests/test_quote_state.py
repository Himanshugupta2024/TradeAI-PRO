"""Quote freshness is judged from bar age, not from the worker snapshot age.

The cockpit banner used to read the snapshot timestamp and announce
"Feed status: STALE — live quotes may be unreliable" while every mark was a few
minutes old and only the autonomous worker had stopped. These tests pin the two
dimensions apart.
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from api.data_honesty import classify_quote_state  # noqa: E402


def test_fresh_bar_during_session_is_live():
    assert classify_quote_state(120, True) == "LIVE"


def test_bar_inside_the_delay_budget_is_delayed():
    assert classify_quote_state(900, True) == "DELAYED"


def test_old_bar_during_session_is_stale():
    assert classify_quote_state(3600, True) == "STALE"


def test_closed_market_is_eod_regardless_of_age():
    assert classify_quote_state(60 * 60 * 20, False) == "EOD"


def test_missing_bar_is_unknown():
    assert classify_quote_state(None, True) == "UNKNOWN"
    assert classify_quote_state("garbage", True) == "UNKNOWN"


def test_boundaries_match_the_published_budgets():
    assert classify_quote_state(300, True) == "LIVE"
    assert classify_quote_state(301, True) == "DELAYED"
    assert classify_quote_state(1800, True) == "DELAYED"
    assert classify_quote_state(1801, True) == "STALE"
