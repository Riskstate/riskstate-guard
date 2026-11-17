"""Minimal Freqtrade strategy governed by RiskState.

Copy to user_data/strategies/, set RISKSTATE_API_KEY, and run in dry-run:

    freqtrade trade --strategy RiskStateSampleStrategy --dry-run

The entry logic (an EMA cross) is a placeholder. The point is the two lines marked below.
"""

from pandas import DataFrame

from freqtrade.strategy import IStrategy

from riskstate_guard.freqtrade import RiskStateMixin          # <-- 1


class RiskStateSampleStrategy(RiskStateMixin, IStrategy):     # <-- 2: mixin first
    INTERFACE_VERSION = 3
    timeframe = "1h"
    minimal_roi = {"0": 0.05}
    stoploss = -0.05
    startup_candle_count = 50

    riskstate = {
        "mode": "shadow",                 # log what the guard would do; change to "enforce" when ready
        "fail_mode": "closed",            # no policy -> no new entries
        "local_max_fraction": 0.5,        # your own ceiling, applied even if RiskState is unreachable
        "log_path": "user_data/logs/riskstate-decisions.jsonl",
    }

    def populate_indicators(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe["ema_fast"] = dataframe["close"].ewm(span=12, adjust=False).mean()
        dataframe["ema_slow"] = dataframe["close"].ewm(span=48, adjust=False).mean()
        return dataframe

    def populate_entry_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe.loc[dataframe["ema_fast"] > dataframe["ema_slow"], "enter_long"] = 1
        return dataframe

    def populate_exit_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe.loc[dataframe["ema_fast"] < dataframe["ema_slow"], "exit_long"] = 1
        return dataframe
