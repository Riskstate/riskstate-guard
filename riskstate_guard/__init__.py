"""RiskState guard: enforce RiskState pre-trade risk permissions locally.

`RiskStateGuard` is framework-agnostic. `riskstate_guard.freqtrade.RiskStateMixin`
wires it into a Freqtrade strategy.
"""

from .guard import Decision, GuardConfig, RiskStateGuard, base_asset

__all__ = ["Decision", "GuardConfig", "RiskStateGuard", "base_asset"]
__version__ = "0.1.0"
