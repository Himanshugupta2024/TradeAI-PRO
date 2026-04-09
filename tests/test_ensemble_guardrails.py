"""Tests for ensemble dynamic weight guardrails."""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from agents.ai_engine.ensemble import _guardrailed_normalize_weights


def test_weight_guardrails_apply_min_max_bounds():
    weights = _guardrailed_normalize_weights(
        {
            "statistical": 0.99,
            "xgboost": 0.01,
            "lightgbm": 0.0,
            "lstm": 0.0,
        }
    )

    assert abs(sum(weights.values()) - 1.0) < 1e-9
    assert 0.0 <= weights["statistical"] <= 1.0
    assert 0.0 <= weights["xgboost"] <= 1.0
    assert weights["statistical"] <= 0.90
    assert weights["xgboost"] >= 0.10


def test_weight_guardrails_limit_step_change_from_previous():
    previous = {
        "statistical": 0.40,
        "xgboost": 0.60,
        "lightgbm": 0.0,
        "lstm": 0.0,
    }
    updated = _guardrailed_normalize_weights(
        {
            "statistical": 0.95,
            "xgboost": 0.05,
            "lightgbm": 0.0,
            "lstm": 0.0,
        },
        previous_weights=previous,
    )

    assert abs(sum(updated.values()) - 1.0) < 1e-9
    assert updated["statistical"] <= previous["statistical"] + 0.20 + 1e-9
    assert updated["xgboost"] >= previous["xgboost"] - 0.20 - 1e-9
