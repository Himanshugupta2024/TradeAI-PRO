"""
SwingSpectrum Multi-Factor Ranking Engine
==========================================
Nightly scan ke baad results ko re-score aur re-grade karta hai.

Existing scanner sirf strength (0-100) se grade assign karta hai.
Yeh engine 5 independent factors ko blend karta hai:

    Factor              Weight   Source
    ─────────────────── ──────   ──────────────────────────────────────
    1. R:R Ratio        30 %     setup.risk_reward
    2. Strength         25 %     setup.strength (scanner score)
    3. Volume Spike     20 %     setup.indicators.vol_ratio
    4. Confluence       15 %     setup.confluence_count
    5. Sector Momentum  10 %     setup.sector_tag

Final composite score 0-100 → Grade A / B / C (thresholds below).

Usage (standalone):
    from scripts.ranking_engine import rank_setups
    ranked = rank_setups(setups_list)

Or from nightly_swing_scan (auto-called after scan).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

# ── Grade thresholds ─────────────────────────────────────────────────────────
GRADE_A_THRESHOLD = 68  # composite score >= 68 → Grade A
GRADE_B_THRESHOLD = 48  # composite score >= 48 → Grade B
# below 48 → Grade C


# ── Factor weights (must sum to 1.0) ─────────────────────────────────────────
WEIGHTS = {
    "rr": 0.30,
    "strength": 0.25,
    "volume": 0.20,
    "confluence": 0.15,
    "sector": 0.10,
}


@dataclass
class RankResult:
    """Ranking output for one setup."""

    symbol: str
    pattern: str
    direction: str
    original_grade: str  # scanner ka original grade
    composite_score: float  # 0-100
    new_grade: str  # re-assigned grade
    rr_score: float
    strength_score: float
    volume_score: float
    confluence_score: float
    sector_score: float
    entry_zone: float
    stop_loss: float
    target_1: float
    target_2: float
    risk_reward: float
    timeframe: str
    description: str  # human-readable entry guidance (alias of entry_note)
    entry_note: str
    sector_tag: str
    setup_date: str
    data_as_of: str
    # Raw scanner strength (before ranking — different from strength_score factor)
    strength: int
    # Fib targets (passed through for UI if available)
    fib_tp1: float
    fib_tp2: float
    # Risk transparency fields
    sl_capped: bool
    sl_warning: str
    # Supporting indicator snapshot (vol_ratio, etc.)
    indicators: dict
    # News / confluence context
    news_score: float
    confluence_count: int


# ── Individual factor scorers (each returns 0.0–100.0) ───────────────────────


def _score_rr(rr: float) -> float:
    """
    R:R scoring (most important factor — risk kab take karna hai).

    R:R  < 1.0  →  0   (scanner allow karega nahi, but safety check)
    R:R = 1.5   → 30
    R:R = 2.0   → 55
    R:R = 2.5   → 72
    R:R = 3.0   → 85
    R:R >= 4.0  → 100
    """
    if rr <= 0:
        return 0.0
    if rr < 1.5:
        return max(0.0, rr * 15.0)
    if rr < 2.0:
        return 30.0 + (rr - 1.5) / 0.5 * 25.0  # 30→55
    if rr < 3.0:
        return 55.0 + (rr - 2.0) / 1.0 * 30.0  # 55→85
    if rr < 4.0:
        return 85.0 + (rr - 3.0) / 1.0 * 15.0  # 85→100
    return 100.0


def _score_strength(strength: int) -> float:
    """
    Linear: strength 0-100 → score 0-100.
    Below 50 gets penalised slightly.
    """
    if strength <= 0:
        return 0.0
    if strength < 50:
        return float(strength) * 0.7  # penalty for weak signals
    return float(strength)


def _score_volume(vol_ratio: float) -> float:
    """
    Volume ratio (current vol / 20-day avg).

    vol_ratio < 0.5  →  0   (illiquid — avoid)
    vol_ratio = 1.0  → 40   (average volume)
    vol_ratio = 1.5  → 65   (good confirmation)
    vol_ratio = 2.0  → 85   (strong breakout volume)
    vol_ratio >= 3.0 → 100  (exceptional volume)
    """
    if vol_ratio <= 0:
        return 0.0
    if vol_ratio < 0.5:
        return 0.0
    if vol_ratio < 1.0:
        return (vol_ratio - 0.5) / 0.5 * 40.0  # 0→40
    if vol_ratio < 1.5:
        return 40.0 + (vol_ratio - 1.0) / 0.5 * 25.0  # 40→65
    if vol_ratio < 2.0:
        return 65.0 + (vol_ratio - 1.5) / 0.5 * 20.0  # 65→85
    if vol_ratio < 3.0:
        return 85.0 + (vol_ratio - 2.0) / 1.0 * 15.0  # 85→100
    return 100.0


def _score_confluence(count: int) -> float:
    """
    Zyada patterns ek hi direction mein agree karein → better.

    1 pattern  → 30
    2 patterns → 65
    3+ patterns → 100
    """
    if count <= 0:
        return 0.0
    if count == 1:
        return 30.0
    if count == 2:
        return 65.0
    return 100.0


def _score_sector(sector_tag: str) -> float:
    """
    TAILWIND = 100, NEUTRAL = 50, HEADWIND = 0.
    """
    tag = str(sector_tag).upper()
    if "TAILWIND" in tag:
        return 100.0
    if "HEADWIND" in tag:
        return 0.0
    return 50.0  # NEUTRAL


# ── Main ranking function ─────────────────────────────────────────────────────


def _new_grade(score: float, news_penalised: bool = False) -> str:
    """Composite score → Grade."""
    if news_penalised:
        return "C"
    if score >= GRADE_A_THRESHOLD:
        return "A"
    if score >= GRADE_B_THRESHOLD:
        return "B"
    return "C"


def rank_single(setup: dict[str, Any]) -> RankResult:
    """
    Ek setup dict ko score karo (asdict() output from SwingSetup).

    setup dict mein yeh keys expected hain:
        symbol, pattern, direction, grade, strength, risk_reward,
        entry_zone, stop_loss, target_1, timeframe, entry_note,
        sector_tag, setup_date, data_as_of, confluence_count,
        news_penalty_applied, indicators
    """
    indicators = setup.get("indicators") or {}

    # Factor scores
    rr_s = _score_rr(float(setup.get("risk_reward", 0) or 0))
    str_s = _score_strength(int(setup.get("strength", 0) or 0))
    vol_s = _score_volume(float(indicators.get("vol_ratio", 1.0) or 1.0))
    conf_s = _score_confluence(int(setup.get("confluence_count", 1) or 1))
    sec_s = _score_sector(str(setup.get("sector_tag", "") or ""))

    # Weighted composite
    composite = (
        rr_s * WEIGHTS["rr"]
        + str_s * WEIGHTS["strength"]
        + vol_s * WEIGHTS["volume"]
        + conf_s * WEIGHTS["confluence"]
        + sec_s * WEIGHTS["sector"]
    )
    composite = round(composite, 1)

    news_penalised = bool(setup.get("news_penalty_applied", False))
    grade = _new_grade(composite, news_penalised)

    raw_entry_note = str(setup.get("entry_note", "") or "")
    raw_description = str(setup.get("description", "") or raw_entry_note)

    return RankResult(
        symbol=setup.get("symbol", ""),
        pattern=setup.get("pattern", ""),
        direction=setup.get("direction", ""),
        original_grade=setup.get("grade", "C"),
        composite_score=composite,
        new_grade=grade,
        rr_score=round(rr_s, 1),
        strength_score=round(str_s, 1),
        volume_score=round(vol_s, 1),
        confluence_score=round(conf_s, 1),
        sector_score=round(sec_s, 1),
        entry_zone=float(setup.get("entry_zone", 0) or 0),
        stop_loss=float(setup.get("stop_loss", 0) or 0),
        target_1=float(setup.get("target_1", 0) or 0),
        target_2=float(setup.get("target_2", 0) or 0),
        risk_reward=float(setup.get("risk_reward", 0) or 0),
        timeframe=str(setup.get("timeframe", "") or ""),
        description=raw_description,
        entry_note=raw_entry_note,
        sector_tag=str(setup.get("sector_tag", "") or ""),
        setup_date=str(setup.get("setup_date", "") or ""),
        data_as_of=str(setup.get("data_as_of", "") or ""),
        strength=int(setup.get("strength", 0) or 0),
        fib_tp1=float(setup.get("fib_tp1", 0) or 0),
        fib_tp2=float(setup.get("fib_tp2", 0) or 0),
        sl_capped=bool(setup.get("sl_capped", False)),
        sl_warning=str(setup.get("sl_warning", "") or ""),
        indicators=dict(setup.get("indicators") or {}),
        news_score=float(setup.get("news_score", 0) or 0),
        confluence_count=int(setup.get("confluence_count", 1) or 1),
    )


def rank_setups(setups: list[dict[str, Any]]) -> list[RankResult]:
    """
    Setups ki list ko rank karo.

    Returns: RankResult list sorted by:
      1. new_grade (A > B > C)
      2. composite_score (high first)
    """
    ranked = [rank_single(s) for s in setups]
    grade_order = {"A": 0, "B": 1, "C": 2}
    ranked.sort(key=lambda r: (grade_order.get(r.new_grade, 3), -r.composite_score))
    return ranked


def rank_summary(ranked: list[RankResult]) -> dict:
    """Grade-wise count aur top picks summary return karo."""
    grade_a = [r for r in ranked if r.new_grade == "A"]
    grade_b = [r for r in ranked if r.new_grade == "B"]
    grade_c = [r for r in ranked if r.new_grade == "C"]
    return {
        "total": len(ranked),
        "grade_a": len(grade_a),
        "grade_b": len(grade_b),
        "grade_c": len(grade_c),
        "top_picks": [
            {
                # ── Identity ──────────────────────────────────────────────
                "rank": i + 1,
                "symbol": r.symbol,
                "grade": r.new_grade,
                "original_grade": r.original_grade,
                "direction": r.direction,
                "pattern": r.pattern,
                # ── Composite ranking score ───────────────────────────────
                "composite_score": r.composite_score,
                "strength": r.strength,  # raw scanner strength 0-100
                # ── Full-name price levels (dashboard grid keys) ──────────
                "entry_zone": r.entry_zone,
                "stop_loss": r.stop_loss,
                "target_1": r.target_1,
                "target_2": r.target_2,
                "risk_reward": r.risk_reward,
                # ── Fib targets (may be 0.0 if not computed) ─────────────
                "fib_tp1": r.fib_tp1,
                "fib_tp2": r.fib_tp2,
                # ── Short aliases kept for backwards-compat consumers ─────
                "entry": r.entry_zone,
                "sl": r.stop_loss,
                "t1": r.target_1,
                "t2": r.target_2,
                "rr": r.risk_reward,
                # ── Narrative / context ───────────────────────────────────
                "timeframe": r.timeframe,
                "description": r.description,
                "entry_note": r.entry_note,
                "sector_tag": r.sector_tag,
                "setup_date": r.setup_date,
                "data_as_of": r.data_as_of,
                # ── Risk transparency ─────────────────────────────────────
                "sl_capped": r.sl_capped,
                "sl_warning": r.sl_warning,
                # ── Supporting data ───────────────────────────────────────
                "indicators": r.indicators,
                "news_score": r.news_score,
                "confluence_count": r.confluence_count,
                "score_breakdown": {
                    "rr_score": r.rr_score,
                    "strength_score": r.strength_score,
                    "volume_score": r.volume_score,
                    "confluence_score": r.confluence_score,
                    "sector_score": r.sector_score,
                },
            }
            for i, r in enumerate(ranked)
        ],
    }
