"""Local enforcement of RiskState policies.

The API answers "how much exposure is allowed". This module turns that answer into
a decision the bot applies before it sends an order, and defines what happens when
the answer is missing, stale or malformed.

Standard library only.
"""

from __future__ import annotations

import json
import math
import os
import threading
import time
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass, field
from typing import Any, Callable, Dict, Optional

SUPPORTED_ASSETS = ("BTC", "ETH")
_WRAPPED = {"WBTC": "BTC", "CBBTC": "BTC", "XBT": "BTC", "WETH": "ETH", "STETH": "ETH", "WSTETH": "ETH"}


def base_asset(pair: str) -> Optional[str]:
    """'BTC/USDT' or 'ETH/USDT:USDT' -> 'BTC' / 'ETH'. None when RiskState does not cover it."""
    base = pair.split("/")[0].split(":")[0].strip().upper()
    base = _WRAPPED.get(base, base)
    return base if base in SUPPORTED_ASSETS else None


@dataclass
class GuardConfig:
    api_key: str = ""
    api_url: str = "https://api.riskstate.ai"
    # "enforce": decisions change orders. "shadow": decisions are logged, orders are untouched.
    mode: str = "enforce"
    # What to do when no usable policy exists (API down, expired permit, bad payload).
    # "closed": no new entries. "open": fall back to `local_max_fraction` only.
    fail_mode: str = "closed"
    # A policy older than this is not used, whatever the API said about its TTL.
    max_policy_age_s: float = 900.0
    # Re-fetch after this long even if the API's ttl_seconds is longer.
    refresh_s: float = 300.0
    request_timeout_s: float = 10.0
    # After a failed fetch, wait this long before calling the API again.
    retry_s: float = 30.0
    # Hard local ceiling on exposure per asset, as a fraction of the portfolio.
    # Applied on top of RiskState and still applied when RiskState is unreachable.
    local_max_fraction: float = 1.0
    # Pairs RiskState does not cover: "allow" (not governed) or "block".
    unsupported_pairs: str = "allow"
    # JSON-lines file with every decision. Empty string disables it.
    log_path: str = ""

    def validate(self) -> None:
        if self.mode not in ("enforce", "shadow"):
            raise ValueError("mode must be 'enforce' or 'shadow'")
        if self.fail_mode not in ("closed", "open"):
            raise ValueError("fail_mode must be 'closed' or 'open'")
        if self.unsupported_pairs not in ("allow", "block"):
            raise ValueError("unsupported_pairs must be 'allow' or 'block'")
        if not (0.0 <= self.local_max_fraction <= 1.0):
            raise ValueError("local_max_fraction must be between 0 and 1")


@dataclass
class Decision:
    """What the guard concluded for one proposed entry."""

    pair: str
    asset: Optional[str]
    allowed: bool
    # Largest notional (stake x leverage) the entry may add, in stake currency.
    max_notional: float
    # Notional after the cap. Equals the proposed notional when nothing binds.
    notional: float
    proposed_notional: float
    reason: str
    governed: bool = True
    policy_level: Optional[int] = None
    max_size_fraction: Optional[float] = None
    policy_age_s: Optional[float] = None
    policy_hash: Optional[str] = None
    binding: Optional[str] = None
    mode: str = "enforce"
    ts: float = field(default_factory=time.time)
    # False when this repeats the previous decision for the pair (not written to the log again).
    changed: bool = True

    @property
    def resized(self) -> bool:
        return self.allowed and self.notional < self.proposed_notional - 1e-12


class _Policy:
    __slots__ = ("asset", "fetched_at", "ttl", "max_size", "blocked", "level", "hash", "binding")

    def __init__(self, asset: str, payload: Dict[str, Any], now: float):
        ep = payload.get("exposure_policy")
        if not isinstance(ep, dict):
            raise ValueError("missing exposure_policy")
        size = ep.get("max_size_fraction")
        if isinstance(size, bool) or not isinstance(size, (int, float)) or not math.isfinite(size):
            raise ValueError("max_size_fraction is not a finite number")
        blocked = ep.get("blocked_actions")
        if not isinstance(blocked, list):
            raise ValueError("blocked_actions is not a list")
        if payload.get("asset") not in (None, asset):
            raise ValueError("policy is for a different asset")
        self.asset = asset
        self.fetched_at = now
        ttl = payload.get("ttl_seconds")
        self.ttl = float(ttl) if isinstance(ttl, (int, float)) and ttl > 0 else 60.0
        self.max_size = max(0.0, min(1.0, float(size)))
        self.blocked = [str(b) for b in blocked]
        self.level = payload.get("policy_level") if isinstance(payload.get("policy_level"), int) else None
        self.hash = payload.get("policy_hash") if isinstance(payload.get("policy_hash"), str) else None
        bc = payload.get("binding_constraint")
        self.binding = bc.get("source") if isinstance(bc, dict) else None


def _http_fetch(url: str, api_key: str, asset: str, timeout: float) -> Dict[str, Any]:
    req = urllib.request.Request(
        url.rstrip("/") + "/v1/risk-state",
        data=json.dumps({"asset": asset}).encode(),
        headers={
            "Authorization": "Bearer " + api_key,
            "Content-Type": "application/json",
            "User-Agent": "riskstate-guard/0.1",
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 (https URL from config)
        return json.loads(resp.read().decode())


class RiskStateGuard:
    """Fetches, caches and applies RiskState policies.

    `fetch` can be replaced (tests, or a different transport); it receives the asset
    and returns the decoded `/v1/risk-state` response.
    """

    def __init__(
        self,
        config: Optional[GuardConfig] = None,
        fetch: Optional[Callable[[str], Dict[str, Any]]] = None,
        clock: Callable[[], float] = time.time,
    ):
        self.config = config or GuardConfig(api_key=os.environ.get("RISKSTATE_API_KEY", ""))
        self.config.validate()
        self._clock = clock
        self._fetch = fetch or (
            lambda asset: _http_fetch(self.config.api_url, self.config.api_key, asset, self.config.request_timeout_s)
        )
        self._cache: Dict[str, _Policy] = {}
        self._last_error: Dict[str, str] = {}
        self._failed_at: Dict[str, float] = {}
        self._last_logged: Dict[str, tuple] = {}
        self._lock = threading.Lock()

    # ── policy ────────────────────────────────────────────────────────────────
    def _policy(self, asset: str) -> Optional[_Policy]:
        now = self._clock()
        with self._lock:
            cached = self._cache.get(asset)
            fresh_for = min(cached.ttl, self.config.refresh_s) if cached else 0.0
            if cached and now - cached.fetched_at < fresh_for:
                return cached
            failed = self._failed_at.get(asset)
            if failed is None or now - failed >= self.config.retry_s:
                try:
                    policy = _Policy(asset, self._fetch(asset), now)
                    self._cache[asset] = policy
                    self._last_error.pop(asset, None)
                    self._failed_at.pop(asset, None)
                    return policy
                except Exception as exc:  # network, HTTP status, JSON, schema: all mean "no new policy"
                    self._last_error[asset] = type(exc).__name__
                    self._failed_at[asset] = now
            # The refresh failed. An older policy is still a permit until it expires.
            if cached and now - cached.fetched_at <= self.config.max_policy_age_s:
                return cached
            return None

    # ── decision ──────────────────────────────────────────────────────────────
    def check_entry(
        self,
        pair: str,
        proposed_notional: float,
        portfolio_value: float,
        open_notional: float = 0.0,
    ) -> Decision:
        """Decide a new entry.

        proposed_notional  stake x leverage of the entry, in stake currency
        portfolio_value    total portfolio value, in stake currency
        open_notional      notional already open in the same asset (all pairs of that asset)
        """
        cfg = self.config
        asset = base_asset(pair)
        d = Decision(pair=pair, asset=asset, allowed=True, max_notional=proposed_notional,
                     notional=proposed_notional, proposed_notional=proposed_notional, reason="OK", mode=cfg.mode)

        numbers_ok = all(
            isinstance(x, (int, float)) and not isinstance(x, bool) and math.isfinite(x) and x >= 0
            for x in (proposed_notional, portfolio_value, open_notional)
        )
        if not numbers_ok:
            d.allowed, d.max_notional, d.notional, d.reason = False, 0.0, 0.0, "INVALID_INPUT"
            return self._finish(d)

        local_room = max(0.0, cfg.local_max_fraction * portfolio_value - open_notional)

        if asset is None:
            d.governed = False
            if cfg.unsupported_pairs == "block":
                d.allowed, d.max_notional, d.notional, d.reason = False, 0.0, 0.0, "UNSUPPORTED_PAIR_BLOCKED"
            else:
                d.reason = "UNSUPPORTED_PAIR_NOT_GOVERNED"
            return self._finish(d)

        policy = self._policy(asset)
        if policy is None:
            err = self._last_error.get(asset, "NoPolicy")
            if cfg.fail_mode == "closed":
                d.allowed, d.max_notional, d.notional = False, 0.0, 0.0
                d.reason = "NO_POLICY_FAIL_CLOSED:" + err
                return self._finish(d)
            room, reason_ok, reason_cap = local_room, "NO_POLICY_FAIL_OPEN:" + err, "NO_POLICY_LOCAL_CAP:" + err
        else:
            d.policy_level, d.max_size_fraction = policy.level, policy.max_size
            d.policy_hash, d.binding = policy.hash, policy.binding
            d.policy_age_s = round(self._clock() - policy.fetched_at, 1)
            if "NEW_TRADES" in policy.blocked:
                d.allowed, d.max_notional, d.notional, d.reason = False, 0.0, 0.0, "NEW_TRADES_BLOCKED"
                return self._finish(d)
            engine_room = max(0.0, policy.max_size * portfolio_value - open_notional)
            room = min(engine_room, local_room)
            reason_ok = "OK"
            reason_cap = "SIZE_CAP" if engine_room <= local_room else "LOCAL_CAP"

        d.max_notional = room
        if room <= 0:
            d.allowed, d.notional = False, 0.0
            d.reason = reason_cap + "_NO_ROOM"
        elif proposed_notional > room:
            d.notional, d.reason = room, reason_cap
        else:
            d.reason = reason_ok
        return self._finish(d)

    def _finish(self, d: Decision) -> Decision:
        # A bot polls the same decision every few seconds; record it when it changes.
        key = (d.allowed, d.reason, round(d.notional, 2), round(d.proposed_notional, 2), d.policy_hash)
        d.changed = self._last_logged.get(d.pair) != key
        self._last_logged[d.pair] = key
        path = self.config.log_path
        if path and d.changed:
            try:
                with open(path, "a", encoding="utf-8") as fh:
                    fh.write(json.dumps(asdict(d), separators=(",", ":")) + "\n")
            except OSError:
                pass  # logging must never decide a trade
        return d
