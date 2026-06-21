"""Thread-safe stdout for Lead streaming + background teammate logs."""

from __future__ import annotations

import sys
import threading
from contextlib import contextmanager
from typing import Iterator

_stdout_lock = threading.Lock()


@contextmanager
def stdout_locked() -> Iterator[None]:
    _stdout_lock.acquire()
    try:
        yield
    finally:
        _stdout_lock.release()


def locked_print(*args, **kwargs) -> None:
    with stdout_locked():
        print(*args, **kwargs)


def locked_stdout_write(text: str) -> None:
    with stdout_locked():
        sys.stdout.write(text)
        sys.stdout.flush()
