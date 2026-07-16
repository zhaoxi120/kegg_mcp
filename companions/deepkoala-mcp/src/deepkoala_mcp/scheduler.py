"""Deterministic single-runner queue state for the DeepKOALA companion."""

from __future__ import annotations

from collections import deque


class JobScheduler:
    """Own a bounded FIFO queue and exactly one running job identifier."""

    def __init__(self, max_queue_size: int) -> None:
        if max_queue_size < 1:
            raise ValueError("max_queue_size must be positive")
        self._max_queue_size = max_queue_size
        self._queue: deque[str] = deque()
        self._running_job_id: str | None = None

    @property
    def queued_count(self) -> int:
        return len(self._queue)

    @property
    def running_job_id(self) -> str | None:
        return self._running_job_id

    @property
    def is_full(self) -> bool:
        return len(self._queue) >= self._max_queue_size

    def enqueue(self, job_id: str) -> None:
        if self.is_full:
            raise RuntimeError("scheduler queue is full")
        if job_id == self._running_job_id or job_id in self._queue:
            raise ValueError("job is already scheduled")
        self._queue.append(job_id)

    def remove(self, job_id: str) -> None:
        self._queue.remove(job_id)

    def start_next(self) -> str | None:
        if self._running_job_id is not None or not self._queue:
            return None
        job_id = self._queue.popleft()
        self._running_job_id = job_id
        return job_id

    def finish(self, job_id: str) -> None:
        if self._running_job_id != job_id:
            raise RuntimeError("job does not own the running scheduler slot")
        self._running_job_id = None

    def clear(self) -> None:
        self._queue.clear()
        self._running_job_id = None


__all__ = ["JobScheduler"]
