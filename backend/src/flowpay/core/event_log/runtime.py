# 打开事件存储，并在服务生命周期内持有运行锁。
from __future__ import annotations

import os
from collections.abc import Generator
from contextlib import contextmanager
from typing import TextIO

try:
    import fcntl
except ImportError:  # Windows does not provide fcntl.
    fcntl = None

from flowpay.core.config import Settings
from flowpay.core.event_log import EventStore, EventStoreProtocol


def _acquire_runtime_lock(lock: TextIO) -> None:
    if fcntl is not None:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return

    import msvcrt

    # msvcrt locks a byte range, so ensure the lock file contains one byte.
    if os.fstat(lock.fileno()).st_size == 0:
        lock.write("\0")
        lock.flush()
    lock.seek(0)
    try:
        msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
    except PermissionError as error:
        raise BlockingIOError("Runtime lock is already held") from error


def _release_runtime_lock(lock: TextIO) -> None:
    if fcntl is not None:
        fcntl.flock(lock, fcntl.LOCK_UN)
        return

    import msvcrt

    lock.seek(0)
    msvcrt.locking(lock.fileno(), msvcrt.LK_UNLCK, 1)


@contextmanager
def open_store(settings: Settings) -> Generator[EventStoreProtocol]:
    """打开事件存储，并在整个使用区间持有运行锁。

    锁跟随本地文件句柄，用于防止同时启动两个管理器恢复或调度同一份会话，
    不是每次查询的锁。必须在退出此上下文前完成后台任务关闭，否则存储可能先于任务释放。
    """
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    with (settings.data_dir / "runtime.lock").open("a") as lock:
        try:
            _acquire_runtime_lock(lock)
        except BlockingIOError as error:
            raise RuntimeError("This data directory already has a running backend") from error
        try:
            yield EventStore(settings.data_dir / "sessions.sqlite")
        finally:
            _release_runtime_lock(lock)
