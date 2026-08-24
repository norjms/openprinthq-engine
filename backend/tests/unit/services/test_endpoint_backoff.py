"""Tests for EndpointBackoff — the retry/log-volume gate used by pollers.

The behaviour that matters here is the thing that motivated the class: a printer
switched off on purpose must stop generating traffic and stop generating
ERROR-level log lines, while a printer that blips must not be penalised.
"""

import logging

from backend.app.services.endpoint_backoff import EndpointBackoff


def _bo(**kw) -> EndpointBackoff:
    return EndpointBackoff(name="test endpoint", **kw)


def test_unknown_endpoint_is_always_attempted():
    bo = _bo()
    assert bo.should_attempt("http://cam/snap") is True


def test_first_failures_carry_no_cooldown():
    """A blip costs nothing: the next poll goes out immediately."""
    bo = _bo()
    bo.record_failure("cam", now=100.0)
    assert bo.should_attempt("cam", now=100.0) is True
    bo.record_failure("cam", now=100.0)
    assert bo.should_attempt("cam", now=100.0) is True


def test_sustained_failure_opens_a_widening_cooldown():
    bo = _bo()
    for _ in range(3):
        bo.record_failure("cam", now=100.0)
    # Third failure -> 5s ladder entry.
    assert bo.should_attempt("cam", now=104.9) is False
    assert bo.should_attempt("cam", now=105.0) is True

    bo.record_failure("cam", now=105.0)  # 4th -> 15s
    assert bo.should_attempt("cam", now=119.0) is False
    assert bo.should_attempt("cam", now=120.0) is True


def test_cooldown_saturates_at_the_ladder_ceiling():
    bo = _bo()
    for _ in range(50):
        bo.record_failure("cam", now=0.0)
    assert bo.cooldown_for(50) == 300.0
    assert bo.should_attempt("cam", now=299.0) is False
    assert bo.should_attempt("cam", now=300.0) is True


def test_log_level_drops_off_after_the_loud_window():
    """The point of the class: an off printer stops shouting."""
    bo = _bo(loud_failures=3)
    levels = [bo.record_failure("cam", now=0.0) for _ in range(6)]
    assert levels[:3] == [logging.ERROR, logging.ERROR, logging.ERROR]
    # One transition line explaining the backoff, then silence.
    assert levels[3] == logging.INFO
    assert levels[4:] == [logging.DEBUG, logging.DEBUG]


def test_failure_summary_names_the_endpoint_and_the_new_cadence():
    bo = _bo(loud_failures=1)
    bo.record_failure("cam", now=0.0)
    bo.record_failure("cam", now=0.0)
    summary = bo.failure_summary("cam")
    assert "test endpoint" in summary
    assert "2 times in a row" in summary


def test_success_clears_state_and_reports_recovery_only_when_quiet():
    bo = _bo(loud_failures=2)
    bo.record_failure("cam", now=0.0)
    # Never went quiet, so recovery is unremarkable and should not be announced.
    assert bo.record_success("cam") is False
    assert bo.should_attempt("cam", now=0.0) is True

    for _ in range(4):
        bo.record_failure("cam", now=0.0)
    assert bo.record_success("cam") is True
    # And the slate is genuinely clean, not merely quiet.
    assert bo.should_attempt("cam", now=0.0) is True


def test_endpoints_are_tracked_independently():
    bo = _bo()
    for _ in range(5):
        bo.record_failure("dead-cam", now=0.0)
    assert bo.should_attempt("dead-cam", now=1.0) is False
    assert bo.should_attempt("live-cam", now=1.0) is True


def test_reset_forgets_one_or_all():
    bo = _bo()
    for _ in range(5):
        bo.record_failure("a", now=0.0)
        bo.record_failure("b", now=0.0)
    bo.reset("a")
    assert bo.should_attempt("a", now=0.0) is True
    assert bo.should_attempt("b", now=0.0) is False
    bo.reset()
    assert bo.should_attempt("b", now=0.0) is True
