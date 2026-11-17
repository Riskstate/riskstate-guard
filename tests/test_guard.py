import json
import math
import os
import sys
import tempfile
import types
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from riskstate_guard import GuardConfig, RiskStateGuard, base_asset  # noqa: E402
from riskstate_guard.freqtrade import RiskStateMixin  # noqa: E402


def policy(size=0.2, blocked=(), level=2, ttl=60, asset="BTC"):
    return {"asset": asset, "exposure_policy": {"max_size_fraction": size, "blocked_actions": list(blocked)},
            "policy_level": level, "ttl_seconds": ttl, "policy_hash": "abc", "binding_constraint": {"source": "CYCLE"}}


class Clock:
    def __init__(self): self.t = 1_000_000.0
    def __call__(self): return self.t


def make(fetch, **cfg):
    clock = Clock()
    return RiskStateGuard(GuardConfig(api_key="k", **cfg), fetch=fetch, clock=clock), clock


class GuardTests(unittest.TestCase):
    def test_base_asset(self):
        self.assertEqual(base_asset("BTC/USDT"), "BTC")
        self.assertEqual(base_asset("ETH/USDT:USDT"), "ETH")
        self.assertEqual(base_asset("WBTC/USDC"), "BTC")
        self.assertIsNone(base_asset("SOL/USDT"))

    def test_within_limit_untouched(self):
        g, _ = make(lambda a: policy(0.2))
        d = g.check_entry("BTC/USDT", 1000, 10000)
        self.assertTrue(d.allowed); self.assertEqual(d.notional, 1000); self.assertFalse(d.resized)

    def test_resized_to_cap(self):
        g, _ = make(lambda a: policy(0.2))
        d = g.check_entry("BTC/USDT", 5000, 10000)
        self.assertTrue(d.allowed); self.assertAlmostEqual(d.notional, 2000); self.assertEqual(d.reason, "SIZE_CAP")

    def test_existing_exposure_counts(self):
        g, _ = make(lambda a: policy(0.2))
        d = g.check_entry("BTC/USDT", 1000, 10000, open_notional=1500)
        self.assertAlmostEqual(d.notional, 500)
        d = g.check_entry("BTC/USDT", 1000, 10000, open_notional=2000)
        self.assertFalse(d.allowed); self.assertEqual(d.reason, "SIZE_CAP_NO_ROOM")

    def test_new_trades_blocked(self):
        g, _ = make(lambda a: policy(0.1, blocked=["NEW_TRADES", "LEVERAGE"], level=1))
        d = g.check_entry("BTC/USDT", 100, 10000)
        self.assertFalse(d.allowed); self.assertEqual(d.reason, "NEW_TRADES_BLOCKED")

    def test_fail_closed_when_api_down(self):
        def boom(a): raise OSError("down")
        g, _ = make(boom)
        d = g.check_entry("BTC/USDT", 100, 10000)
        self.assertFalse(d.allowed); self.assertTrue(d.reason.startswith("NO_POLICY_FAIL_CLOSED"))

    def test_fail_open_uses_local_cap(self):
        def boom(a): raise OSError("down")
        g, _ = make(boom, fail_mode="open", local_max_fraction=0.05)
        d = g.check_entry("BTC/USDT", 1000, 10000)
        self.assertTrue(d.allowed); self.assertAlmostEqual(d.notional, 500)

    def test_stale_policy_served_then_expires(self):
        calls = {"n": 0}
        def fetch(a):
            calls["n"] += 1
            if calls["n"] > 1: raise OSError("down")
            return policy(0.2)
        g, clock = make(fetch, max_policy_age_s=900, retry_s=0)
        self.assertTrue(g.check_entry("BTC/USDT", 100, 10000).allowed)
        clock.t += 600  # refresh fails, cached policy is 10 min old: still a permit
        d = g.check_entry("BTC/USDT", 100, 10000)
        self.assertTrue(d.allowed); self.assertEqual(d.policy_age_s, 600.0)
        clock.t += 400  # now older than max_policy_age_s
        self.assertFalse(g.check_entry("BTC/USDT", 100, 10000).allowed)

    def test_cache_respects_ttl(self):
        calls = {"n": 0}
        def fetch(a): calls["n"] += 1; return policy(0.2, ttl=60)
        g, clock = make(fetch)
        g.check_entry("BTC/USDT", 1, 10000); g.check_entry("BTC/USDT", 1, 10000)
        self.assertEqual(calls["n"], 1)
        clock.t += 61
        g.check_entry("BTC/USDT", 1, 10000)
        self.assertEqual(calls["n"], 2)

    def test_failed_fetch_backs_off(self):
        calls = {"n": 0}
        def fetch(a):
            calls["n"] += 1
            if calls["n"] < 3: raise OSError("down")
            return policy(0.2)
        g, clock = make(fetch, retry_s=30)
        for _ in range(5): self.assertFalse(g.check_entry("BTC/USDT", 100, 10000).allowed)
        self.assertEqual(calls["n"], 1)
        clock.t += 31; g.check_entry("BTC/USDT", 100, 10000); self.assertEqual(calls["n"], 2)
        clock.t += 31; self.assertTrue(g.check_entry("BTC/USDT", 100, 10000).allowed)

    def test_malformed_payload_fails_closed(self):
        for bad in ({}, {"exposure_policy": {"max_size_fraction": float("nan"), "blocked_actions": []}},
                    {"exposure_policy": {"max_size_fraction": "0.5", "blocked_actions": []}},
                    {"exposure_policy": {"max_size_fraction": 0.5}},
                    policy(0.5, asset="ETH")):
            g, _ = make(lambda a, bad=bad: bad)
            self.assertFalse(g.check_entry("BTC/USDT", 100, 10000).allowed, bad)

    def test_size_clamped_to_unit_interval(self):
        g, _ = make(lambda a: policy(7.0))
        self.assertAlmostEqual(g.check_entry("BTC/USDT", 50000, 10000).notional, 10000)

    def test_invalid_numbers_refused(self):
        g, _ = make(lambda a: policy(0.5))
        for bad in (float("nan"), float("inf"), -1):
            self.assertFalse(g.check_entry("BTC/USDT", bad, 10000).allowed)
            self.assertFalse(g.check_entry("BTC/USDT", 100, bad).allowed)
            self.assertFalse(g.check_entry("BTC/USDT", 100, 10000, open_notional=bad).allowed)

    def test_local_cap_tighter_than_engine(self):
        g, _ = make(lambda a: policy(0.5), local_max_fraction=0.1)
        d = g.check_entry("BTC/USDT", 4000, 10000)
        self.assertAlmostEqual(d.notional, 1000); self.assertEqual(d.reason, "LOCAL_CAP")

    def test_unsupported_pairs(self):
        g, _ = make(lambda a: policy(0.0))
        d = g.check_entry("SOL/USDT", 100, 10000)
        self.assertTrue(d.allowed); self.assertFalse(d.governed)
        g, _ = make(lambda a: policy(0.5), unsupported_pairs="block")
        self.assertFalse(g.check_entry("SOL/USDT", 100, 10000).allowed)

    def test_log_written(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "d.jsonl")
            g, _ = make(lambda a: policy(0.2), log_path=path)
            g.check_entry("BTC/USDT", 5000, 10000)
            with open(path) as fh: row = json.loads(fh.read().strip())
            self.assertEqual(row["reason"], "SIZE_CAP"); self.assertEqual(row["policy_hash"], "abc")

    def test_repeated_decision_logged_once(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "d.jsonl")
            g, _ = make(lambda a: policy(0.2), log_path=path)
            for _ in range(5): g.check_entry("BTC/USDT", 5000, 10000)
            g.check_entry("BTC/USDT", 6000, 10000)
            with open(path) as fh: self.assertEqual(len(fh.read().strip().splitlines()), 2)

    def test_config_validation(self):
        with self.assertRaises(ValueError): RiskStateGuard(GuardConfig(mode="x"))
        with self.assertRaises(ValueError): RiskStateGuard(GuardConfig(local_max_fraction=2))


# ── mixin, against a stand-in for IStrategy ───────────────────────────────────
class Base:
    def custom_stake_amount(self, proposed_stake, **kw): return proposed_stake
    def confirm_trade_entry(self, **kw): return True
    adjust_result = None
    def adjust_trade_position(self, **kw): return self.adjust_result


def strategy(fetch, runmode="dry_run", total=10000.0, **cfg):
    class S(RiskStateMixin, Base):
        riskstate = dict(cfg)
    s = S()
    s._rs_guard = RiskStateGuard(GuardConfig(api_key="k", **cfg), fetch=fetch)
    s.dp = types.SimpleNamespace(runmode=types.SimpleNamespace(value=runmode))
    s.wallets = types.SimpleNamespace(get_total_stake_amount=lambda: total)
    s._rs_open_notional = lambda asset: 0.0
    return s


def stake(s, proposed, leverage=1.0, min_stake=10.0, pair="BTC/USDT"):
    return s.custom_stake_amount(pair=pair, current_time=None, current_rate=1.0, proposed_stake=proposed,
                                 min_stake=min_stake, max_stake=1e9, leverage=leverage, entry_tag=None, side="long")


def confirm(s, amount, rate=1.0, pair="BTC/USDT"):
    return s.confirm_trade_entry(pair=pair, order_type="limit", amount=amount, rate=rate, time_in_force="GTC",
                                 current_time=None, entry_tag=None, side="long")


class MixinTests(unittest.TestCase):
    def test_stake_capped(self):
        self.assertAlmostEqual(stake(strategy(lambda a: policy(0.2)), 5000), 2000)

    def test_leverage_counts_as_notional(self):
        self.assertAlmostEqual(stake(strategy(lambda a: policy(0.2)), 1000, leverage=5.0), 400)

    def test_blocked_returns_zero_and_refuses_confirm(self):
        s = strategy(lambda a: policy(0.1, blocked=["NEW_TRADES"]))
        self.assertEqual(stake(s, 500), 0.0); self.assertFalse(confirm(s, 500))

    def test_below_min_stake_skips(self):
        self.assertEqual(stake(strategy(lambda a: policy(0.0005)), 500, min_stake=10.0), 0.0)

    def test_shadow_changes_nothing(self):
        s = strategy(lambda a: policy(0.1, blocked=["NEW_TRADES"]), mode="shadow")
        self.assertEqual(stake(s, 5000), 5000); self.assertTrue(confirm(s, 5000))

    def test_backtest_passthrough(self):
        s = strategy(lambda a: policy(0.0, blocked=["NEW_TRADES"]), runmode="backtest")
        self.assertEqual(stake(s, 5000), 5000); self.assertTrue(confirm(s, 5000))

    def test_api_down_fails_closed(self):
        def boom(a): raise OSError("down")
        s = strategy(boom)
        self.assertEqual(stake(s, 500), 0.0); self.assertFalse(confirm(s, 500))

    def test_unknown_exposure_refused(self):
        s = strategy(lambda a: policy(0.5))
        s._rs_open_notional = lambda asset: math.nan
        self.assertEqual(stake(s, 500), 0.0)

    def test_confirm_rejects_oversize(self):
        s = strategy(lambda a: policy(0.2))
        self.assertTrue(confirm(s, 2000)); self.assertFalse(confirm(s, 2500))

    def test_internal_error_refuses(self):
        s = strategy(lambda a: policy(0.5))
        s.wallets = types.SimpleNamespace(get_total_stake_amount=lambda: 1 / 0)  # nan path
        self.assertEqual(stake(s, 500), 0.0)
        def explode(asset): raise RuntimeError("x")
        s = strategy(lambda a: policy(0.5)); s._rs_open_notional = explode
        self.assertEqual(stake(s, 500), 0.0); self.assertFalse(confirm(s, 500))

    def adjust(self, s, result, leverage=1.0, min_stake=10.0):
        s.adjust_result = result
        trade = types.SimpleNamespace(pair="BTC/USDT", leverage=leverage)
        return s.adjust_trade_position(trade=trade, current_time=None, current_rate=1.0, current_profit=0.0,
                                       min_stake=min_stake, max_stake=1e9, current_entry_rate=1.0,
                                       current_exit_rate=1.0, current_entry_profit=0.0, current_exit_profit=0.0)

    def test_position_increase_capped(self):
        s = strategy(lambda a: policy(0.2)); s._rs_open_notional = lambda asset: 1500.0
        self.assertAlmostEqual(self.adjust(s, 1000.0), 500.0)
        self.assertEqual(self.adjust(s, (1000.0, "dca")), (500.0, "dca"))

    def test_position_increase_dropped_when_blocked_or_full(self):
        s = strategy(lambda a: policy(0.2, blocked=["NEW_TRADES"]))
        self.assertIsNone(self.adjust(s, 1000.0))
        s = strategy(lambda a: policy(0.2)); s._rs_open_notional = lambda asset: 2000.0
        self.assertIsNone(self.adjust(s, 1000.0))

    def test_position_reduction_and_noop_pass(self):
        s = strategy(lambda a: policy(0.0, blocked=["NEW_TRADES"]))
        self.assertEqual(self.adjust(s, -300.0), -300.0); self.assertIsNone(self.adjust(s, None))


class OverrideTests(unittest.TestCase):
    """A strategy that defines the callbacks itself, with and without super()."""

    def build(self, call_super, fetch):
        class S(RiskStateMixin, Base):
            riskstate = {}
            def custom_stake_amount(self, pair, current_time, current_rate, proposed_stake, min_stake, max_stake,
                                    leverage, entry_tag, side, **kwargs):
                mine = proposed_stake * 2
                if call_super:
                    return super().custom_stake_amount(pair=pair, current_time=current_time, current_rate=current_rate,
                                                       proposed_stake=mine, min_stake=min_stake, max_stake=max_stake,
                                                       leverage=leverage, entry_tag=entry_tag, side=side, **kwargs)
                return mine
            def confirm_trade_entry(self, pair, order_type, amount, rate, time_in_force, current_time, entry_tag,
                                    side, **kwargs):
                return True
            def adjust_trade_position(self, trade, current_time, current_rate, current_profit, min_stake, max_stake,
                                      current_entry_rate, current_exit_rate, current_entry_profit,
                                      current_exit_profit, **kwargs):
                return 5000.0, "dca"
        s = S()
        s._rs_guard = RiskStateGuard(GuardConfig(api_key="k"), fetch=fetch)
        s.dp = types.SimpleNamespace(runmode=types.SimpleNamespace(value="live"))
        s.wallets = types.SimpleNamespace(get_total_stake_amount=lambda: 100000.0)
        s._rs_open_notional = lambda asset: 5000.0
        return s

    def test_own_callbacks_are_governed(self):
        for call_super in (False, True):
            calls = {"n": 0}
            def fetch(a): calls["n"] += 1; return policy(0.15)
            s = self.build(call_super, fetch)
            # 2 x 4000 stake x 5 = 40k notional; room = 15k - 5k = 10k -> stake 2000
            self.assertAlmostEqual(stake(s, 4000, leverage=5.0), 2000.0, msg=str(call_super))
            trade = types.SimpleNamespace(pair="BTC/USDT:USDT", leverage=5.0)
            out = s.adjust_trade_position(trade, None, 1.0, 0.0, 10.0, 1e9, 1.0, 1.0, 0.0, 0.0)
            self.assertEqual(out[1], "dca"); self.assertAlmostEqual(out[0], 2000.0)
            self.assertFalse(confirm(s, 20000))      # 20k notional > 10k room
            self.assertTrue(confirm(s, 9000))

    def test_blocked_with_own_callbacks(self):
        s = self.build(False, lambda a: policy(0.15, blocked=["NEW_TRADES"]))
        self.assertEqual(stake(s, 4000), 0.0)
        trade = types.SimpleNamespace(pair="BTC/USDT:USDT", leverage=5.0)
        self.assertIsNone(s.adjust_trade_position(trade, None, 1.0, 0.0, 10.0, 1e9, 1.0, 1.0, 0.0, 0.0))
        self.assertFalse(confirm(s, 100))

    def test_positional_arguments(self):
        s = self.build(False, lambda a: policy(0.15))
        out = s.custom_stake_amount("BTC/USDT:USDT", None, 1.0, 4000, 10.0, 1e9, 5.0, None, "long")
        self.assertAlmostEqual(out, 2000.0)


if __name__ == "__main__":
    unittest.main()
