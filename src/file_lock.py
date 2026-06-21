"""Advisory file locks (fcntl) shared by mailbox, tasks, etc."""

from __future__ import annotations

import random
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

LOCK_RETRIES = 10
LOCK_MIN_TIMEOUT_MS = 5
LOCK_MAX_TIMEOUT_MS = 100


@contextmanager
def file_lock(lock_path: Path) -> Iterator[None]:
    import fcntl

    lock_path.parent.mkdir(parents=True, exist_ok=True)
    last_err: OSError | None = None
    for attempt in range(LOCK_RETRIES):
        fh = lock_path.open("a+")
        acquired = False
        try:
            try:
                fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                acquired = True
            except BlockingIOError as e:
                last_err = e
                fh.close()
                delay_ms = min(
                    LOCK_MAX_TIMEOUT_MS,
                    LOCK_MIN_TIMEOUT_MS * (2 ** attempt) + random.randint(0, 10),
                )
                time.sleep(delay_ms / 1000.0)
                continue
            yield
            return
        finally:
            if acquired:
                try:
                    fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
                except OSError:
                    pass
                fh.close()
    raise TimeoutError(
        f"Could not acquire lock {lock_path} after {LOCK_RETRIES} retries"
    ) from last_err
