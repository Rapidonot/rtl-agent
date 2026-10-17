"""Cooperative cancellation and a shared wall-clock budget for local web runs."""

import threading
import time


class RunStopped(RuntimeError):
    pass


class RunControl:
    def __init__(self, seconds: float):
        self.deadline = time.monotonic() + seconds
        self.cancelled = threading.Event()

    def check(self) -> None:
        if self.cancelled.is_set():
            raise RunStopped("Stopped by user. Partial results are preserved.")
        if time.monotonic() >= self.deadline:
            raise RunStopped("Run wall-clock timeout reached. Partial results are preserved.")

    def remaining(self, maximum: float) -> float:
        self.check()
        return min(maximum, max(0.001, self.deadline - time.monotonic()))
