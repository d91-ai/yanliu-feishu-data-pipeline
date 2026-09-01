#!/usr/bin/env python3
"""Coordinate readers and writers for one MAS dispatch directory."""

from __future__ import annotations

from contextlib import contextmanager
import fcntl
from pathlib import Path
from typing import Iterator


@contextmanager
def mas_task_lock(task_dir: Path, *, exclusive: bool) -> Iterator[None]:
    task_dir.mkdir(parents=True, exist_ok=True)
    lock_path = task_dir / ".mas-task.lock"
    operation = fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH
    with lock_path.open("a+b") as handle:
        fcntl.flock(handle.fileno(), operation)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
