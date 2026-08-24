"""Per-endpoint failure backoff for pollers that talk to printer hardware.

Background pollers in this app retry unreachable endpoints on a fixed cadence
forever. That is correct for a printer that is briefly rebooting and wrong for
one that is simply switched off: a machine powered down on purpose produces an
ERROR-level line every poll interval, indefinitely, and keeps issuing network
requests nobody wants. On a relayed (connector-tunnelled) deployment those
requests are not free either, since each one occupies the tunnel.

``EndpointBackoff`` is the shared state for "this endpoint keeps failing, stop
trying so often". It is deliberately a pure, synchronous, in-memory bookkeeper:
no I/O, no locks, no background tasks. Callers ask ``should_attempt()`` before
doing work and report the outcome with ``record_success()`` /
``record_failure()``.

Log-noise control lives here too, because "how loud should this failure be" is a
function of how many times it has already happened. ``record_failure`` returns
the level the caller should log at: the first few failures are worth an
ERROR/WARNING, the thousandth identical one is not.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field

# Cooldown applied after N consecutive failures. The first two failures get no
# cooldown at all so a transient blip costs nothing, then the endpoint is backed
# off progressively to a five-minute ceiling. A printer that comes back is
# noticed within five minutes at worst, which is well inside the "I turned it
# on, why is it not showing up" threshold.
DEFAULT_COOLDOWN_LADDER: tuple[float, ...] = (0.0, 0.0, 5.0, 15.0, 30.0, 60.0, 120.0, 300.0)

# Failures logged at full volume before the endpoint is treated as known-bad.
DEFAULT_LOUD_FAILURES = 3


@dataclass
class _EndpointState:
    consecutive_failures: int = 0
    next_attempt_at: float = 0.0
    quiet: bool = False
    suppressed_since: float = 0.0
    suppressed_count: int = 0
    last_error: str = ""


@dataclass
class EndpointBackoff:
    """Tracks consecutive failures per endpoint key and gates retries.

    Args:
        name: Used in log messages to say what is being backed off.
        ladder: Cooldown seconds indexed by consecutive-failure count.
        loud_failures: How many failures are logged at the caller's normal level
            before repeats are demoted to DEBUG.
    """

    name: str = "endpoint"
    ladder: tuple[float, ...] = DEFAULT_COOLDOWN_LADDER
    loud_failures: int = DEFAULT_LOUD_FAILURES
    _states: dict[str, _EndpointState] = field(default_factory=dict)

    def _state(self, key: str) -> _EndpointState:
        state = self._states.get(key)
        if state is None:
            state = _EndpointState()
            self._states[key] = state
        return state

    def should_attempt(self, key: str, now: float | None = None) -> bool:
        """True when this endpoint is due for another attempt.

        A caller acting on a direct user request should skip this check (or pass
        ``force``) — a person clicking "show me the camera" deserves a real
        attempt even when the breaker is open.
        """
        state = self._states.get(key)
        if state is None:
            return True
        return (now if now is not None else time.monotonic()) >= state.next_attempt_at

    def cooldown_for(self, consecutive_failures: int) -> float:
        idx = min(max(consecutive_failures - 1, 0), len(self.ladder) - 1)
        return self.ladder[idx]

    def record_failure(self, key: str, error: str = "", now: float | None = None) -> int:
        """Record a failed attempt and return the logging level to use.

        Returns ``logging.ERROR`` (caller's normal level, which it may downgrade
        to WARNING) for the first ``loud_failures`` failures and for the single
        transition line when the endpoint goes quiet, and ``logging.DEBUG``
        afterwards, so a permanently-off printer stops filling the log.
        """
        now = now if now is not None else time.monotonic()
        state = self._state(key)
        state.consecutive_failures += 1
        state.last_error = error
        state.next_attempt_at = now + self.cooldown_for(state.consecutive_failures)

        if state.consecutive_failures <= self.loud_failures:
            return logging.ERROR

        if not state.quiet:
            # The one line that explains the silence that follows.
            state.quiet = True
            state.suppressed_since = now
            state.suppressed_count = 0
            return logging.INFO

        state.suppressed_count += 1
        return logging.DEBUG

    def failure_summary(self, key: str) -> str:
        """Human-readable tail for the transition-to-quiet log line."""
        state = self._state(key)
        return (
            f"{self.name} failed {state.consecutive_failures} times in a row; "
            f"backing off to one attempt every {self.cooldown_for(state.consecutive_failures):.0f}s "
            f"until it recovers (further failures logged at debug level)"
        )

    def record_success(self, key: str) -> bool:
        """Clear the failure state. Returns True if this was a recovery.

        A True return means the endpoint had gone quiet and is worth one INFO
        line, so the log shows the machine coming back as clearly as it showed
        it going away.
        """
        state = self._states.pop(key, None)
        return bool(state and state.quiet)

    def reset(self, key: str | None = None) -> None:
        """Forget one endpoint's state, or all of it."""
        if key is None:
            self._states.clear()
        else:
            self._states.pop(key, None)
