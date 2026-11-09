"""The manual cycle endpoint must never OOM-kill the API process.

The old fallback executed the full cycle inside the web worker: it imports the
ML stack, blows past the 512 MB Render limit, and takes the whole API down with
it (every panel then shows a 502). In lightweight mode the route must refuse the
inline path and point the operator at the GitHub dispatcher.
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from api.cycle_scheduler import LOCAL_CYCLE_REFUSED_MESSAGE, local_cycle_allowed  # noqa: E402


def test_lightweight_mode_refuses_inline_cycle():
    allowed, message = local_cycle_allowed({"LIGHTWEIGHT_API_MODE": "true"})
    assert allowed is False
    assert message == LOCAL_CYCLE_REFUSED_MESSAGE
    assert "GITHUB_TOKEN" in message


def test_background_refresh_disabled_also_counts_as_lightweight():
    allowed, _ = local_cycle_allowed({"LIGHTWEIGHT_API_MODE": "false", "BACKGROUND_REFRESH_ENABLED": "false"})
    assert allowed is False


def test_operator_can_explicitly_force_inline_cycle():
    allowed, message = local_cycle_allowed(
        {"LIGHTWEIGHT_API_MODE": "true", "ALLOW_LOCAL_AUTONOMOUS_CYCLE": "true"}
    )
    assert allowed is True
    assert message == ""


def test_full_mode_allows_inline_cycle():
    allowed, _ = local_cycle_allowed({"LIGHTWEIGHT_API_MODE": "false", "BACKGROUND_REFRESH_ENABLED": "true"})
    assert allowed is True
