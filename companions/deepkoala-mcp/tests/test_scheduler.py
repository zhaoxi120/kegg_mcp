"""Tests for the independent bounded DeepKOALA scheduler state."""

import pytest

from deepkoala_mcp.scheduler import JobScheduler


def test_scheduler_is_fifo_bounded_and_single_runner() -> None:
    scheduler = JobScheduler(2)
    scheduler.enqueue("job-a")
    scheduler.enqueue("job-b")

    assert scheduler.is_full
    with pytest.raises(RuntimeError, match="full"):
        scheduler.enqueue("job-c")

    assert scheduler.start_next() == "job-a"
    assert scheduler.start_next() is None
    assert scheduler.running_job_id == "job-a"
    scheduler.finish("job-a")
    assert scheduler.start_next() == "job-b"


def test_scheduler_removal_and_clear_leave_no_owned_state() -> None:
    scheduler = JobScheduler(2)
    scheduler.enqueue("job-a")
    scheduler.enqueue("job-b")
    scheduler.remove("job-a")

    assert scheduler.start_next() == "job-b"
    scheduler.clear()

    assert scheduler.queued_count == 0
    assert scheduler.running_job_id is None
