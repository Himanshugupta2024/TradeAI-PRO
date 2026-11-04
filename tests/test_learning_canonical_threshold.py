import re
from pathlib import Path
from config.settings import PAPER_TRADING_UNLOCK_MIN_TRADES
from agents.analytics.feedback_loop import get_learning_stats


def test_canonical_learning_threshold():
    """Verify backend configuration and learning stats share canonical threshold of 30."""
    assert PAPER_TRADING_UNLOCK_MIN_TRADES == 30

    stats = get_learning_stats()
    health = stats.get("health", {})
    assert health.get("canonical_sample_requirement") == 30
    assert health.get("analyse_every_n_trades") == 30


def test_dashboard_learning_html_threshold():
    """Verify frontend HTML reflects the canonical 30 trades requirement and has no 50 trades mockup text."""
    html_path = Path("dashboard/index.html")
    assert html_path.exists()
    content = html_path.read_text(encoding="utf-8")

    assert "Updating every 30 trades" in content
    assert "30 trades remaining" in content
    assert "Updating every 50 trades" not in content
    assert "50 trades remaining" not in content
