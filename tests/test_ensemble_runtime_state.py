"""Tests for ensemble runtime state accessor."""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from agents.ai_engine.ensemble import get_dynamic_weight_state


def test_dynamic_weight_state_schema():
    payload = get_dynamic_weight_state()

    assert isinstance(payload, dict)
    assert "effective_weights" in payload
    assert "multipliers" in payload
    assert "accuracy" in payload
    assert "guardrails" in payload

    guardrails = payload["guardrails"]
    assert guardrails["min_active_weight"] <= guardrails["max_active_weight"]
    assert guardrails["max_weight_step"] > 0
