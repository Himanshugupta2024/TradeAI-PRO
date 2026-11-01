"""
Tests for agents/macro/macro_scorer.py

Tests the macro scoring logic for risk-on/risk-off conditions.
"""

import pytest
from datetime import datetime, timedelta
from agents.macro.macro_scorer import MacroScorer
from agents.macro.macro_fetcher import MacroIndicators


@pytest.fixture
def scorer():
    """Create MacroScorer instance"""
    return MacroScorer()


@pytest.fixture
def risk_on_data():
    """Mock macro data for risk-on conditions"""
    return MacroIndicators(
        gdp_growth_yoy=3.5,  # Strong growth
        cpi_yoy=2.0,  # Low inflation
        unemployment_rate=3.5,  # Low unemployment
        fed_funds_rate=2.0,  # Low rates
        treasury_10y=2.5,  # Low yields
        timestamp=datetime.now()
    )


@pytest.fixture
def risk_off_data():
    """Mock macro data for risk-off conditions"""
    return MacroIndicators(
        gdp_growth_yoy=-1.5,  # Negative growth
        cpi_yoy=6.0,  # High inflation
        unemployment_rate=8.0,  # High unemployment
        fed_funds_rate=5.5,  # High rates
        treasury_10y=5.0,  # High yields
        timestamp=datetime.now()
    )


@pytest.fixture
def neutral_data():
    """Mock macro data for neutral conditions"""
    return MacroIndicators(
        gdp_growth_yoy=2.0,  # Moderate growth
        cpi_yoy=3.0,  # Moderate inflation
        unemployment_rate=5.0,  # Moderate unemployment
        fed_funds_rate=3.5,  # Moderate rates
        treasury_10y=3.5,  # Moderate yields
        timestamp=datetime.now()
    )


@pytest.fixture
def partial_data():
    """Mock macro data with only 3 of 5 indicators (minimum required)"""
    return MacroIndicators(
        gdp_growth_yoy=3.0,
        cpi_yoy=2.5,
        unemployment_rate=4.0,
        fed_funds_rate=None,  # Missing
        treasury_10y=None,  # Missing
        timestamp=datetime.now()
    )


def test_risk_on_scoring(scorer, risk_on_data):
    """Test scoring logic for risk-on conditions returns positive score"""
    score = scorer.get_macro_score(risk_on_data)
    
    assert score > 50, "Risk-on conditions should produce positive score"
    assert score <= 100, "Score should not exceed 100"
    assert isinstance(score, (int, float)), "Score should be numeric"


def test_risk_off_scoring(scorer, risk_off_data):
    """Test scoring logic for risk-off conditions returns negative score"""
    score = scorer.get_macro_score(risk_off_data)
    
    assert score < -50, "Risk-off conditions should produce negative score"
    assert score >= -100, "Score should not be below -100"
    assert isinstance(score, (int, float)), "Score should be numeric"


def test_neutral_scoring(scorer, neutral_data):
    """Test scoring logic for neutral conditions returns near-zero score"""
    score = scorer.get_macro_score(neutral_data)
    
    assert -50 <= score <= 50, "Neutral conditions should produce score near zero"
    assert isinstance(score, (int, float)), "Score should be numeric"


def test_partial_indicators(scorer, partial_data):
    """Test scoring with minimum 3 of 5 indicators available"""
    score = scorer.get_macro_score(partial_data)
    
    # Should still produce a valid score with 3 indicators
    assert -100 <= score <= 100, "Score should be in valid range with partial data"
    assert isinstance(score, (int, float)), "Score should be numeric"


def test_insufficient_indicators(scorer):
    """Test scoring fails gracefully with less than 3 indicators"""
    insufficient_data = MacroIndicators(
        gdp_growth_yoy=3.0,
        cpi_yoy=2.5,
        unemployment_rate=None,
        fed_funds_rate=None,
        treasury_10y=None,
        timestamp=datetime.now()
    )
    
    score = scorer.get_macro_score(insufficient_data)
    
    # Should return 0 or handle gracefully
    assert score == 0 or -100 <= score <= 100, "Should handle insufficient data gracefully"


def test_none_data(scorer):
    """Test scoring with None data returns 0"""
    score = scorer.get_macro_score(None)
    assert score == 0, "None data should return 0 score"


def test_score_range_bounds(scorer, risk_on_data, risk_off_data):
    """Test that scores are always within -100 to +100 range"""
    risk_on_score = scorer.get_macro_score(risk_on_data)
    risk_off_score = scorer.get_macro_score(risk_off_data)
    
    assert -100 <= risk_on_score <= 100, "Risk-on score out of bounds"
    assert -100 <= risk_off_score <= 100, "Risk-off score out of bounds"


def test_extreme_values(scorer):
    """Test scoring with extreme macro values"""
    extreme_bullish = MacroIndicators(
        gdp_growth_yoy=10.0,  # Extremely high growth
        cpi_yoy=0.5,  # Very low inflation
        unemployment_rate=2.0,  # Very low unemployment
        fed_funds_rate=0.5,  # Very low rates
        treasury_10y=1.0,  # Very low yields
        timestamp=datetime.now()
    )
    
    extreme_bearish = MacroIndicators(
        gdp_growth_yoy=-5.0,  # Severe recession
        cpi_yoy=10.0,  # High inflation
        unemployment_rate=12.0,  # Very high unemployment
        fed_funds_rate=8.0,  # Very high rates
        treasury_10y=7.0,  # Very high yields
        timestamp=datetime.now()
    )
    
    bullish_score = scorer.get_macro_score(extreme_bullish)
    bearish_score = scorer.get_macro_score(extreme_bearish)
    
    assert bullish_score > 70, "Extreme bullish conditions should score high"
    assert bearish_score < -70, "Extreme bearish conditions should score low"


def test_stale_data_handling(scorer):
    """Test scoring with stale timestamp data"""
    stale_data = MacroIndicators(
        gdp_growth_yoy=3.0,
        cpi_yoy=2.5,
        unemployment_rate=4.0,
        fed_funds_rate=2.5,
        treasury_10y=3.0,
        timestamp=datetime.now() - timedelta(days=60)  # 60 days old
    )
    
    score = scorer.get_macro_score(stale_data)
    
    # Should still produce a score but may be flagged as stale
    assert -100 <= score <= 100, "Should handle stale data"


def test_consistency(scorer, risk_on_data):
    """Test that same input produces same output (deterministic)"""
    score1 = scorer.get_macro_score(risk_on_data)
    score2 = scorer.get_macro_score(risk_on_data)
    
    assert score1 == score2, "Scoring should be deterministic"


def test_gdp_weight(scorer):
    """Test that GDP has significant impact on score"""
    high_gdp = MacroIndicators(
        gdp_growth_yoy=5.0,
        cpi_yoy=3.0,
        unemployment_rate=5.0,
        fed_funds_rate=3.0,
        treasury_10y=3.5,
        timestamp=datetime.now()
    )
    
    low_gdp = MacroIndicators(
        gdp_growth_yoy=-2.0,
        cpi_yoy=3.0,
        unemployment_rate=5.0,
        fed_funds_rate=3.0,
        treasury_10y=3.5,
        timestamp=datetime.now()
    )
    
    high_score = scorer.get_macro_score(high_gdp)
    low_score = scorer.get_macro_score(low_gdp)
    
    assert high_score > low_score, "Higher GDP should produce higher score"
    assert abs(high_score - low_score) > 20, "GDP should have significant impact"


def test_inflation_impact(scorer):
    """Test that inflation affects score appropriately"""
    low_inflation = MacroIndicators(
        gdp_growth_yoy=2.5,
        cpi_yoy=1.5,  # Low inflation (good)
        unemployment_rate=5.0,
        fed_funds_rate=3.0,
        treasury_10y=3.5,
        timestamp=datetime.now()
    )
    
    high_inflation = MacroIndicators(
        gdp_growth_yoy=2.5,
        cpi_yoy=7.0,  # High inflation (bad)
        unemployment_rate=5.0,
        fed_funds_rate=3.0,
        treasury_10y=3.5,
        timestamp=datetime.now()
    )
    
    low_inf_score = scorer.get_macro_score(low_inflation)
    high_inf_score = scorer.get_macro_score(high_inflation)
    
    assert low_inf_score > high_inf_score, "Lower inflation should produce higher score"
