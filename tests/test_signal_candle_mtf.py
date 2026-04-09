"""Tests for candle-quality and MTF vote-stack confidence adjustments."""

import os
import sys

import pandas as pd

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from agents.signal_engine.signal_generator import (
    _candle_quality_confidence_penalty,
    _mtf_agreement_votes_and_penalty,
)
from config import settings


def test_candle_penalty_low_body():
    df = pd.DataFrame(
        {
            "_raw_direction": ["LONG"],
            "open": [100.0],
            "high": [101.0],
            "low": [99.5],
            "close": [100.1],
            "candle_body_ratio": [0.1],
        }
    )
    pen = _candle_quality_confidence_penalty(df)
    assert float(pen.iloc[0]) <= -float(settings.CANDLE_QUALITY_BODY_PENALTY) + 0.01


def test_candle_penalty_red_bar_on_long():
    df = pd.DataFrame(
        {
            "_raw_direction": ["LONG"],
            "open": [100.0],
            "high": [101.0],
            "low": [98.0],
            "close": [99.0],
            "candle_body_ratio": [0.5],
        }
    )
    pen = _candle_quality_confidence_penalty(df)
    assert float(pen.iloc[0]) <= -float(settings.CANDLE_QUALITY_DIRECTION_FIGHT_PENALTY) + 0.01


def test_mtf_votes_proxy_shortfall():
    idx = pd.RangeIndex(1)
    htf_bull = pd.Series([True], index=idx)
    htf_bear = pd.Series([False], index=idx)
    ema_buy = pd.Series([False], index=idx)
    ema_sell = pd.Series([True], index=idx)
    macd_bull = pd.Series([False], index=idx)
    macd_bear = pd.Series([False], index=idx)
    vwap_bull = pd.Series([False], index=idx)
    vwap_bear = pd.Series([False], index=idx)
    result = pd.DataFrame({"_raw_direction": ["LONG"]}, index=idx)
    votes, delta = _mtf_agreement_votes_and_penalty(
        result,
        htf_bull,
        htf_bear,
        ema_buy,
        ema_sell,
        macd_bull,
        macd_bear,
        vwap_bull,
        vwap_bear,
        None,
    )
    assert int(votes.iloc[0]) == 1
    assert float(delta.iloc[0]) < 0


def test_mtf_score_maps_votes_and_weak_penalty():
    idx = pd.RangeIndex(1)
    z = pd.Series([False], index=idx)
    result = pd.DataFrame({"_raw_direction": ["LONG"]}, index=idx)
    votes, delta = _mtf_agreement_votes_and_penalty(
        result,
        z,
        z,
        z,
        z,
        z,
        z,
        z,
        z,
        20.0,
    )
    assert int(votes.iloc[0]) >= 0
    assert float(delta.iloc[0]) < 0
