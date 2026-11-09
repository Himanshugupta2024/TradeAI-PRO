"""
Unit tests for AppState encapsulation in api.service.
"""

import pytest
import pandas as pd
from api.service import AppState, get_app_state
import api.service as svc


def test_get_app_state_singleton():
    state = get_app_state()
    assert isinstance(state, AppState)
    assert get_app_state() is state


def test_app_state_zero_copy_references():
    state = get_app_state()
    assert state.market_data is svc._market_data
    assert state.signal_cache is svc._signal_cache
    assert state.regime_cache is svc._regime_cache
    assert state.swing_cache is svc._swing_cache
    assert state.loading_status is svc._loading_status
    assert state.retrain_jobs is svc._retrain_jobs


def test_app_state_mutation_propagates_to_service():
    state = get_app_state()
    test_key = "__TEST_SYMBOL__"
    df = pd.DataFrame({"close": [100.0, 101.0]})
    state.market_data[test_key] = df
    try:
        assert test_key in svc._market_data
        assert svc._market_data[test_key] is df
    finally:
        state.market_data.pop(test_key, None)
        svc._market_data.pop(test_key, None)


def test_app_state_independent_instance():
    custom_state = AppState()
    assert custom_state.market_data == {}
    assert custom_state.signal_cache == {}
    assert custom_state.loading_status == {}
    assert custom_state is not get_app_state()
