"""
Point-in-time daily panel built from official NSE bhavcopies.

Why this exists
---------------
A backtest is only as honest as its data. Two biases break most NSE backtests:

1. **Survivorship** — testing today's index members over ten years silently
   excludes every company that was delisted or removed.
2. **Corporate actions** — an unadjusted 1:2 split looks like a −50% day and
   will trigger every stop in the history.

This module builds a dated panel from the official UDiFF bhavcopy, applies
split/bonus adjustments, and can restrict rows to a point-in-time membership
table. It never backtracks a holiday to an older file (that would mislabel
data); holidays are simply absent.

Typical use:

    panel = BhavcopyPanel().load(date(2024, 1, 1), date(2024, 12, 31),
                                 symbols=universe.symbols_on(date(2024, 6, 1)),
                                 actions=actions_df)
    closes = BhavcopyPanel.to_wide(panel, "close")
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Iterable, Optional

import numpy as np
import pandas as pd

from data.providers.nse_bhavcopy import NSEBhavcopyProvider

logger = logging.getLogger(__name__)

PRICE_COLUMNS = ("open", "high", "low", "close", "prev_close")
PANEL_COLUMNS = ["date", "symbol", *PRICE_COLUMNS, "volume", "turnover"]


def _to_date(value) -> date:
    if isinstance(value, date) and not isinstance(value, pd.Timestamp):
        return value
    return pd.Timestamp(value).date()


def adjust_for_corporate_actions(
    panel: pd.DataFrame,
    actions: pd.DataFrame,
    symbol_col: str = "symbol",
    date_col: str = "date",
    price_cols: Iterable[str] = PRICE_COLUMNS,
    volume_col: str = "volume",
) -> pd.DataFrame:
    """Back-adjust prices for splits and bonus issues.

    ``actions`` needs ``symbol``, ``ex_date`` and ``ratio`` where ratio is
    "new shares per old share":

        - 1:2 split  → ratio 2.0
        - 1:1 bonus  → ratio 2.0
        - 3:2 bonus  → ratio 1.5

    Prices on dates **before** ``ex_date`` are divided by the ratio; volume is
    multiplied by it. Rows on/after the ex-date are untouched.
    """
    if panel is None or panel.empty:
        return pd.DataFrame(columns=PANEL_COLUMNS)
    if actions is None or len(actions) == 0:
        return panel.copy()

    required = {"symbol", "ex_date", "ratio"}
    missing = required - set(actions.columns)
    if missing:
        raise ValueError(f"corporate actions missing columns: {sorted(missing)}")

    out = panel.copy()
    out[date_col] = pd.to_datetime(out[date_col])

    acts = actions.copy()
    acts["ex_date"] = pd.to_datetime(acts["ex_date"])
    acts = acts.sort_values("ex_date", ascending=False)

    for _, action in acts.iterrows():
        ratio = float(action["ratio"])
        if not np.isfinite(ratio) or ratio <= 0 or ratio == 1.0:
            continue
        mask = (out[symbol_col].astype(str) == str(action["symbol"])) & (
            out[date_col] < action["ex_date"]
        )
        if not mask.any():
            continue
        for column in price_cols:
            if column in out.columns:
                out.loc[mask, column] = out.loc[mask, column] / ratio
        if volume_col in out.columns:
            out.loc[mask, volume_col] = out.loc[mask, volume_col] * ratio

    return out


class BhavcopyPanel:
    """Build a dated OHLCV panel from official NSE bhavcopy files."""

    def __init__(
        self,
        provider: Optional[NSEBhavcopyProvider] = None,
        cache_dir=None,
    ):
        self.provider = provider or NSEBhavcopyProvider(cache_dir=cache_dir)

    def load(
        self,
        start: date,
        end: date,
        symbols: Optional[Iterable[str]] = None,
        allow_download: bool = True,
        actions: Optional[pd.DataFrame] = None,
    ) -> pd.DataFrame:
        """Collect every available trading day in ``[start, end]``.

        ``allow_download=False`` reads only the local cache — useful for tests
        and for offline backtests.
        """
        start = _to_date(start)
        end = _to_date(end)
        if end < start:
            raise ValueError("end date must be on or after start date")

        frames = []
        cursor = start
        while cursor <= end:
            if cursor.weekday() < 5:
                try:
                    daily = self.provider.fetch_daily_exact(
                        cursor, allow_download=allow_download
                    )
                except TypeError:
                    # Older provider signature without allow_download
                    daily = self.provider.fetch_daily_exact(cursor)
                except Exception as exc:
                    logger.debug("[BhavcopyPanel] %s skipped: %s", cursor, exc)
                    daily = pd.DataFrame()

                if daily is not None and not daily.empty:
                    daily = daily.copy()
                    if "date" in daily.columns:
                        daily["date"] = pd.to_datetime(daily["date"], errors="coerce")
                    else:
                        daily["date"] = pd.Timestamp(cursor)
                    frames.append(daily)
            cursor += timedelta(days=1)

        if not frames:
            return pd.DataFrame(columns=PANEL_COLUMNS)

        panel = pd.concat(frames, ignore_index=True)
        panel["date"] = pd.to_datetime(panel["date"], errors="coerce")
        panel = panel.dropna(subset=["date", "symbol"])
        panel["symbol"] = panel["symbol"].astype(str).str.upper().str.strip()

        if symbols:
            wanted = {str(s).upper().strip() for s in symbols}
            panel = panel[panel["symbol"].isin(wanted)]

        for column in ("open", "high", "low", "close", "prev_close", "volume", "turnover"):
            if column in panel.columns:
                panel[column] = pd.to_numeric(panel[column], errors="coerce")

        panel = (
            panel.drop_duplicates(["date", "symbol"], keep="last")
            .sort_values(["symbol", "date"])
            .reset_index(drop=True)
        )

        if actions is not None and len(actions):
            panel = adjust_for_corporate_actions(panel, actions)

        return panel

    @staticmethod
    def to_wide(panel: pd.DataFrame, field: str = "close") -> pd.DataFrame:
        """Pivot a long panel into date × symbol for one field."""
        if panel is None or panel.empty:
            return pd.DataFrame()
        if field not in panel.columns:
            raise ValueError(f"field '{field}' not present in panel")
        wide = panel.pivot(index="date", columns="symbol", values=field)
        return wide.sort_index()


@dataclass(frozen=True)
class Membership:
    """A symbol's inclusion window in a universe."""

    symbol: str
    start: date
    end: Optional[date] = None

    def __post_init__(self):
        if self.end is not None and self.end < self.start:
            raise ValueError(f"membership end before start for {self.symbol}")

    def covers(self, day: date) -> bool:
        if day < self.start:
            return False
        return self.end is None or day <= self.end


class PointInTimeUniverse:
    """Restrict a panel to the symbols that were actually in the index."""

    def __init__(self, memberships: Iterable[Membership]):
        self.memberships = list(memberships)

    def symbols_on(self, day: date) -> set[str]:
        day = _to_date(day)
        return {m.symbol for m in self.memberships if m.covers(day)}

    def filter(self, panel: pd.DataFrame, date_col: str = "date", symbol_col: str = "symbol") -> pd.DataFrame:
        if panel is None or panel.empty:
            return panel
        out = panel.copy()
        out[date_col] = pd.to_datetime(out[date_col])

        mask = pd.Series(False, index=out.index)
        for membership in self.memberships:
            condition = (out[symbol_col].astype(str) == membership.symbol) & (
                out[date_col] >= pd.Timestamp(membership.start)
            )
            if membership.end is not None:
                condition &= out[date_col] <= pd.Timestamp(membership.end)
            mask |= condition

        return out[mask].reset_index(drop=True)

    @classmethod
    def from_records(cls, rows: Iterable[dict]) -> "PointInTimeUniverse":
        memberships = []
        for row in rows:
            memberships.append(
                Membership(
                    symbol=str(row["symbol"]).upper().strip(),
                    start=_to_date(row["start"]),
                    end=_to_date(row["end"]) if row.get("end") else None,
                )
            )
        return cls(memberships)

    @classmethod
    def static(cls, symbols: Iterable[str], start: date, end: Optional[date] = None) -> "PointInTimeUniverse":
        """Convenience for an explicit, survivorship-biased universe.

        Use only when you can state the limitation; it does not fix survivorship.
        """
        return cls(
            Membership(symbol=str(s).upper().strip(), start=_to_date(start), end=end)
            for s in symbols
        )
