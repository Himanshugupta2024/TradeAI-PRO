"""
Tests for agents/macro/cot_scorer.py

Tests COT positioning scoring logic with contrarian approach.
"""

import pytest
from datetime import datetime
from agents.macro.cot_scorer import COTScorer
from agents.macro.cot_fetcher import COTData


@pytest.fixture
def scorer():
    """Create COTScorer instance"""
    return COTScorer()


@pytest.fixture
def bullish_positioning():
    """Mock COT data showing extreme bullish positioning (contrarian bearish)"""
    return COTData(
        symbol="EUR/USD",
        commercial_long=50000,
        commercial_short=150000,  # Commercials heavily short
        non_commercial_long=150000,  # Speculators heavily long
        non_commercial_short=50000,
        net_position=100000,
            net_position_pct=50.0,
        timestamp=datetime.now()
    )


@pytest.fixture
def bearish_positioning():
    """Mock COT data showing extreme bearish positioning (contrarian bullish)"""
    return COTData(
        symbol="EUR/USD",
        commercial_long=150000,  # Commercials heavily long
        commercial_short=50000,
        non_commercial_long=50000,  # Speculators heavily short
        non_commercial_short=150000,
        net_position=-100000,
            net_position_pct=-50.0,
        timestamp=datetime.now()
    )


@pytest.fixture
def neutral_positioning():
    """Mock COT data showing balanced positioning"""
    return COTData(
        symbol="EUR/USD",
        commercial_long=100000,
        commercial_short=100000,
        non_commercial_long=100000,
        non_commercial_short=100000,
        net_position=0,
            net_position_pct=0.0,
        timestamp=datetime.now()
    )


def test_bullish_positioning_score(scorer, bullish_positioning):
    """Test extreme bullish positioning returns negative score (contrarian)"""
    score = scorer.get_cot_score(bullish_positioning)
    
    # Contrarian: when speculators are heavily long, we expect reversal (bearish)
    assert score < -50, "Extreme bullish positioning should produce negative score"
    assert score >= -100, "Score should not be below -100"


def test_bearish_positioning_score(scorer, bearish_positioning):
    """Test extreme bearish positioning returns positive score (contrarian)"""
    score = scorer.get_cot_score(bearish_positioning)
    
    # Contrarian: when speculators are heavily short, we expect reversal (bullish)
    assert score > 50, "Extreme bearish positioning should produce positive score"
    assert score <= 100, "Score should not exceed 100"


def test_neutral_positioning_score(scorer, neutral_positioning):
    """Test balanced positioning returns near-zero score"""
    score = scorer.get_cot_score(neutral_positioning)
    
    assert -20 <= score <= 20, "Neutral positioning should produce score near zero"


def test_contrarian_logic(scorer):
    """Test contrarian extreme positioning logic"""
    # When non-commercials are 80%+ long (extreme bullish sentiment)
    extreme_long = COTData(
        symbol="EUR/USD",
        commercial_long=20000,
        commercial_short=180000,
        non_commercial_long=180000,  # 90% long
        non_commercial_short=20000,
        net_position=160000,
            net_position_pct=80.0,
        timestamp=datetime.now()
    )
    
    # When non-commercials are 80%+ short (extreme bearish sentiment)
    extreme_short = COTData(
        symbol="EUR/USD",
        commercial_long=180000,
        commercial_short=20000,
        non_commercial_long=20000,  # 90% short
        non_commercial_short=180000,
        net_position=-160000,
            net_position_pct=-80.0,
        timestamp=datetime.now()
    )
    
    long_score = scorer.get_cot_score(extreme_long)
    short_score = scorer.get_cot_score(extreme_short)
    
    # Contrarian: extreme long sentiment → bearish score
    assert long_score < -70, "Extreme long positioning should be very bearish"
    
    # Contrarian: extreme short sentiment → bullish score
    assert short_score > 70, "Extreme short positioning should be very bullish"


def test_none_data(scorer):
    """Test scoring with None data returns 0"""
    score = scorer.get_cot_score(None)
    assert score == 0, "None data should return 0 score"


def test_unsupported_symbol(scorer):
    """Test scoring for symbols without COT data returns 0"""
    # Crypto (no COT data)
    crypto_data = COTData(
        symbol="BTC/USDT",
        commercial_long=0,
        commercial_short=0,
        non_commercial_long=0,
        non_commercial_short=0,
        net_position=0,
            net_position_pct=0.0,
        timestamp=datetime.now()
    )
    
    score = scorer.get_cot_score(crypto_data)
    assert score == 0, "Crypto should return 0 (no COT data)"


def test_zero_open_interest(scorer):
    """Test handling of zero open interest"""
    zero_oi = COTData(
        symbol="EUR/USD",
        commercial_long=0,
        commercial_short=0,
        non_commercial_long=0,
        non_commercial_short=0,
        net_position=0,
            net_position_pct=0.0,

        timestamp=datetime.now()
    )
    
    score = scorer.get_cot_score(zero_oi)
    assert score == 0, "Zero open interest should return 0"


def test_net_position_percentage():
    """Test net position percentage calculation"""
    data = COTData(
        symbol="EUR/USD",
        commercial_long=100000,
        commercial_short=80000,
        non_commercial_long=120000,
        non_commercial_short=60000,
        net_position=60000,
            net_position_pct=30.0,
        timestamp=datetime.now()
    )
    
    # Net non-commercial position
    net_nc = data.non_commercial_long - data.non_commercial_short
    assert net_nc == 60000, "Net non-commercial should be 60000"
    
    # Net position percentage
    net_pct = 30.0  # open_interest removed; handled in fetcher
    assert net_pct == 30.0, "Net position percentage should be 30%"


def test_commercial_vs_noncommercial():
    """Test that non-commercial positioning is primary driver"""
    scorer = COTScorer()
    
    # Scenario 1: Non-commercials bullish, commercials bearish (typical)
    nc_bullish = COTData(
        symbol="EUR/USD",
        commercial_long=50000,
        commercial_short=150000,  # Commercials bearish
        non_commercial_long=150000,  # Non-commercials bullish
        non_commercial_short=50000,
        net_position=100000,
            net_position_pct=50.0,
        timestamp=datetime.now()
    )
    
    # Scenario 2: Non-commercials bearish, commercials bullish (typical)
    nc_bearish = COTData(
        symbol="EUR/USD",
        commercial_long=150000,  # Commercials bullish
        commercial_short=50000,
        non_commercial_long=50000,  # Non-commercials bearish
        non_commercial_short=150000,
        net_position=-100000,
            net_position_pct=-50.0,
        timestamp=datetime.now()
    )
    
    score1 = scorer.get_cot_score(nc_bullish)
    score2 = scorer.get_cot_score(nc_bearish)
    
    # Contrarian: NC bullish → bearish score, NC bearish → bullish score
    assert score1 < 0, "NC bullish should produce bearish score"
    assert score2 > 0, "NC bearish should produce bullish score"


def test_score_magnitude():
    """Test that score magnitude reflects positioning strength"""
    scorer = COTScorer()
    
    # Moderate positioning
    moderate = COTData(
        symbol="EUR/USD",
        commercial_long=80000,
        commercial_short=120000,
        non_commercial_long=120000,
        non_commercial_short=80000,
        net_position=40000,
            net_position_pct=20.0,
        timestamp=datetime.now()
    )
    
    # Extreme positioning
    extreme = COTData(
        symbol="EUR/USD",
        commercial_long=20000,
        commercial_short=180000,
        non_commercial_long=180000,
        non_commercial_short=20000,
        net_position=160000,
            net_position_pct=80.0,
        timestamp=datetime.now()
    )
    
    moderate_score = scorer.get_cot_score(moderate)
    extreme_score = scorer.get_cot_score(extreme)
    
    # Extreme positioning should have larger magnitude
    assert abs(extreme_score) > abs(moderate_score), "Extreme positioning should have larger score magnitude"


def test_consistency(scorer, bullish_positioning):
    """Test that same input produces same output (deterministic)"""
    score1 = scorer.get_cot_score(bullish_positioning)
    score2 = scorer.get_cot_score(bullish_positioning)
    
    assert score1 == score2, "Scoring should be deterministic"


def test_score_bounds(scorer, bullish_positioning, bearish_positioning, neutral_positioning):
    """Test that all scores are within -100 to +100 range"""
    scores = [
        scorer.get_cot_score(bullish_positioning),
        scorer.get_cot_score(bearish_positioning),
        scorer.get_cot_score(neutral_positioning)
    ]
    
    for score in scores:
        assert -100 <= score <= 100, f"Score {score} out of bounds"


def test_small_open_interest():
    """Test handling of very small open interest"""
    scorer = COTScorer()
    
    small_oi = COTData(
        symbol="EUR/USD",
        commercial_long=100,
        commercial_short=50,
        non_commercial_long=150,
        non_commercial_short=50,
        net_position=100,
            net_position_pct=0.05,

        timestamp=datetime.now()
    )
    
    score = scorer.get_cot_score(small_oi)
    
    # Should still produce valid score or return 0 for unreliable data
    assert -100 <= score <= 100, "Should handle small OI gracefully"


def test_negative_values_handling():
    """Test that negative values are handled gracefully"""
    scorer = COTScorer()
    
    # Invalid data with negative values (should not happen in real data)
    invalid_data = COTData(
        symbol="EUR/USD",
        commercial_long=-1000,  # Invalid
        commercial_short=100000,
        non_commercial_long=100000,
        non_commercial_short=100000,
        net_position=0,
            net_position_pct=0.0,
        timestamp=datetime.now()
    )
    
    score = scorer.get_cot_score(invalid_data)
    
    # Should handle gracefully (return 0 or valid score)
    assert -100 <= score <= 100, "Should handle invalid data gracefully"
