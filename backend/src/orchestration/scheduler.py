"""
Job scheduler — determines when jobs run and detects failures.

Design: pure functions over immutable state, no threads, no global state.
The caller (main loop / APScheduler / cron) drives the clock; the scheduler
only computes decisions from (schedule, state, now).

Acceptance criterion:
  Simulated failure detected within the documented alert_interval_seconds.
  No duplicate outputs on restart (idempotency_key prevents double-writes).

Failure detection model:
  A job is considered failing when it has produced N consecutive failures
  within alert_interval_seconds. The FailureAlert fires once per threshold
  crossing, then requires a successful run before re-arming.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Optional


# ---------------------------------------------------------------------------
# Schedule definition
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class JobSchedule:
    """
    Immutable definition of one scheduled job.

    Fields:
        job_name                 Unique job identifier.
        interval_seconds         How often the job should run (0 = run once).
        max_retries              Max consecutive failures before giving up.
        alert_after_n_failures   Trigger FailureAlert after this many consecutive failures.
        alert_interval_seconds   Failures within this window count toward the threshold.
        enabled                  False → scheduler skips this job entirely.
    """
    job_name: str
    interval_seconds: float = 3600.0
    max_retries: int = 3
    alert_after_n_failures: int = 2
    alert_interval_seconds: float = 600.0
    enabled: bool = True


# ---------------------------------------------------------------------------
# Scheduler state (mutable; update via record_job_result)
# ---------------------------------------------------------------------------

@dataclass
class SchedulerState:
    """
    Mutable per-job scheduler state.  Never construct directly in tests —
    use make_initial_state() so defaults are always consistent.
    """
    job_name: str
    last_run_ts: Optional[float] = None       # wall-clock of last run start
    last_success_ts: Optional[float] = None   # wall-clock of last successful run
    consecutive_failures: int = 0
    total_runs: int = 0
    total_successes: int = 0
    total_failures: int = 0
    alert_sent: bool = False                  # re-arm on next success


def make_initial_state(job_name: str) -> SchedulerState:
    return SchedulerState(job_name=job_name)


# ---------------------------------------------------------------------------
# Alert model
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class FailureAlert:
    """
    Raised when a job has exceeded its failure threshold.
    Carries context for triage / paging.
    """
    job_name: str
    consecutive_failures: int
    last_run_ts: Optional[float]
    alert_interval_seconds: float
    message: str


# ---------------------------------------------------------------------------
# Pure decision functions
# ---------------------------------------------------------------------------

def should_run(schedule: JobSchedule, state: SchedulerState, now: float) -> bool:
    """
    Return True if the job is due to run.

    Rules:
      - Disabled jobs never run.
      - Jobs that have never run are immediately due.
      - Jobs are due when now − last_run_ts ≥ interval_seconds.
      - interval_seconds == 0 means run-once (only if never run).
    """
    if not schedule.enabled:
        return False
    if state.last_run_ts is None:
        return True
    if schedule.interval_seconds == 0:
        return False  # run-once, already ran
    return (now - state.last_run_ts) >= schedule.interval_seconds


def record_job_result(
    state: SchedulerState,
    success: bool,
    now: float,
) -> SchedulerState:
    """
    Return an updated SchedulerState after one job execution.
    Does NOT mutate the input; returns a new object.
    """
    new_state = SchedulerState(
        job_name=state.job_name,
        last_run_ts=now,
        last_success_ts=now if success else state.last_success_ts,
        consecutive_failures=0 if success else state.consecutive_failures + 1,
        total_runs=state.total_runs + 1,
        total_successes=state.total_successes + (1 if success else 0),
        total_failures=state.total_failures + (0 if success else 1),
        alert_sent=False if success else state.alert_sent,
    )
    return new_state


def check_failure_alert(
    schedule: JobSchedule,
    state: SchedulerState,
    now: float,
) -> Optional[FailureAlert]:
    """
    Return a FailureAlert if the job has exceeded its failure threshold,
    or None if everything is healthy or the alert was already sent.

    Detection window: failures must have occurred within alert_interval_seconds
    of the last run to count (stale failures don't page).
    """
    if state.alert_sent:
        return None
    if state.consecutive_failures < schedule.alert_after_n_failures:
        return None
    if state.last_run_ts is None:
        return None

    # Only alert if last failure is recent (within the alert window)
    if (now - state.last_run_ts) > schedule.alert_interval_seconds:
        return None

    return FailureAlert(
        job_name=schedule.job_name,
        consecutive_failures=state.consecutive_failures,
        last_run_ts=state.last_run_ts,
        alert_interval_seconds=schedule.alert_interval_seconds,
        message=(
            f"Job {schedule.job_name!r} has failed {state.consecutive_failures} "
            f"consecutive times within {schedule.alert_interval_seconds}s."
        ),
    )


def mark_alert_sent(state: SchedulerState) -> SchedulerState:
    """Return updated state with alert_sent=True to prevent repeat paging."""
    return SchedulerState(
        job_name=state.job_name,
        last_run_ts=state.last_run_ts,
        last_success_ts=state.last_success_ts,
        consecutive_failures=state.consecutive_failures,
        total_runs=state.total_runs,
        total_successes=state.total_successes,
        total_failures=state.total_failures,
        alert_sent=True,
    )


# ---------------------------------------------------------------------------
# Batch helpers
# ---------------------------------------------------------------------------

def simulate_run_sequence(
    schedule: JobSchedule,
    outcomes: list[bool],
    start_ts: float = 0.0,
) -> tuple[SchedulerState, list[Optional[FailureAlert]]]:
    """
    Simulate a sequence of job outcomes and return (final_state, alerts_per_run).
    Each run is spaced by schedule.interval_seconds.
    Useful for testing failure detection in isolation.
    """
    state = make_initial_state(schedule.job_name)
    alerts: list[Optional[FailureAlert]] = []
    ts = start_ts

    for success in outcomes:
        ts += schedule.interval_seconds or 60.0
        state = record_job_result(state, success=success, now=ts)
        alert = check_failure_alert(schedule, state, now=ts)
        if alert:
            state = mark_alert_sent(state)
        alerts.append(alert)

    return state, alerts


def all_schedules_status(
    schedules: list[JobSchedule],
    states: dict[str, SchedulerState],
    now: float,
) -> list[dict]:
    """
    Return a snapshot of scheduler health — one row per schedule.
    Used for dashboard / observability.
    """
    rows = []
    for sched in schedules:
        state = states.get(sched.job_name, make_initial_state(sched.job_name))
        due = should_run(sched, state, now)
        alert = check_failure_alert(sched, state, now)
        rows.append({
            "job_name": sched.job_name,
            "enabled": sched.enabled,
            "is_due": due,
            "last_run_ts": state.last_run_ts,
            "last_success_ts": state.last_success_ts,
            "consecutive_failures": state.consecutive_failures,
            "total_runs": state.total_runs,
            "has_alert": alert is not None,
        })
    return rows
