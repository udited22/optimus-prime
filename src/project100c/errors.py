"""Typed error hierarchy. Every failure path in Project 100C raises one of these (no silent failures)."""

from __future__ import annotations


class Project100CError(Exception):
    """Base class for all Project 100C errors."""


class ConfigError(Project100CError):
    """A configuration file is missing, malformed, internally inconsistent, or unverified."""


class UnverifiedConfigError(ConfigError):
    """A config entry is marked unverified and the caller did not explicitly opt in to using it."""


class CostModelError(Project100CError):
    """The cost model cannot price the request (bad input, no schedule for the date, missing plan field)."""


class NoScheduleForDateError(CostModelError):
    """No charge schedule covers the requested trade date."""


class SessionError(Project100CError):
    """Session/trading-window config or query error (e.g. naive datetime, no version for a date)."""


class InstrumentMasterError(Project100CError):
    """Instrument file could not be parsed or fails integrity checks."""


class DataQualityInputError(Project100CError):
    """A data-quality check was called with structurally invalid input (not a data defect: a caller bug)."""


class SpecValidationError(Project100CError):
    """A StrategySpec failed validation."""


class RegistryError(Project100CError):
    """Experiment-registry violation (tamper, illegal mutation, unknown run)."""


class KernelInvariantError(Project100CError):
    """Kernel state violates an invariant (e.g. a net short option position exists). Maps to SYSTEM_INTEGRITY_KILL."""


class TicketRejectedError(KernelInvariantError):
    """The gateway refused an entry: no RiskTicket, or one that is forged, expired or for another order (K-07)."""


class StaleInstrumentMasterError(InstrumentMasterError):
    """The instrument file contains contracts that expired before its as-of date (stale file)."""


class LotSizeAmbiguityError(InstrumentMasterError):
    """More than one lot size is live for the requested underlying/expiry; caller must be specific."""


class LotSizeHistoryError(InstrumentMasterError):
    """A lot size was requested outside the sourced lot-size history (never extrapolated)."""


class CrossSourceMismatchError(InstrumentMasterError):
    """Two instrument sources disagree on a contract attribute."""


class CalendarError(Project100CError):
    """Trading-calendar config or query error."""


class CalendarCoverageError(CalendarError):
    """The requested date is outside the years the holiday calendar covers (never assume 'no holidays')."""


class JournalError(Project100CError):
    """Journal write/read failure or tamper detection. A write failure maps to SYSTEM_INTEGRITY_KILL."""


class BrokerError(Project100CError):
    """Broker adapter / fake broker error (typed subclasses below)."""


class BrokerDisconnectedError(BrokerError):
    """The broker connection is down; the request was NOT sent."""


class BrokerTimeoutError(BrokerError):
    """The request may or may not have reached the broker (state UNKNOWN until reconciled)."""


class BrokerRejectError(BrokerError):
    """The broker rejected the request synchronously."""


class GovernorError(Project100CError):
    """The Risk Governor was called with structurally invalid input (a caller bug, not a rejection)."""


class RecorderError(Project100CError):
    """Tick recorder could not persist or read data (never silently dropped)."""


class LakeError(Project100CError):
    """Data-lake write/read failure: raw immutability violated, hash mismatch, unreadable part."""


class DataSourceError(Project100CError):
    """Historical/market-data vendor failure (typed subclasses below). Never swallowed."""


class MissingCredentialError(DataSourceError):
    """A required credential environment variable is absent or malformed. Nothing was sent to the vendor."""


class TransportError(DataSourceError):
    """Network-level failure (DNS, TLS, timeout, connection reset): the request may not have reached the vendor."""


class EndpointNotAllowedError(DataSourceError):
    """Code attempted to call a vendor endpoint outside the data-only allowlist (a programming error)."""


class VendorAPIError(DataSourceError):
    """The vendor answered with an error. ``code`` is the vendor error code (e.g. DH-904, 806) when present."""

    def __init__(self, message: str, *, http_status: int, code: str | None) -> None:
        super().__init__(message)
        self.http_status = http_status
        self.code = code


class VendorAuthError(VendorAPIError):
    """Access token missing/invalid/expired at the vendor (e.g. DH-901, 807-810). Stops the job."""


class VendorSubscriptionError(VendorAPIError):
    """The data plan is not active (e.g. DH-902, 806). Stops the job."""


class VendorRequestError(VendorAPIError):
    """The vendor rejected the request parameters (e.g. DH-905, 811-814). Not retried."""


class VendorRateLimitError(VendorAPIError):
    """Rate limit still breached after all backoff attempts (HTTP 429, DH-904, 805)."""


class VendorServerError(VendorAPIError):
    """Vendor-side failure still present after retries (HTTP 5xx, DH-908/909, 800)."""


class VendorResponseError(DataSourceError):
    """The vendor response is structurally invalid (bad JSON, ragged arrays, wrong types)."""


class QuotaExhaustedError(DataSourceError):
    """Our own daily request budget (below the vendor's) is used up. The job stops and resumes next day."""


class DownloadJobError(DataSourceError):
    """Download job store problem (spec mismatch on resume, corrupt state, unknown job)."""


class DownloadIncompleteError(DataSourceError):
    """A download run finished with FAILED chunks; the summary lists them. Re-run to retry."""


class BacktestError(Project100CError):
    """Backtest engine misuse or invariant violation (clock going backwards, bad order spec, unknown order)."""


class LookAheadError(BacktestError):
    """A strategy or feed tried to use information not yet available at the simulated decision time."""


class SpreadModelError(BacktestError):
    """Spread model misuse: invalid parameters, a bar outside every time band, or missing contract context."""


class BacktestDataError(BacktestError):
    """Backtest input data unusable: mixed data versions, conflicting duplicate bars, unknown contract terms."""


class EconomicsError(Project100CError):
    """Whole-system economics misuse: non-flat journal day, missing NAV, inconsistent months or bad inputs."""


class ChecksumMismatchError(VendorResponseError):
    """A downloaded archive file does not match its published checksum (e.g. a Binance .CHECKSUM sidecar)."""
