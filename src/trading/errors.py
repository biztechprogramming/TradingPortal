"""Typed exceptions.

Every error the system can raise is one of these, so callers can decide
whether a failure is *safe to retry*, *safe to abort*, or *requires human
intervention because money may already be at risk*.
"""

from __future__ import annotations


class TradingError(Exception):
    """Base class. ``retryable`` and ``position_at_risk`` drive recovery."""

    retryable = False
    #: True when the failure happened after an order may have reached the
    #: broker, meaning local state cannot be trusted without reconciliation.
    position_at_risk = False


# --- configuration / startup -------------------------------------------------


class ConfigError(TradingError):
    """Invalid or missing configuration. Raised before anything trades."""


class LiveTradingDisabled(TradingError):
    """A live order was attempted without the explicit live-trading opt-in."""


# --- connectivity ------------------------------------------------------------


class BrokerConnectionError(TradingError):
    retryable = True


class BrokerAuthError(TradingError):
    """Credentials missing, expired, or rejected."""


class BrokerTimeout(BrokerConnectionError):
    """Request exceeded its deadline. May or may not have been applied."""

    position_at_risk = True


class RateLimited(BrokerConnectionError):
    def __init__(self, message: str, retry_after: float | None = None) -> None:
        super().__init__(message)
        self.retry_after = retry_after


# --- market data -------------------------------------------------------------


class QuoteError(TradingError):
    """Base for anything wrong with the quote we were given."""


class StaleQuoteError(QuoteError):
    pass


class InvalidQuoteError(QuoteError):
    """Missing/zero/crossed bid-ask, or non-numeric fields."""


class WideSpreadError(QuoteError):
    """Bid-ask spread exceeds the configured maximum."""


class HaltedSecurityError(QuoteError):
    pass


# --- order lifecycle ---------------------------------------------------------


class OrderError(TradingError):
    pass


class OrderRejected(OrderError):
    pass


class OrderValidationError(OrderError):
    """Failed pre-submit validation. Nothing was sent to the broker."""


class MaxSlippageExceeded(OrderError):
    """Market moved past the hard guard. We abort rather than chase."""


class OrderTimeout(OrderError):
    position_at_risk = True


class InsufficientFunds(OrderError):
    pass


# --- safety / concurrency ----------------------------------------------------


class ExecutionLocked(TradingError):
    """Another command holds the execution lock."""


class DuplicateCommand(TradingError):
    """Same command re-issued inside the duplicate-suppression window."""


class ConfirmationRequired(TradingError):
    """Live LONG/SHORT needs a deliberate confirmation token."""

    def __init__(self, message: str, token: str, expires_at: float) -> None:
        super().__init__(message)
        self.token = token
        self.expires_at = expires_at


class UnreconciledState(TradingError):
    """A previous run left brokers in disagreement; needs acknowledgement."""


class MarketClosed(TradingError):
    pass


class SafetyLimitExceeded(TradingError):
    """A configured hard cap (daily commands, notional, shares) was hit."""
