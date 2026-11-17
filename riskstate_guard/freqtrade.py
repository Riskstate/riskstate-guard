"""Freqtrade integration.

    from riskstate_guard.freqtrade import RiskStateMixin

    class MyStrategy(RiskStateMixin, IStrategy):
        riskstate = {"mode": "shadow"}      # start here; switch to "enforce" later
        ...

The mixin hooks three Freqtrade callbacks:

  custom_stake_amount     caps the stake so the asset's total notional stays inside the permission
  confirm_trade_entry     last check before the order is sent; rejects when new entries are blocked
  adjust_trade_position   caps or drops position increases (DCA); reductions pass untouched

Freqtrade treats an exception inside a callback as "use the default", which for these hooks
means "let the trade through". So the mixin never raises: any internal error is a refusal in
enforce mode.

It only acts in live and dry-run. In backtesting and hyperopt it passes everything through:
the API serves the current policy, not a historical one, so applying it to past candles
would be wrong.

Put the mixin BEFORE IStrategy in the class bases. Your strategy may define any of these
callbacks itself: the guard is applied to whatever they return, with or without `super()`.
"""

from __future__ import annotations

import functools
import logging
import os
from datetime import datetime
from typing import Any, Optional

from .guard import Decision, GuardConfig, RiskStateGuard, base_asset

logger = logging.getLogger("riskstate_guard")

_LIVE_MODES = ("live", "dry_run")


class RiskStateMixin:
    #: Overrides for GuardConfig. `api_key` defaults to the RISKSTATE_API_KEY environment variable.
    riskstate: dict = {}

    _rs_guard: Optional[RiskStateGuard] = None
    _rs_warned_backtest = False
    _rs_depth = 0  # > 0 while a wrapped callback of the strategy is running

    def __init_subclass__(cls, **kwargs: Any) -> None:
        """Wrap the callbacks a strategy defines itself, so the guard always has the last word.

        Without this, a strategy that overrides `custom_stake_amount` and forgets `super()` would
        silently run ungoverned.
        """
        super().__init_subclass__(**kwargs)
        for name, post in (("custom_stake_amount", "_rs_post_stake"), ("confirm_trade_entry", "_rs_post_confirm"),
                           ("adjust_trade_position", "_rs_post_adjust")):
            fn = cls.__dict__.get(name)
            if fn is None or getattr(fn, "_rs_wrapped", False):
                continue
            cls._rs_wrap(name, fn, post)

    @classmethod
    def _rs_wrap(cls, name: str, fn: Any, post: str) -> None:
        @functools.wraps(fn)
        def wrapper(self: Any, *args: Any, **kwargs: Any) -> Any:
            self._rs_depth += 1
            try:
                result = fn(self, *args, **kwargs)
            finally:
                self._rs_depth -= 1
            if self._rs_depth > 0:      # an outer wrapped callback will apply the guard
                return result
            return getattr(self, post)(result, _rs_bind(fn, self, args, kwargs))

        wrapper._rs_wrapped = True  # type: ignore[attr-defined]
        setattr(cls, name, wrapper)

    # ── plumbing ──────────────────────────────────────────────────────────────
    def _rs_get_guard(self) -> RiskStateGuard:
        if self._rs_guard is None:
            opts = dict(self.riskstate or {})
            opts.setdefault("api_key", os.environ.get("RISKSTATE_API_KEY", ""))
            self._rs_guard = RiskStateGuard(GuardConfig(**opts))
            logger.info("RiskState guard active: mode=%s fail_mode=%s", self._rs_guard.config.mode,
                        self._rs_guard.config.fail_mode)
        return self._rs_guard

    def _rs_active(self) -> bool:
        dp = getattr(self, "dp", None)
        mode = getattr(getattr(dp, "runmode", None), "value", None)
        if mode in _LIVE_MODES:
            return True
        if not self._rs_warned_backtest:
            type(self)._rs_warned_backtest = True
            logger.warning("RiskState guard is inactive in runmode %s: the API serves current policy only.", mode)
        return False

    def _rs_portfolio_value(self) -> float:
        wallets = getattr(self, "wallets", None)
        if wallets is None:
            return float("nan")
        try:
            return float(wallets.get_total_stake_amount())
        except Exception:
            try:
                return float(wallets.get_total(self.config["stake_currency"]))
            except Exception:
                return float("nan")

    @staticmethod
    def _rs_open_notional(asset: Optional[str]) -> float:
        """Notional already open in this asset, across every pair that trades it."""
        if asset is None:
            return 0.0
        try:
            from freqtrade.persistence import Trade

            total = 0.0
            for t in Trade.get_trades_proxy(is_open=True):
                if base_asset(t.pair) == asset:
                    total += float(t.stake_amount or 0.0) * float(getattr(t, "leverage", None) or 1.0)
            return total
        except Exception:
            return float("nan")  # unknown exposure is treated as invalid input: the entry is refused

    def _rs_check(self, pair: str, notional: float) -> Decision:
        try:
            guard = self._rs_get_guard()
            d = guard.check_entry(pair, notional, self._rs_portfolio_value(),
                                  self._rs_open_notional(base_asset(pair)))
        except Exception as exc:  # never let an internal error become an approval
            mode = str((self.riskstate or {}).get("mode", "enforce"))
            logger.error("RiskState guard error (%s): entry refused", type(exc).__name__)
            return Decision(pair=pair, asset=base_asset(pair), allowed=False, max_notional=0.0, notional=0.0,
                            proposed_notional=notional, reason="GUARD_ERROR:" + type(exc).__name__, mode=mode)
        return d

    @staticmethod
    def _rs_log(d: Decision, outcome: Optional[str]) -> None:
        """Log what actually happened to the order, once per change."""
        if outcome and d.changed:
            logger.info("RiskState %s %s: %s (proposed %.2f, room %.2f, level %s)",
                        "would" if d.mode == "shadow" else "did", outcome, d.reason,
                        d.proposed_notional, d.max_notional, d.policy_level)

    @staticmethod
    def _rs_outcome(d: Decision, lev: float, min_stake: Any) -> Optional[str]:
        if not d.allowed:
            return "block"
        if min_stake is not None and d.notional / lev < min_stake:
            return "skip (room left is below the exchange minimum)"
        return "resize" if d.resized else None

    # ── guard applied to a callback's result ──────────────────────────────────
    def _rs_post_stake(self, stake: Any, a: dict) -> Any:
        if not self._rs_active() or not stake:
            return stake
        lev = a.get("leverage")
        lev = lev if isinstance(lev, (int, float)) and lev > 0 else 1.0
        d = self._rs_check(str(a.get("pair", "")), stake * lev)
        self._rs_log(d, self._rs_outcome(d, lev, a.get("min_stake")))
        if d.mode == "shadow":
            return stake
        if not d.allowed:
            return 0.0
        capped = d.notional / lev
        min_stake = a.get("min_stake")
        if min_stake is not None and capped < min_stake:
            return 0.0  # what is left is below the exchange minimum: skip the entry
        return min(stake, capped)

    def _rs_post_confirm(self, ok: Any, a: dict) -> bool:
        if not ok:
            return False
        if not self._rs_active():
            return True
        try:
            notional = float(a["amount"]) * float(a["rate"])
        except Exception:
            notional = float("nan")  # refused as invalid input
        d = self._rs_check(str(a.get("pair", "")), notional)
        refused = not d.allowed or not notional <= d.max_notional * 1.01
        self._rs_log(d, "block" if refused else None)
        if d.mode == "shadow":
            return True
        # 1 % tolerance: the stake was capped a moment ago at a slightly different price.
        return not refused

    def _rs_post_adjust(self, result: Any, a: dict) -> Any:
        stake, tag = (result if isinstance(result, tuple) else (result, None))
        # None = no change; negative = reduction. Neither adds exposure.
        if not isinstance(stake, (int, float)) or isinstance(stake, bool) or not stake > 0 or not self._rs_active():
            return result
        trade = a.get("trade")
        try:
            lev = float(getattr(trade, "leverage", None) or 1.0)
            pair = str(trade.pair)
        except Exception:
            lev, pair = 1.0, ""
        d = self._rs_check(pair, stake * lev)  # open notional already includes this trade
        self._rs_log(d, self._rs_outcome(d, lev, a.get("min_stake")))
        if d.mode == "shadow":
            return result
        if not d.allowed:
            return None
        capped = min(stake, d.notional / lev)
        min_stake = a.get("min_stake")
        if min_stake is not None and capped < min_stake:
            return None
        return (capped, tag) if isinstance(result, tuple) else capped

    # ── Freqtrade callbacks (used when the strategy does not define its own) ──
    def custom_stake_amount(self, pair: str, current_time: datetime, current_rate: float, proposed_stake: float,
                            min_stake: Optional[float], max_stake: float, leverage: float,
                            entry_tag: Optional[str], side: str, **kwargs: Any) -> float:
        parent = getattr(super(), "custom_stake_amount", None)
        stake = proposed_stake if parent is None else parent(
            pair=pair, current_time=current_time, current_rate=current_rate, proposed_stake=proposed_stake,
            min_stake=min_stake, max_stake=max_stake, leverage=leverage, entry_tag=entry_tag, side=side, **kwargs)
        if self._rs_depth > 0:
            return stake
        return self._rs_post_stake(stake, {"pair": pair, "leverage": leverage, "min_stake": min_stake})

    def confirm_trade_entry(self, pair: str, order_type: str, amount: float, rate: float, time_in_force: str,
                            current_time: datetime, entry_tag: Optional[str], side: str, **kwargs: Any) -> bool:
        parent = getattr(super(), "confirm_trade_entry", None)
        ok = True if parent is None else parent(
            pair=pair, order_type=order_type, amount=amount, rate=rate, time_in_force=time_in_force,
            current_time=current_time, entry_tag=entry_tag, side=side, **kwargs)
        if self._rs_depth > 0:
            return ok
        return self._rs_post_confirm(ok, {"pair": pair, "amount": amount, "rate": rate})

    def adjust_trade_position(self, trade: Any, current_time: datetime, current_rate: float, current_profit: float,
                              min_stake: Optional[float], max_stake: float, current_entry_rate: float,
                              current_exit_rate: float, current_entry_profit: float, current_exit_profit: float,
                              **kwargs: Any) -> Any:
        parent = getattr(super(), "adjust_trade_position", None)
        if parent is None:
            return None
        result = parent(trade=trade, current_time=current_time, current_rate=current_rate,
                        current_profit=current_profit, min_stake=min_stake, max_stake=max_stake,
                        current_entry_rate=current_entry_rate, current_exit_rate=current_exit_rate,
                        current_entry_profit=current_entry_profit, current_exit_profit=current_exit_profit, **kwargs)
        if self._rs_depth > 0:
            return result
        return self._rs_post_adjust(result, {"trade": trade, "min_stake": min_stake})


def _rs_bind(fn: Any, self: Any, args: tuple, kwargs: dict) -> dict:
    """Arguments of a callback by name, however the caller passed them."""
    import inspect

    try:
        bound = inspect.signature(fn).bind_partial(self, *args, **kwargs)
        out = dict(bound.arguments)
        extra = out.pop("kwargs", None)
        if isinstance(extra, dict):
            out.update(extra)
        return out
    except Exception:
        return dict(kwargs)
