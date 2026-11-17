# riskstate-guard

Apply [RiskState](https://riskstate.ai) risk permissions inside your bot, before the order is sent.

RiskState answers one question for BTC and ETH: how much exposure is allowed right now. This package
turns that answer into a decision your bot enforces, and defines what happens when the answer is
missing. It ships a mixin for [Freqtrade](https://www.freqtrade.io) and a plain Python class for
anything else.

It does not generate signals and it never places, changes or closes an order.

## What it enforces

| Situation | Result |
|---|---|
| Entry fits inside `max_size_fraction` for the asset | Passes untouched |
| Entry would take the asset over the limit | Stake is cut to the room that is left |
| No room left, or `blocked_actions` contains `NEW_TRADES` | Entry is refused |
| API unreachable, key rejected, or malformed response | Refused (`fail_mode: "closed"`, the default) |
| Last good policy is older than `max_policy_age_s` (15 min) | Treated as no policy |
| Pair is not BTC or ETH | Not governed (or refused with `unsupported_pairs: "block"`) |

The limit is on **notional per asset**: stake × leverage, summed over every open trade in that asset.
A 5x futures entry uses five times its stake.

## Freqtrade

```bash
pip install riskstate-guard
# or straight from the repository
pip install git+https://github.com/Riskstate/riskstate-guard
export RISKSTATE_API_KEY=rs_live_...        # free key at https://riskstate.ai
```

```python
from freqtrade.strategy import IStrategy
from riskstate_guard.freqtrade import RiskStateMixin

class MyStrategy(RiskStateMixin, IStrategy):     # mixin first
    riskstate = {"mode": "shadow"}
    ...
```

That is the whole integration. A full strategy is in [`examples/`](examples/RiskStateSampleStrategy.py).
Documentation: [riskstate.ai/docs/freqtrade](https://riskstate.ai/docs/freqtrade).

The mixin hooks three callbacks:

- `custom_stake_amount` caps the stake of a new trade.
- `confirm_trade_entry` checks again right before the order goes out.
- `adjust_trade_position` caps or drops position increases (DCA). Reductions always pass.

Your strategy can define any of these itself. The guard is applied to whatever they return, so you
do not need to call `super()`, and forgetting it cannot leave an entry ungoverned.

### Start in shadow mode

With `mode: "shadow"` the guard decides and logs, and your orders are not touched. Set `log_path` and
you get one JSON line each time a decision changes:

```json
{"pair":"BTC/USDT","asset":"BTC","allowed":true,"max_notional":15660.0,"notional":15660.0,
 "proposed_notional":50000,"reason":"SIZE_CAP","policy_level":2,"max_size_fraction":0.1566,
 "policy_age_s":0.0,"policy_hash":"3b9d2ed2…","binding":"MACRO","mode":"shadow","ts":1794900000.0}
```

Run it for a few weeks next to your strategy, compare, then switch to `"enforce"`.

### Backtesting

The guard is inactive in backtesting and hyperopt. The API serves the current policy, not the policy
of a past date, so applying it to historical candles would be wrong. Your backtest results are those
of the ungoverned strategy.

## Options

| Option | Default | Meaning |
|---|---|---|
| `mode` | `"enforce"` | `"shadow"` logs decisions without changing orders |
| `fail_mode` | `"closed"` | `"open"` falls back to `local_max_fraction` when there is no policy |
| `local_max_fraction` | `1.0` | Your own ceiling per asset. Applies always, including when RiskState is down |
| `max_policy_age_s` | `900` | A cached policy older than this is not used |
| `refresh_s` | `300` | Re-fetch at least this often |
| `request_timeout_s` | `10` | HTTP timeout |
| `retry_s` | `30` | After a failed fetch, wait this long before calling the API again |
| `unsupported_pairs` | `"allow"` | `"block"` refuses pairs RiskState does not cover |
| `log_path` | `""` | JSON-lines decision log |
| `api_key` | `$RISKSTATE_API_KEY` | |
| `api_url` | `https://api.riskstate.ai` | |

## Without Freqtrade

```python
from riskstate_guard import RiskStateGuard, GuardConfig

guard = RiskStateGuard(GuardConfig(api_key="rs_live_...", local_max_fraction=0.5))

d = guard.check_entry("ETH/USDT", proposed_notional=20_000, portfolio_value=100_000, open_notional=5_000)
if d.allowed:
    place_order(notional=d.notional)      # already capped
else:
    log(d.reason)                         # e.g. NEW_TRADES_BLOCKED, SIZE_CAP_NO_ROOM
```

## What is sent

Each policy fetch sends your API key and the asset symbol (`BTC` or `ETH`) to `api.riskstate.ai`.
Pairs, sizes, balances and positions stay on your machine. Policies are cached, so the call rate is
at most one per asset every 60 seconds.

## Limits

- BTC and ETH only.
- The guard governs entries and position increases. Stops, exits and liquidation risk are yours.
- Portfolio value is what Freqtrade reports as total stake (`wallets.get_total_stake_amount()`),
  so `tradable_balance_ratio` and `available_capital` shrink the base the fraction applies to.
- A callback that raises is read by Freqtrade as approval. The mixin catches its own errors and
  refuses; errors raised in your own callbacks are still yours to handle.

## Tests

```bash
python -m unittest tests.test_guard
```

No dependencies beyond the Python standard library.

Also run end to end in Freqtrade 2026.7 dry-run on Binance (BTC/ETH/SOL): spot in enforce, shadow and
rejected-key runs, and futures at 5x with position increases.

## License

MIT
